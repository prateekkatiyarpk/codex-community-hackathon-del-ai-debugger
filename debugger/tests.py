import io
import json
import os
import subprocess
import tempfile
import urllib.error
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import NoReverseMatch
from django.test import Client, SimpleTestCase

from debugger.demo import DEMO_ANALYSIS, DEMO_CODE_CONTEXT, DEMO_ERROR_LOG
from debugger.forms import BugReportForm
from debugger.services.debugger import (
    DEBUGGER_RESPONSE_FORMAT,
    analysis_from_dict,
    build_prompt_messages,
    fallback_analysis,
    parse_model_response,
)
from debugger.services.language_detect import LanguageProfile, detect_language_profile
from debugger.services.repo_ingest import (
    RepositoryContext,
    RepositoryWorkspace,
    _build_github_request,
    build_repository_context,
)
from debugger.services.repo_search import discover_repo_context, parse_traceback_clues
from debugger.services.repro_runner import CommandCapture, capture_repro_command
from debugger.views import _build_demo_detail_url


REPO_TRACEBACK = """Traceback (most recent call last):
  File "/app/posts/views.py", line 6, in post_list
    return render(request, "posts/list.html", {"posts": posts})
django.urls.exceptions.NoReverseMatch: Reverse for 'post_detail' with keyword arguments '{'pk': ''}' not found."""

INTENTIONAL_FAILURE_TRACEBACK = """Traceback (most recent call last):
  File "/app/debugger/views.py", line 100, in intentional_failure
    detail_url = _build_demo_detail_url(demo_post)
  File "/app/debugger/views.py", line 113, in _build_demo_detail_url
    return reverse("debugger:demo-detail", kwargs={"pk": post.get("pk", "")})
django.urls.exceptions.NoReverseMatch: Reverse for 'demo-detail' with keyword arguments '{'pk': ''}' not found. 1 pattern(s) tried: ['__demo__/post/(?P<pk>[0-9]+)/\\\\Z']"""

PYTEST_FAILURE = """FAILED tests/test_views.py::test_post_list_links - django.urls.exceptions.NoReverseMatch: Reverse for 'post_detail' not found"""
REPRO_OUTPUT = "$ pytest\nExit code: 1\n\nstdout:\nFAILED tests/test_views.py::test_post_list_links"


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


