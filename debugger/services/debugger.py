from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from debugger.demo import DEMO_ANALYSIS, DEMO_CODE_CONTEXT, DEMO_ERROR_LOG


SYSTEM_PROMPT = """You are a senior failure triage assistant for software repositories.
Work for any language/framework, with strongest support for Python repos.
Prioritize evidence from the error log and discovered repository snippets.
Prefer one clear diagnosis over many vague guesses.
For Python repos, use traceback-first reasoning and framework-specific fix heuristics.
For non-Python repos, stay useful but conservative; leave patch diffs empty when evidence is weak.
Return strict JSON only. Do not include markdown, commentary, or code fences."""

USER_PROMPT_TEMPLATE = """Analyze this repository failure.

Detected language: {detected_language}
Detected framework: {detected_framework}

Return a JSON object that matches the required schema. Use concise, evidence-backed
language. Provide three ranked fix options. Only include patch diffs when the
evidence is strong enough to support a minimal change.

Failure log:
```text
{error_log}
```

Auto-discovered repository context and optional extra context:
```text
{code_context}
```"""


FIX_OPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "explanation": {"type": "string"},
        "tradeoff": {"type": "string"},
        "patch_diff": {"type": "string"},
    },
    "required": ["title", "explanation", "tradeoff", "patch_diff"],
}


DEBUGGER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "failure_triage_analysis",
        "description": "Evidence-backed failure triage for a software repository.",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "detected_language": {"type": "string"},
                "detected_framework": {"type": "string"},
                "bug_type": {"type": "string"},
                "issue_summary": {"type": "string"},
                "root_cause": {"type": "string"},
                "suspected_location": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "file": {"type": "string"},
                        "function": {"type": "string"},
                    },
                    "required": ["file", "function"],
                },
                "evidence_used": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "recommended_fix": FIX_OPTION_SCHEMA,
                "safest_fix": FIX_OPTION_SCHEMA,
                "alternative_fix": FIX_OPTION_SCHEMA,
                "confidence": {"type": "number"},
                "confidence_label": {"type": "string"},
                "confidence_reason": {"type": "string"},
                "regression_test": {"type": "string"},
            },
            "required": [
                "detected_language",
                "detected_framework",
                "bug_type",
                "issue_summary",
                "root_cause",
                "suspected_location",
                "evidence_used",
                "recommended_fix",
                "safest_fix",
                "alternative_fix",
                "confidence",
                "confidence_label",
                "confidence_reason",
                "regression_test",
            ],
        },
    },
}


@dataclass(frozen=True)
class SuspectedLocation:
    file: str
    function: str


@dataclass(frozen=True)
class FixOption:
    title: str
    explanation: str
    tradeoff: str
    patch_diff: str = ""


