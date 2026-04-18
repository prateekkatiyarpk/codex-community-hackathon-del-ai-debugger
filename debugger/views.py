from dataclasses import replace

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from debugger.demo import DEMO_CODE_CONTEXT, DEMO_ERROR_LOG
from debugger.forms import BugReportForm
from debugger.services.debugger import analysis_from_dict, analyze_bug
from debugger.services.repo_ingest import (
    build_repository_context_from_workspace,
    prepare_repository_workspace,
)
from debugger.services.repro_runner import CommandCapture, capture_repro_command
from debugger.services.traceback_parse import fallback_evidence, parse_failure_clues

ANALYSIS_FLASH_SESSION_KEY = "analysis_flash_state"


def index(request):
    analysis = None
    analysis_payload = None
    repo_context = None
    command_capture = None
    command_output_used_for_analysis = False
    failure_source_label = ""

    if request.method == "POST":
        form = BugReportForm(request.POST, request.FILES)
        if form.is_valid():
            pasted_error_log = form.cleaned_data.get("error_log", "").strip()
            repro_command = form.cleaned_data.get("repro_command", "").strip()
            manual_context = form.cleaned_data.get("code_context", "")

            with prepare_repository_workspace(
                github_url=form.cleaned_data.get("github_url", ""),
                github_token=form.cleaned_data.get("github_token", ""),
                uploaded_zip=form.cleaned_data.get("repo_zip"),
            ) as workspace:
                if repro_command and workspace.has_repo_input:
                    command_capture = capture_repro_command(workspace.execution_root, repro_command)
                elif repro_command and not pasted_error_log:
                    command_capture = capture_repro_command(None, repro_command)

                active_error_log = pasted_error_log
                if active_error_log:
                    failure_source_label = "Pasted log"
                elif command_capture and command_capture.has_output:
                    active_error_log = command_capture.output
                    failure_source_label = "Captured from command"
                    command_output_used_for_analysis = True

                if active_error_log:
                    repo_context = build_repository_context_from_workspace(
                        workspace,
                        error_log=active_error_log,
                        manual_context=manual_context,
                    )
                    analysis = analyze_bug(
                        error_log=active_error_log,
                        code_context=repo_context.combined_context,
                        detected_language=repo_context.detected_language,
                        detected_framework=repo_context.detected_framework,
                        fallback_evidence=fallback_evidence(
                            parse_failure_clues(active_error_log),
                            repo_context.inspected_files,
                            repo_context.detected_language,
                            repo_context.detected_framework,
                        ),
                    )
                    _store_analysis_flash(
                        request,
                        analysis=analysis,
                        repo_context=repo_context,
                        command_capture=command_capture,
                        command_output_used_for_analysis=command_output_used_for_analysis,
                        failure_source_label=failure_source_label,
                    )
                    return redirect("debugger:index")
                else:
                    if workspace.errors:
                        for error in workspace.errors:
                            form.add_error(None, error)
                    if command_capture and command_capture.error_message:
                        form.add_error(None, command_capture.error_message)
                    elif repro_command:
                        form.add_error(
                            None,
                            "The repro command did not produce usable output. Paste the failure log instead or try a command that reproduces the failure.",
                        )
    else:
        form = BugReportForm()
        flash_state = request.session.pop(ANALYSIS_FLASH_SESSION_KEY, None)
        if flash_state:
            analysis = _deserialize_analysis(flash_state.get("analysis"))
            if analysis:
                analysis_payload = analysis.as_dict()
            repo_context = flash_state.get("repo_context")
            command_capture = _deserialize_command_capture(flash_state.get("command_capture"))
            command_output_used_for_analysis = bool(flash_state.get("command_output_used_for_analysis"))
            failure_source_label = flash_state.get("failure_source_label", "")

    return render(
        request,
        "debugger/index.html",
        {
            "form": form,
            "analysis": analysis,
            "analysis_payload": analysis_payload,
            "repo_context": repo_context,
            "command_capture": command_capture,
            "command_output_used_for_analysis": command_output_used_for_analysis,
            "failure_source_label": failure_source_label,
            "demo_error_log": DEMO_ERROR_LOG,
            "demo_code_context": DEMO_CODE_CONTEXT,
        },
    )


def intentional_failure(request):
    demo_post = _load_broken_demo_post()
    detail_url = _build_demo_detail_url(demo_post)
    return HttpResponse(detail_url)


def demo_detail(request, pk):
    return HttpResponse(f"Demo detail {pk}")


def _load_broken_demo_post():
    return {"title": "Intentional prod failure"}


def _build_demo_detail_url(post):
    return reverse("debugger:demo-detail", kwargs={"pk": post.get("pk", "")})


def _store_analysis_flash(
    request,
    *,
    analysis,
    repo_context,
    command_capture,
    command_output_used_for_analysis: bool,
    failure_source_label: str,
) -> None:
    request.session[ANALYSIS_FLASH_SESSION_KEY] = {
        "analysis": _serialize_analysis(analysis),
        "repo_context": _serialize_repo_context(repo_context),
        "command_capture": _serialize_command_capture(command_capture),
        "command_output_used_for_analysis": command_output_used_for_analysis,
        "failure_source_label": failure_source_label,
    }


def _serialize_analysis(analysis) -> dict:
    return {
        "payload": analysis.as_dict(),
        "parsed": analysis.parsed,
        "raw_response": analysis.raw_response,
        "fallback_reason": analysis.fallback_reason,
        "source": analysis.source,
    }


def _deserialize_analysis(state: dict | None):
    if not state:
        return None
    payload = state.get("payload")
    if not isinstance(payload, dict):
        return None
    analysis = analysis_from_dict(payload, source=state.get("source", "llm"))
    return replace(
        analysis,
        parsed=bool(state.get("parsed", True)),
        raw_response=state.get("raw_response", ""),
        fallback_reason=state.get("fallback_reason", ""),
    )


def _serialize_repo_context(repo_context) -> dict | None:
    if not repo_context:
        return None
    return {
        "source": repo_context.source,
        "source_label": repo_context.source_label,
        "repo_label": repo_context.repo_label,
        "has_repo_input": repo_context.has_repo_input,
        "errors": list(repo_context.errors),
        "snippets": [
            {
                "file_path": snippet.file_path,
                "start_line": snippet.start_line,
                "end_line": snippet.end_line,
                "reason": snippet.reason,
                "preview": snippet.preview,
            }
            for snippet in repo_context.snippets
        ],
    }


def _serialize_command_capture(command_capture: CommandCapture | None) -> dict | None:
    if not command_capture or not command_capture.attempted:
        return None
    return {
        "command": command_capture.command,
        "attempted": command_capture.attempted,
        "ran": command_capture.ran,
        "output": command_capture.output,
        "exit_code": command_capture.exit_code,
        "timed_out": command_capture.timed_out,
        "error_message": command_capture.error_message,
    }


def _deserialize_command_capture(state: dict | None) -> CommandCapture | None:
    if not state:
        return None
    return CommandCapture(
        command=state.get("command", ""),
        attempted=bool(state.get("attempted", False)),
        ran=bool(state.get("ran", False)),
        output=state.get("output", ""),
        exit_code=state.get("exit_code"),
        timed_out=bool(state.get("timed_out", False)),
        error_message=state.get("error_message", ""),
    )
