from django.shortcuts import render

from debugger.demo import DEMO_CODE_CONTEXT, DEMO_ERROR_LOG
from debugger.forms import BugReportForm
from debugger.services.debugger import analyze_bug
from debugger.services.repo_ingest import build_repository_context
from debugger.services.traceback_parse import fallback_evidence, parse_failure_clues


def index(request):
    analysis = None
    analysis_payload = None
    repo_context = None

    if request.method == "POST":
        form = BugReportForm(request.POST, request.FILES)
        if form.is_valid():
            repo_context = build_repository_context(
                error_log=form.cleaned_data["error_log"],
                github_url=form.cleaned_data.get("github_url", ""),
                uploaded_zip=form.cleaned_data.get("repo_zip"),
                manual_context=form.cleaned_data.get("code_context", ""),
            )
            analysis = analyze_bug(
                error_log=form.cleaned_data["error_log"],
                code_context=repo_context.combined_context,
                detected_language=repo_context.detected_language,
                detected_framework=repo_context.detected_framework,
                fallback_evidence=fallback_evidence(
                    parse_failure_clues(form.cleaned_data["error_log"]),
                    repo_context.inspected_files,
                    repo_context.detected_language,
                    repo_context.detected_framework,
                ),
            )
            analysis_payload = analysis.as_dict()
    else:
        form = BugReportForm()

    return render(
        request,
        "debugger/index.html",
        {
            "form": form,
            "analysis": analysis,
            "analysis_payload": analysis_payload,
            "repo_context": repo_context,
            "demo_error_log": DEMO_ERROR_LOG,
            "demo_code_context": DEMO_CODE_CONTEXT,
        },
    )