@dataclass(frozen=True)
class DebuggerAnalysis:
    detected_language: str
    detected_framework: str
    bug_type: str
    issue_summary: str
    root_cause: str
    suspected_location: SuspectedLocation
    evidence_used: list[str]
    recommended_fix: FixOption
    safest_fix: FixOption
    alternative_fix: FixOption
    confidence: float
    confidence_label: str
    confidence_reason: str
    regression_test: str
    parsed: bool = True
    raw_response: str = ""
    fallback_reason: str = ""
    source: str = "llm"

    @property
    def confidence_percent(self) -> int:
        return round(self.confidence * 100)

    @property
    def confidence_explanation(self) -> str:
        return self.confidence_reason

    @property
    def suggested_fix(self) -> str:
        return self.recommended_fix.explanation

    @property
    def patch_diff(self) -> str:
        return self.recommended_fix.patch_diff

    @property
    def fix_options(self) -> list[FixOption]:
        return [self.recommended_fix, self.safest_fix, self.alternative_fix]

    @property
    def timeline_steps(self) -> list[dict[str, str]]:
        return [
            {
                "badge": "01",
                "title": "Failure log parsed",
                "detail": "The error output was reduced to filenames, symbols, exception names, and test clues.",
            },
            {
                "badge": "02",
                "title": "Repository context inspected",
                "detail": f"{self.detected_language} / {self.detected_framework} signals were used to rank relevant files.",
            },
            {
                "badge": "03",
                "title": "Suspected location inferred",
                "detail": f"{self.suspected_location.file} - {self.suspected_location.function}",
            },
            {
                "badge": "04",
                "title": "Evidence weighed",
                "detail": self.evidence_used[0] if self.evidence_used else self.issue_summary,
            },
            {
                "badge": "05",
                "title": "Ranked fixes generated",
                "detail": self.recommended_fix.title,
            },
        ]

    @property
    def diagnosis_reasons(self) -> list[str]:
        return self.evidence_used or [
            "The issue summary and root cause agree on one primary failure path.",
            "The suspected location gives a concrete place to inspect first.",
            "The recommended fix is intentionally narrow so it can be tested quickly.",
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "detected_language": self.detected_language,
            "detected_framework": self.detected_framework,
            "bug_type": self.bug_type,
            "issue_summary": self.issue_summary,
            "root_cause": self.root_cause,
            "suspected_location": {
                "file": self.suspected_location.file,
                "function": self.suspected_location.function,
            },
            "evidence_used": self.evidence_used,
            "recommended_fix": _fix_as_dict(self.recommended_fix),
            "safest_fix": _fix_as_dict(self.safest_fix),
            "alternative_fix": _fix_as_dict(self.alternative_fix),
            "confidence": self.confidence,
            "confidence_label": self.confidence_label,
            "confidence_reason": self.confidence_reason,
            "regression_test": self.regression_test,
        }


class DebuggerServiceError(Exception):
    """Raised when the LLM transport cannot produce a usable response."""


def analyze_bug(
    error_log: str,
    code_context: str = "",
    detected_language: str = "Unknown",
    detected_framework: str = "Unknown",
    fallback_evidence: list[str] | None = None,
) -> DebuggerAnalysis:
    """Analyze a pasted failure and always return a renderable result."""
    error_log = error_log.strip()
    code_context = code_context.strip()
    fallback_evidence = fallback_evidence or []

    if _is_demo_payload(error_log, code_context) and not os.environ.get("OPENAI_API_KEY"):
        return analysis_from_dict(DEMO_ANALYSIS, source="demo")

    try:
        raw_response = _call_openai(
            error_log=error_log,
            code_context=code_context,
            detected_language=detected_language,
            detected_framework=detected_framework,
        )
    except Exception as exc:
        if _is_demo_payload(error_log, code_context):
            demo = analysis_from_dict(DEMO_ANALYSIS, source="demo")
            return replace(
                demo,
                fallback_reason=f"Using the built-in demo analysis because the LLM call failed: {exc}",
            )
        return fallback_analysis(
            raw_response=str(exc),
            reason="The analyzer could not reach the LLM service.",
            detected_language=detected_language,
            detected_framework=detected_framework,
            fallback_evidence=fallback_evidence,
        )

    try:
        return parse_model_response(
            raw_response,
            fallback_language=detected_language,
            fallback_framework=detected_framework,
            fallback_evidence=fallback_evidence,
        )
    except ValueError as exc:
        return fallback_analysis(
            raw_response=raw_response,
            reason=f"The model returned output that was not valid for this app: {exc}",
            detected_language=detected_language,
            detected_framework=detected_framework,
            fallback_evidence=fallback_evidence,
        )


