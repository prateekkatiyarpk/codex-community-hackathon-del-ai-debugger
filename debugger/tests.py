import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase

from debugger.demo import DEMO_ANALYSIS, DEMO_CODE_CONTEXT, DEMO_ERROR_LOG
from debugger.forms import BugReportForm
from debugger.services.debugger import (
    DEBUGGER_RESPONSE_FORMAT,
    analysis_from_dict,
    fallback_analysis,
    parse_model_response,
)
from debugger.services.language_detect import detect_language_profile
from debugger.services.repo_ingest import build_repository_context
from debugger.services.repo_search import discover_repo_context, parse_traceback_clues


REPO_TRACEBACK = """Traceback (most recent call last):
  File "/app/posts/views.py", line 6, in post_list
    return render(request, "posts/list.html", {"posts": posts})
django.urls.exceptions.NoReverseMatch: Reverse for 'post_detail' with keyword arguments '{'pk': ''}' not found."""


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


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


class DebuggerViewTests(SimpleTestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""})
    def test_demo_post_renders_structured_result_without_api_key(self):
        response = Client().post(
            "/",
            {
                "error_log": DEMO_ERROR_LOG,
                "code_context": DEMO_CODE_CONTEXT,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diagnosis Complete")
        self.assertContains(response, "Analysis Timeline")
        self.assertContains(response, "Evidence Used")
        self.assertContains(response, "Ranked fix options")
        self.assertContains(response, "Copy JSON")
        self.assertContains(response, "The post list template tries to reverse")

    @patch("debugger.views.analyze_bug")
    def test_zip_upload_discovers_context_for_analysis(self, mock_analyze_bug):
        mock_analyze_bug.return_value = analysis_from_dict(DEMO_ANALYSIS, source="llm")
        repo_zip = SimpleUploadedFile(
            "repo.zip",
            make_zip(
                {
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
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Context discovered from repo")
        self.assertContains(response, "posts/views.py")
        _args, kwargs = mock_analyze_bug.call_args
        self.assertIn("posts/views.py", kwargs["code_context"])
        self.assertEqual(kwargs["detected_language"], "Python")
        self.assertEqual(kwargs["detected_framework"], "Unknown")


class RepositoryContextTests(SimpleTestCase):
    def test_parse_traceback_clues_extracts_file_line_and_exception(self):
        clues = parse_traceback_clues(REPO_TRACEBACK)

        self.assertIn("views.py", clues.file_names)
        self.assertEqual(clues.line_numbers["views.py"], 6)
        self.assertEqual(clues.exception_type, "NoReverseMatch")
        self.assertIn("post_list", clues.symbols)

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
                "code_context": "",
            },
            files={"repo_zip": uploaded},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["github_url"], "")