class FakeUrlResponse(io.BytesIO):
    def __init__(self, payload: bytes, headers=None):
        super().__init__(payload)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class DebuggerServiceTests(SimpleTestCase):
    def test_parse_model_response_returns_analysis(self):
        raw = json.dumps(
            {
                "detected_language": "Python",
                "detected_framework": "Django",
                "bug_type": "Queryset shape mismatch",
                "issue_summary": "Template reverses a URL with a missing pk.",
                "root_cause": "The context does not include a primary key.",
                "suspected_location": {"file": "posts/views.py", "function": "post_list"},
                "evidence_used": [
                    "Traceback points to template rendering.",
                    "URL reverse needs pk.",
                ],
                "recommended_fix": {
                    "title": "Include id in values query",
                    "explanation": "Include id in the values() query.",
                    "tradeoff": "Smallest change.",
                    "patch_diff": "",
                },
                "safest_fix": {
                    "title": "Use model instances",
                    "explanation": "Pass Post instances to the template.",
                    "tradeoff": "More robust but broader.",
                    "patch_diff": "",
                },
                "alternative_fix": {
                    "title": "Use slug route",
                    "explanation": "Use slug consistently in route and template.",
                    "tradeoff": "Changes URL contract.",
                    "patch_diff": "",
                },
                "confidence": 0.82,
                "confidence_label": "High confidence",
                "confidence_reason": "File and URL evidence align.",
                "regression_test": "Render the list page with one Post and assert it links to detail.",
            }
        )

        analysis = parse_model_response(raw)

        self.assertTrue(analysis.parsed)
        self.assertEqual(analysis.confidence_percent, 82)
        self.assertEqual(analysis.suspected_location.file, "posts/views.py")
        self.assertEqual(
            analysis.as_dict()["suspected_location"]["function"],
            "post_list",
        )
        self.assertEqual(analysis.detected_language, "Python")
        self.assertEqual(analysis.detected_framework, "Django")
        self.assertEqual(analysis.bug_type, "Queryset shape mismatch")
        self.assertEqual(analysis.recommended_fix.title, "Include id in values query")
        self.assertEqual(analysis.confidence_label, "High confidence")
        self.assertEqual(len(analysis.timeline_steps), 5)
        self.assertIn("Failure log parsed", analysis.timeline_steps[0]["title"])
        self.assertTrue(analysis.diagnosis_reasons)

    def test_parse_model_response_caps_confidence_and_deduplicates_evidence(self):
        raw = json.dumps(
            {
                "detected_language": "Python",
                "detected_framework": "Django",
                "bug_type": "NoReverseMatch",
                "issue_summary": "Reverse() receives an empty pk.",
                "root_cause": "The view passes an empty pk into reverse().",
                "suspected_location": {"file": "debugger/views.py", "function": "_build_demo_detail_url"},
                "evidence_used": [
                    "Traceback points to debugger/views.py line 113.",
                    "Traceback points to debugger/views.py line 113.",
                    "Route requires an integer pk.",
                    "Missing pk is visible in the log.",
                    "The helper reads post.get('pk', '').",
                    "This duplicate line should be dropped.",
                ],
                "recommended_fix": {
                    "title": "Provide a pk",
                    "explanation": "Pass a numeric pk into reverse().",
                    "tradeoff": "Smallest change.",
                    "patch_diff": "",
                },
                "safest_fix": {
                    "title": "Guard missing pk",
                    "explanation": "Check for a missing pk first.",
                    "tradeoff": "More defensive.",
                    "patch_diff": "",
                },
                "alternative_fix": {
                    "title": "Return a fallback URL",
                    "explanation": "Return a fallback response instead of raising.",
                    "tradeoff": "Changes behavior.",
                    "patch_diff": "",
                },
                "confidence": 0.99,
                "confidence_label": "High confidence",
                "confidence_reason": "The traceback and repo point to the same call site.",
                "regression_test": "Add a test for the failure route.",
            }
        )

        analysis = parse_model_response(raw)

        self.assertEqual(analysis.confidence, 0.94)
        self.assertLessEqual(len(analysis.evidence_used), 5)
        self.assertEqual(analysis.evidence_used[0], "Traceback points to debugger/views.py line 113.")

    def test_analysis_validation_rejects_missing_required_fields(self):
        with self.assertRaises(ValueError):
            analysis_from_dict({"issue_summary": "Too small"})

    def test_fallback_analysis_is_renderable(self):
        analysis = fallback_analysis("not json", "Bad JSON")

        self.assertFalse(analysis.parsed)
        self.assertEqual(analysis.confidence, 0.0)
        self.assertIn("not json", analysis.raw_response)

    def test_response_format_requires_expected_product_fields(self):
        schema = DEBUGGER_RESPONSE_FORMAT["json_schema"]["schema"]

        self.assertEqual(DEBUGGER_RESPONSE_FORMAT["type"], "json_schema")
        self.assertTrue(DEBUGGER_RESPONSE_FORMAT["json_schema"]["strict"])
        self.assertIn("evidence_used", schema["required"])
        self.assertIn("recommended_fix", schema["required"])
        self.assertIn("confidence_label", schema["required"])
        self.assertFalse(schema["additionalProperties"])

    def test_prompt_messages_use_python_and_django_guidance(self):
        messages = build_prompt_messages(
            error_log=REPO_TRACEBACK,
            code_context="def post_list(request): ...",
            detected_language="Python",
            detected_framework="Django",
        )

        self.assertIn("senior Python debugging assistant", messages[0]["content"])
        self.assertIn("recommended_fix", messages[0]["content"])
        self.assertIn("If any patch diff is uncertain", messages[0]["content"])
        self.assertIn("Prompt mode: Python-optimized debugging", messages[1]["content"])
        self.assertIn("Framework-specific guidance for Django", messages[1]["content"])
        self.assertIn("URL routing", messages[1]["content"])

    def test_prompt_messages_include_flask_and_fastapi_guidance(self):
        cases = {
            "Flask": "Flask's test client",
            "FastAPI": "request/response validation",
        }

        for framework, expected in cases.items():
            with self.subTest(framework=framework):
                messages = build_prompt_messages(
                    error_log="RuntimeError: example",
                    code_context="",
                    detected_language="Python",
                    detected_framework=framework,
                )

                self.assertIn(f"Framework-specific guidance for {framework}", messages[1]["content"])
                self.assertIn(expected, messages[1]["content"])

    def test_prompt_messages_use_generic_triage_for_non_python(self):
        messages = build_prompt_messages(
            error_log="TypeError: Cannot read properties of undefined",
            code_context="export function App() { return user.name; }",
            detected_language="TypeScript",
            detected_framework="React",
        )

        self.assertIn("senior code-debugging triage assistant", messages[0]["content"])
        self.assertNotIn("senior Python debugging assistant", messages[0]["content"])
        self.assertIn("Prompt mode: Generic code triage", messages[1]["content"])
        self.assertIn("no Django, Flask, or FastAPI guidance applies", messages[1]["content"])
        self.assertIn("recommended_fix", messages[0]["content"])