def _call_openai(
    *,
    error_log: str,
    code_context: str,
    detected_language: str,
    detected_framework: str,
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise DebuggerServiceError("OPENAI_API_KEY is not set.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise DebuggerServiceError("The openai package is not installed.") from exc

    model = os.environ.get("AI_DEBUGGER_MODEL", "gpt-5.4-mini")
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "45"))
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    client_kwargs = {"api_key": api_key, "timeout": timeout}
    if base_url:
        client_kwargs["base_url"] = base_url

    client = OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        response_format=DEBUGGER_RESPONSE_FORMAT,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    detected_language=detected_language or "Unknown",
                    detected_framework=detected_framework or "Unknown",
                    error_log=error_log,
                    code_context=code_context or "No repository context provided.",
                ),
            },
        ],
    )

    content = response.choices[0].message.content
    if not content:
        raise DebuggerServiceError("The LLM returned an empty response.")
    return content


def parse_model_response(
    raw_response: str,
    fallback_language: str = "Unknown",
    fallback_framework: str = "Unknown",
    fallback_evidence: list[str] | None = None,
) -> DebuggerAnalysis:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse failed at character {exc.pos}") from exc

    return analysis_from_dict(
        payload,
        fallback_language=fallback_language,
        fallback_framework=fallback_framework,
        fallback_evidence=fallback_evidence or [],
    )


def analysis_from_dict(
    payload: dict[str, Any],
    source: str = "llm",
    fallback_language: str = "Unknown",
    fallback_framework: str = "Unknown",
    fallback_evidence: list[str] | None = None,
) -> DebuggerAnalysis:
    if not isinstance(payload, dict):
        raise ValueError("top-level response must be a JSON object")

    suspected = payload.get("suspected_location")
    if not isinstance(suspected, dict):
        raise ValueError("suspected_location must be an object")

    confidence = _coerce_confidence(payload.get("confidence"))
    detected_language = _string_or_unknown(payload.get("detected_language"), fallback_language)
    detected_framework = _string_or_unknown(payload.get("detected_framework"), fallback_framework)
    evidence_used = _string_list(payload.get("evidence_used")) or list(fallback_evidence or [])

    recommended_fix = _fix_from_payload(
        payload.get("recommended_fix"),
        fallback_title="Recommended fix",
        fallback_explanation=payload.get("suggested_fix", ""),
        fallback_patch=payload.get("patch_diff", ""),
    )
    safest_fix = _fix_from_payload(
        payload.get("safest_fix"),
        fallback_title="Safest long-term fix",
        fallback_explanation=recommended_fix.explanation,
    )
    alternative_fix = _fix_from_payload(
        payload.get("alternative_fix"),
        fallback_title="Alternative fix",
        fallback_explanation=recommended_fix.explanation,
    )

    return DebuggerAnalysis(
        detected_language=detected_language,
        detected_framework=detected_framework,
        bug_type=_required_string(payload, "bug_type", default="Unknown runtime issue"),
        issue_summary=_required_string(payload, "issue_summary"),
        root_cause=_required_string(payload, "root_cause"),
        suspected_location=SuspectedLocation(
            file=_string_or_unknown(suspected.get("file")),
            function=_string_or_unknown(suspected.get("function")),
        ),
        evidence_used=evidence_used[:6],
        recommended_fix=recommended_fix,
        safest_fix=safest_fix,
        alternative_fix=alternative_fix,
        confidence=confidence,
        confidence_label=_coerce_confidence_label(payload.get("confidence_label"), confidence),
        confidence_reason=_required_string(
            payload,
            "confidence_reason",
            default=_default_confidence_reason(confidence),
        ),
        regression_test=_required_string(payload, "regression_test"),
        source=source,
    )


def fallback_analysis(
    raw_response: str,
    reason: str,
    detected_language: str = "Unknown",
    detected_framework: str = "Unknown",
    fallback_evidence: list[str] | None = None,
) -> DebuggerAnalysis:
    confidence = 0.0
    evidence_used = list(fallback_evidence or [])[:6] or [
        "The app could not complete structured model analysis, so this fallback is based on available inputs only."
    ]
    return DebuggerAnalysis(
        detected_language=detected_language or "Unknown",
        detected_framework=detected_framework or "Unknown",
        bug_type="Unknown runtime issue",
        issue_summary="The analyzer could not produce structured JSON.",
        root_cause=(
            "The page is still working, but the model response could not be parsed or "
            "validated. Review the raw response below, then try again with a shorter "
            "failure log or more focused repository context."
        ),
        suspected_location=SuspectedLocation(file="Unknown", function="Unknown"),
        evidence_used=evidence_used,
        recommended_fix=FixOption(
            title="Retry with tighter context",
            explanation="Retry the analysis with the most relevant error frames and nearby code.",
            tradeoff="This keeps the next attempt focused instead of guessing from weak evidence.",
            patch_diff="",
        ),
        safest_fix=FixOption(
            title="Reproduce locally first",
            explanation="Run the failing command or test locally and capture the shortest reproducible log.",
            tradeoff="It takes an extra step but produces a more trustworthy diagnosis.",
            patch_diff="",
        ),
        alternative_fix=FixOption(
            title="Add manual context",
            explanation="Paste the specific file, function, component, or config mentioned by the failure.",
            tradeoff="Manual context is slower, but it works when repo access is unavailable.",
            patch_diff="",
        ),
        confidence=confidence,
        confidence_label=_coerce_confidence_label("", confidence),
        confidence_reason=_default_confidence_reason(confidence),
        regression_test="Once the issue is identified, add one failing test or validation command that reproduces the failure before applying the fix.",
        parsed=False,
        raw_response=raw_response,
        fallback_reason=reason,
        source="fallback",
    )


def _required_string(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value.strip():
        if default:
            return default
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_or_unknown(value: Any, fallback: str = "Unknown") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if fallback and fallback != "Unknown":
        return fallback
    return "Unknown"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _fix_from_payload(
    value: Any,
    fallback_title: str,
    fallback_explanation: str = "",
    fallback_patch: str = "",
) -> FixOption:
    if isinstance(value, dict):
        return FixOption(
            title=_string_or_unknown(value.get("title"), fallback_title),
            explanation=_string_or_unknown(value.get("explanation"), fallback_explanation or "No explanation provided."),
            tradeoff=_string_or_unknown(value.get("tradeoff"), "Tradeoff not specified."),
            patch_diff=value.get("patch_diff", "") if isinstance(value.get("patch_diff", ""), str) else "",
        )
    if fallback_explanation:
        return FixOption(
            title=fallback_title,
            explanation=fallback_explanation,
            tradeoff="Fallback fix option derived from the available model output.",
            patch_diff=fallback_patch if isinstance(fallback_patch, str) else "",
        )
    raise ValueError(f"{fallback_title} must be an object")


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number") from exc
    return max(0.0, min(1.0, confidence))


def _coerce_confidence_label(value: Any, confidence: float) -> str:
    if isinstance(value, str) and value.strip() in {
        "High confidence",
        "Medium confidence",
        "Low confidence",
    }:
        return value.strip()
    if confidence >= 0.75:
        return "High confidence"
    if confidence >= 0.45:
        return "Medium confidence"
    return "Low confidence"


def _default_confidence_reason(confidence: float) -> str:
    if confidence >= 0.75:
        return "The error log and repository context point to the same likely cause."
    if confidence >= 0.45:
        return "There is useful signal, but more focused context would improve certainty."
    return "Evidence is limited, so treat this as a starting point rather than a final fix."


def _fix_as_dict(fix: FixOption) -> dict[str, str]:
    return {
        "title": fix.title,
        "explanation": fix.explanation,
        "tradeoff": fix.tradeoff,
        "patch_diff": fix.patch_diff,
    }


def _is_demo_payload(error_log: str, code_context: str) -> bool:
    return _normalize(error_log) == _normalize(DEMO_ERROR_LOG) and _normalize(
        code_context
    ) == _normalize(DEMO_CODE_CONTEXT)


def _normalize(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines())