class ReproRunnerTests(SimpleTestCase):
    @patch.dict(os.environ, {"DJANGO_DEBUG": "0", "AI_DEBUGGER_ENABLE_COMMAND_EXECUTION": "0"})
    def test_capture_repro_command_respects_execution_toggle(self):
        capture = capture_repro_command(Path("."), "pytest")

        self.assertTrue(capture.attempted)
        self.assertFalse(capture.ran)
        self.assertIn("disabled on this deployment", capture.error_message)

    def test_capture_repro_command_returns_helpful_error_without_repo(self):
        capture = capture_repro_command(None, "pytest")

        self.assertTrue(capture.attempted)
        self.assertFalse(capture.ran)
        self.assertFalse(capture.has_output)
        self.assertIn("Add a repo ZIP", capture.error_message)

    def test_capture_repro_command_rejects_unsupported_command(self):
        capture = capture_repro_command(Path("."), "bash -lc 'pytest'")

        self.assertTrue(capture.attempted)
        self.assertFalse(capture.ran)
        self.assertIn("supports common test and build repro commands", capture.error_message)

    @patch("debugger.services.repro_runner.subprocess.run")
    def test_capture_repro_command_formats_output(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["pytest"],
            returncode=1,
            stdout="FAILED tests/test_views.py::test_post_list_links",
            stderr="",
        )

        capture = capture_repro_command(Path("."), "pytest")

        self.assertTrue(capture.attempted)
        self.assertTrue(capture.ran)
        self.assertTrue(capture.has_output)
        self.assertEqual(capture.exit_code, 1)
        self.assertIn("$ pytest", capture.output)
        self.assertIn("Exit code: 1", capture.output)


class DebuggerViewTests(SimpleTestCase):
    def test_index_renders_theme_toggle(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-option="light"')
        self.assertContains(response, 'data-theme-option="dark"')
        self.assertContains(response, 'ai-debugger-theme')

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""})
    def test_demo_post_renders_structured_result_without_api_key(self):
        response = Client().post(
            "/",
            {
                "error_log": DEMO_ERROR_LOG,
                "code_context": DEMO_CODE_CONTEXT,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diagnosis Complete")
        self.assertContains(response, "Analysis Timeline")
        self.assertContains(response, "Why this diagnosis?")
        self.assertContains(response, "Ranked fix options")
        self.assertContains(response, "Copy JSON")
        self.assertContains(response, "The post list template tries to reverse")

    @patch("debugger.views.analyze_bug")
    @patch("debugger.views.build_repository_context_from_workspace")
    def test_github_token_reaches_analysis_flow(self, mock_build_context, mock_analyze_bug):
        workspace = RepositoryWorkspace(
            source="github",
            repo_label="acme/private-repo",
            analysis_root=None,
            execution_root=None,
            errors=[],
        )

        @contextmanager
        def fake_workspace(**kwargs):
            self.assertEqual(kwargs["github_url"], "https://github.com/acme/private-repo")
            self.assertEqual(kwargs["github_token"], "github_pat_secret")
            yield workspace

        mock_build_context.return_value = RepositoryContext(
            source="github",
            repo_label="acme/private-repo",
            language_profile=LanguageProfile(language="Python", framework="Django"),
            snippets=[],
            errors=[],
            combined_context="repo context",
        )
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")

        with patch("debugger.views.prepare_repository_workspace", side_effect=fake_workspace):
            response = Client().post(
                "/",
                {
                    "error_log": REPO_TRACEBACK,
                    "github_url": "https://github.com/acme/private-repo",
                    "github_token": "github_pat_secret",
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        _args, kwargs = mock_analyze_bug.call_args
        self.assertEqual(kwargs["code_context"], "repo context")
        self.assertEqual(kwargs["detected_language"], "Python")
        self.assertEqual(kwargs["detected_framework"], "Django")

    def test_github_token_is_not_rendered_back_after_form_error(self):
        token = "github_pat_secret_token_value"
        response = Client().post(
            "/",
            {
                "error_log": REPO_TRACEBACK,
                "github_url": "not a github url",
                "github_token": token,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, token)

    @patch("debugger.views.analyze_bug")
    def test_successful_post_redirects_and_reload_clears_previous_analysis(self, mock_analyze_bug):
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")
        client = Client()

        response = client.post(
            "/",
            {
                "error_log": "CUSTOM_FAILURE_MARKER",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [("/", 302)])
        self.assertContains(response, "Diagnosis Complete")
        self.assertNotContains(response, "CUSTOM_FAILURE_MARKER")

        reload_response = client.get("/")

        self.assertEqual(reload_response.status_code, 200)
        self.assertNotContains(reload_response, "Diagnosis Complete")

    @patch("debugger.views.analyze_bug")
    @patch("debugger.views.build_repository_context_from_workspace")
    def test_repo_error_without_snippets_uses_compact_fallback_notice(self, mock_build_context, mock_analyze_bug):
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")
        mock_build_context.return_value = RepositoryContext(
            source="github",
            repo_label="https://github.com/acme/private-repo",
            language_profile=LanguageProfile(language="Python", framework="Django"),
            snippets=[],
            errors=["GitHub rate limit was reached while fetching this repo. Try again later, add a token, or use ZIP upload."],
            combined_context="",
        )

        response = Client().post(
            "/",
            {
                "error_log": REPO_TRACEBACK,
                "github_url": "https://github.com/acme/private-repo",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Repository context unavailable.")
        self.assertContains(response, "Analysis continued using the failure log and any optional extra context.")
        self.assertNotContains(response, "0 top files shown")

    @patch("debugger.views.analyze_bug")
    def test_zip_upload_discovers_context_for_analysis(self, mock_analyze_bug):
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")
        repo_zip = SimpleUploadedFile(
            "repo.zip",
            make_zip(
                {
                    "manage.py": "from django.core.management import execute_from_command_line\n",
                    "posts/views.py": 'def post_list(request):\n    posts = Post.objects.values("title", "slug")\n',
                    "posts/templates/posts/list.html": "{% url 'post_detail' pk=post.pk %}",
                }
            ),
            content_type="application/zip",
        )

        response = Client().post(
            "/",
            {
                "error_log": REPO_TRACEBACK,
                "repo_zip": repo_zip,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Context discovered from repo")
        self.assertContains(response, "posts/views.py")
        _args, kwargs = mock_analyze_bug.call_args
        self.assertIn("posts/views.py", kwargs["code_context"])
        self.assertEqual(kwargs["detected_language"], "Python")
        self.assertEqual(kwargs["detected_framework"], "Django")
        html = response.content.decode("utf-8")
        self.assertLess(html.index("Start with the recommended fix"), html.index("Why this diagnosis?"))
        self.assertLess(html.index("Recommended Patch Diff"), html.index("Context discovered from repo"))

    def test_intentional_failure_route_returns_500(self):
        response = Client(raise_request_exception=False).get("/__demo__/intentional-failure/")

        self.assertEqual(response.status_code, 500)

    def test_intentional_failure_route_raises_no_reverse_match(self):
        with self.assertRaises(NoReverseMatch):
            _build_demo_detail_url({"title": "Intentional prod failure"})

    @patch("debugger.views.analyze_bug")
    @patch("debugger.views.capture_repro_command")
    def test_repro_command_without_pasted_log_uses_captured_output(self, mock_capture, mock_analyze_bug):
        mock_capture.return_value = CommandCapture(
            command="pytest",
            attempted=True,
            ran=True,
            output=REPRO_OUTPUT,
            exit_code=1,
        )
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")
        repo_zip = SimpleUploadedFile(
            "repo.zip",
            make_zip(
                {
                    "manage.py": "from django.core.management import execute_from_command_line\n",
                    "tests/test_views.py": "def test_post_list_links(): assert False\n",
                }
            ),
            content_type="application/zip",
        )

        response = Client().post(
            "/",
            {
                "error_log": "",
                "repro_command": "pytest",
                "repo_zip": repo_zip,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Failure source: Captured from command")
        self.assertContains(response, "Captured Command Output")
        _args, kwargs = mock_analyze_bug.call_args
        self.assertEqual(kwargs["error_log"], REPRO_OUTPUT)

    @patch("debugger.views.analyze_bug")
    @patch("debugger.views.capture_repro_command")
    def test_repro_command_with_pasted_log_prefers_pasted_failure(self, mock_capture, mock_analyze_bug):
        mock_capture.return_value = CommandCapture(
            command="pytest",
            attempted=True,
            ran=True,
            output=REPRO_OUTPUT,
            exit_code=1,
        )
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")
        repo_zip = SimpleUploadedFile(
            "repo.zip",
            make_zip(
                {
                    "manage.py": "from django.core.management import execute_from_command_line\n",
                    "tests/test_views.py": "def test_post_list_links(): assert False\n",
                }
            ),
            content_type="application/zip",
        )

        response = Client().post(
            "/",
            {
                "error_log": REPO_TRACEBACK,
                "repro_command": "pytest",
                "repo_zip": repo_zip,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Failure source: Pasted log")
        self.assertContains(response, "Captured Command Output")
        _args, kwargs = mock_analyze_bug.call_args
        self.assertEqual(kwargs["error_log"], REPO_TRACEBACK)


class RepositoryContextTests(SimpleTestCase):
    def test_build_github_request_omits_auth_header_without_token(self):
        request = _build_github_request("https://api.github.com/repos/acme/public-repo")
        headers = {key.lower(): value for key, value in request.header_items()}

        self.assertNotIn("authorization", headers)

    def test_build_github_request_includes_auth_header_with_token(self):
        request = _build_github_request(
            "https://api.github.com/repos/acme/private-repo",
            github_token="github_pat_secret",
        )
        headers = {key.lower(): value for key, value in request.header_items()}

        self.assertEqual(headers["authorization"], "Bearer github_pat_secret")

    def test_parse_traceback_clues_extracts_file_line_and_exception(self):
        clues = parse_traceback_clues(REPO_TRACEBACK)

        self.assertIn("views.py", clues.file_names)
        self.assertEqual(clues.line_numbers["views.py"], 6)
        self.assertEqual(clues.exception_type, "NoReverseMatch")
        self.assertIn("post_list", clues.symbols)

    def test_parse_traceback_clues_extracts_pytest_nodeid(self):
        clues = parse_traceback_clues(PYTEST_FAILURE)

        self.assertIn("tests/test_views.py", clues.file_names)
        self.assertIn("test_post_list_links", clues.test_names)
        self.assertEqual(clues.line_numbers["tests/test_views.py"], 1)

    def test_discover_repo_context_prioritizes_traceback_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            posts = root / "posts"
            posts.mkdir()
            (posts / "views.py").write_text(
                "\n".join(
                    [
                        "from django.shortcuts import render",
                        "",
                        "def post_list(request):",
                        "    posts = Post.objects.values('title', 'slug')",
                        "    return render(request, 'posts/list.html', {'posts': posts})",
                    ]
                ),
                encoding="utf-8",
            )

            snippets = discover_repo_context(root, REPO_TRACEBACK)

        self.assertTrue(snippets)
        self.assertEqual(snippets[0].file_path, "posts/views.py")

    def test_discover_repo_context_includes_django_url_and_template_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            posts = root / "posts"
            templates = posts / "templates" / "posts"
            templates.mkdir(parents=True)
            (posts / "views.py").write_text(
                "def post_list(request):\n    return render(request, 'posts/list.html', {'posts': posts})\n",
                encoding="utf-8",
            )
            (posts / "urls.py").write_text(
                "urlpatterns = [path('posts/<int:pk>/', views.post_detail, name='post_detail')]\n",
                encoding="utf-8",
            )
            (templates / "list.html").write_text(
                "{% for post in posts %}{% url 'post_detail' pk=post.pk %}{% endfor %}",
                encoding="utf-8",
            )

            snippets = discover_repo_context(root, REPO_TRACEBACK)

        paths = {snippet.file_path for snippet in snippets}
        self.assertIn("posts/urls.py", paths)
        self.assertIn("posts/templates/posts/list.html", paths)

    def test_discover_repo_context_drops_low_signal_internal_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "debugger").mkdir()
            (root / "debugger" / "services").mkdir(parents=True)
            (root / "ai_debugger").mkdir()
            (root / "debugger" / "views.py").write_text(
                "\n".join(
                    [
                        "from django.urls import reverse",
                        "",
                        "def intentional_failure(request):",
                        "    demo_post = _load_broken_demo_post()",
                        "    detail_url = _build_demo_detail_url(demo_post)",
                        "    return HttpResponse(detail_url)",
                        "",
                        "def _build_demo_detail_url(post):",
                        "    return reverse('debugger:demo-detail', kwargs={'pk': post.get('pk', '')})",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "debugger" / "urls.py").write_text(
                "urlpatterns = [path('__demo__/post/<int:pk>/', views.demo_detail, name='demo-detail')]\n",
                encoding="utf-8",
            )
            (root / "ai_debugger" / "urls.py").write_text(
                "urlpatterns = [path('', include('debugger.urls'))]\n",
                encoding="utf-8",
            )
            (root / "debugger" / "tests.py").write_text(
                "def test_intentional_failure_route_returns_500(): pass\n",
                encoding="utf-8",
            )
            (root / "debugger" / "demo.py").write_text(
                'DEMO_ERROR_LOG = "Traceback (most recent call last): ..."\n',
                encoding="utf-8",
            )
            (root / "debugger" / "services" / "repo_search.py").write_text(
                "def score_file(relative_path, content, clues):\n    return 0, []\n",
                encoding="utf-8",
            )

            snippets = discover_repo_context(root, INTENTIONAL_FAILURE_TRACEBACK)

        paths = [snippet.file_path for snippet in snippets]
        self.assertEqual(paths[0], "debugger/views.py")
        self.assertIn("debugger/urls.py", paths)
        self.assertNotIn("ai_debugger/urls.py", paths)
        self.assertNotIn("debugger/services/repo_search.py", paths)
        self.assertLessEqual(len(paths), 4)

    def test_build_repository_context_reports_unsafe_zip_path(self):
        uploaded = SimpleUploadedFile(
            "repo.zip",
            make_zip({"../evil.py": "print('nope')"}),
            content_type="application/zip",
        )

        context = build_repository_context(
            error_log=REPO_TRACEBACK,
            uploaded_zip=uploaded,
            manual_context="fallback",
        )

        self.assertTrue(context.errors)
        self.assertIn("unsafe", context.errors[0])
        self.assertIn("fallback", context.combined_context)

    @patch("debugger.services.repo_ingest.urllib.request.urlopen")
    def test_private_github_repo_without_token_returns_private_repo_hint(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/acme/private-repo",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b""),
        )

        context = build_repository_context(
            error_log=REPO_TRACEBACK,
            github_url="https://github.com/acme/private-repo",
        )

        self.assertTrue(context.errors)
        self.assertIn("If it is private, add a GitHub access token", context.errors[0])

    @patch("debugger.services.repo_ingest.urllib.request.urlopen")
    def test_private_github_repo_with_invalid_token_returns_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/acme/private-repo",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b""),
        )

        context = build_repository_context(
            error_log=REPO_TRACEBACK,
            github_url="https://github.com/acme/private-repo",
            github_token="github_pat_secret",
        )

        self.assertTrue(context.errors)
        self.assertIn("token was rejected", context.errors[0])

    @patch("debugger.services.repo_ingest.urllib.request.urlopen")
    def test_authenticated_github_repo_download_builds_context(self, mock_urlopen):
        mock_urlopen.side_effect = [
            FakeUrlResponse(json.dumps({"default_branch": "main"}).encode("utf-8")),
            FakeUrlResponse(
                make_zip(
                    {
                        "private-repo-main/manage.py": "from django.core.management import execute_from_command_line\n",
                        "private-repo-main/posts/views.py": "def post_list(request): pass\n",
                    }
                )
            ),
        ]

        context = build_repository_context(
            error_log=REPO_TRACEBACK,
            github_url="https://github.com/acme/private-repo",
            github_token="github_pat_secret",
        )

        self.assertEqual(context.detected_language, "Python")
        requests = [call.args[0] for call in mock_urlopen.call_args_list]
        first_headers = {key.lower(): value for key, value in requests[0].header_items()}
        second_headers = {key.lower(): value for key, value in requests[1].header_items()}
        self.assertEqual(first_headers["authorization"], "Bearer github_pat_secret")
        self.assertEqual(second_headers["authorization"], "Bearer github_pat_secret")
        self.assertEqual(requests[0].full_url, "https://api.github.com/repos/acme/private-repo")
        self.assertTrue(requests[1].full_url.endswith("/repos/acme/private-repo/zipball/main"))

    def test_language_detection_supports_javascript_repo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"react": "^19.0.0"}}),
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "App.jsx").write_text("export function App() { return null; }", encoding="utf-8")

            profile = detect_language_profile(root)

        self.assertEqual(profile.language, "JavaScript")
        self.assertEqual(profile.framework, "React")

    def test_language_detection_reads_python_config_for_framework(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pyproject.toml").write_text(
                "[project]\ndependencies = ['Django>=5.0']\n",
                encoding="utf-8",
            )
            (root / "app.py").write_text("print('hello')", encoding="utf-8")

            profile = detect_language_profile(root)

        self.assertEqual(profile.language, "Python")
        self.assertEqual(profile.framework, "Django")

    def test_language_detection_detects_django_project_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "config"
            posts = root / "posts"
            templates = posts / "templates" / "posts"
            project.mkdir()
            posts.mkdir()
            templates.mkdir(parents=True)
            (project / "settings.py").write_text("INSTALLED_APPS = []", encoding="utf-8")
            (project / "urls.py").write_text("urlpatterns = []", encoding="utf-8")
            (posts / "views.py").write_text("def post_list(request): pass", encoding="utf-8")
            (templates / "list.html").write_text("{{ posts }}", encoding="utf-8")

            profile = detect_language_profile(root)

        self.assertEqual(profile.language, "Python")
        self.assertEqual(profile.framework, "Django")

    def test_zip_context_detects_nested_django_project(self):
        uploaded = SimpleUploadedFile(
            "repo.zip",
            make_zip(
                {
                    "demo-main/manage.py": "from django.core.management import execute_from_command_line\n",
                    "demo-main/posts/views.py": "def post_list(request): pass\n",
                    "demo-main/posts/templates/posts/list.html": "{% url 'post_detail' pk=post.pk %}",
                }
            ),
            content_type="application/zip",
        )

        context = build_repository_context(error_log=REPO_TRACEBACK, uploaded_zip=uploaded)

        self.assertEqual(context.detected_language, "Python")
        self.assertEqual(context.detected_framework, "Django")
        self.assertIn("demo-main/posts/views.py", context.inspected_files)

    def test_language_detection_handles_github_archive_top_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_root = root / "demo-main"
            app_root.mkdir()
            (app_root / "package.json").write_text(
                json.dumps({"dependencies": {"next": "^15.0.0", "react": "^19.0.0"}}),
                encoding="utf-8",
            )
            (app_root / "pages").mkdir()
            (app_root / "pages" / "index.tsx").write_text("export default function Home() { return null; }", encoding="utf-8")

            profile = detect_language_profile(root)

        self.assertEqual(profile.language, "TypeScript")
        self.assertEqual(profile.framework, "Next.js")

    def test_zip_upload_takes_precedence_over_github_url_validation(self):
        uploaded = SimpleUploadedFile(
            "repo.zip",
            make_zip({"posts/views.py": "def post_list(request):\n    pass\n"}),
            content_type="application/zip",
        )
        form = BugReportForm(
            data={
                "error_log": REPO_TRACEBACK,
                "github_url": "not a github url",
                "github_token": "github_pat_secret",
                "code_context": "",
            },
            files={"repo_zip": uploaded},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["github_url"], "")
        self.assertEqual(form.cleaned_data["github_token"], "")

    def test_form_accepts_private_github_url_with_token(self):
        form = BugReportForm(
            data={
                "error_log": REPO_TRACEBACK,
                "github_url": "https://github.com/acme/private-repo",
                "github_token": "github_pat_secret",
                "code_context": "",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["github_url"], "https://github.com/acme/private-repo")
        self.assertEqual(form.cleaned_data["github_token"], "github_pat_secret")

    def test_form_requires_failure_signal(self):
        form = BugReportForm(
            data={
                "error_log": "",
                "repro_command": "",
                "github_url": "",
                "code_context": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Provide a pasted failure log or a repro command.", form.non_field_errors())

    def test_form_requires_repo_for_repro_command_only_mode(self):
        form = BugReportForm(
            data={
                "error_log": "",
                "repro_command": "pytest",
                "github_url": "",
                "code_context": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Add a repo ZIP or GitHub URL", form.errors["repro_command"][0])
