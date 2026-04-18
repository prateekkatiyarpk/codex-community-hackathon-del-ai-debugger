from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from debugger.demo import DEMO_ANALYSIS, DEMO_CODE_CONTEXT, DEMO_ERROR_LOG


SYSTEM_PROMPT = """You are a senior Django debugging assistant.
Prioritize traceback-first reasoning. Prefer one strong diagnosis over many vague guesses.
Suggest the smallest realistic fix. Include exactly one regression test idea.
Only provide patch_diff if the traceback and code context give enough evidence.
Return strict JSON only. Do not include markdown, commentary, or code fences."""

USER_PROMPT_TEMPLATE = """Analyze this Django/Python failure.

Return a JSON object that matches the required schema. Keep each field concise,
specific, and useful for a developer about to make a small fix.

Traceback or failing output:
```text
{error_log}
```

Optional code context:
```text
{code_context}
```"""

DEBUGGER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "debugger_analysis",
        "description": "A focused Django/Python debugging analysis.",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "issue_summary": {
                    "type": "string",
                    "description": "A short summary of the observed failure.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "The most likely root cause, grounded in the traceback.",
                },
                "suspected_location": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Best guess file path, module, or Unknown.",
                        },
                        "function": {
                            "type": "string",
                            "description": "Best guess function, class, method, template block, or area.",
                        },
                    },
                    "required": ["file", "function"],
                },
                "suggested_fix": {
                    "type": "string",
                    "description": "The smallest realistic fix to try first.",
                },
                "patch_diff": {
                    "type": "string",
                    "description": "Minimal unified diff, or an empty string if evidence is insufficient.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence from 0.0 to 1.0.",
                },
                "regression_test": {
                    "type": "string",
                    "description": "One concrete regression test suggestion.",
                },
            },
            "required": [
                "issue_summary",
                "root_cause",
                "suspected_location",
                "suggested_fix",
                "patch_diff",
                "confidence",
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
class DebuggerAnalysis:
    issue_summary: str
    root_cause: str
    suspected_location: SuspectedLocation
    suggested_fix: str
    patch_diff: str
    confidence: float
    regression_test: str
    parsed: bool = True
    raw_response: str = ""
    fallback_reason: str = ""
    source: str = "llm"

    @property
    def confidence_percent(self) -> int:
        return round(self.confidence * 100)

    def as_dict(self) -> dict[str, Any]:
        return {
            "issue_summary": self.issue_summary,
            "root_cause": self.root_cause,
            "suspected_location": {
                "file": self.suspected_location.file,
                "function": self.suspected_location.function,
            },
            "suggested_fix": self.suggested_fix,
            "patch_diff": self.patch_diff,
            "confidence": self.confidence,
            "regression_test": self.regression_test,
        }


class DebuggerServiceError(Exception):
    """Raised when the LLM transport cannot produce a usable response."""


def analyze_bug(error_log: str, code_context: str = "") -> DebuggerAnalysis:
    """Analyze a pasted failure and always return a renderable result."""
    error_log = error_log.strip()
    code_context = code_context.strip()

    if _is_demo_payload(error_log, code_context) and not os.environ.get("OPENAI_API_KEY"):
        return analysis_from_dict(DEMO_ANALYSIS, source="demo")

    try:
        raw_response = _call_openai(error_log=error_log, code_context=code_context)
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
        )

    try:
        return parse_model_response(raw_response)
    except ValueError as exc:
        return fallback_analysis(
            raw_response=raw_response,
            reason=f"The model returned output that was not valid for this app: {exc}",
        )


def _call_openai(error_log: str, code_context: str) -> str:
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
                    error_log=error_log,
                    code_context=code_context or "No code context provided.",
                ),
            },
        ],
    )

    content = response.choices[0].message.content
    if not content:
        raise DebuggerServiceError("The LLM returned an empty response.")
    return content


def parse_model_response(raw_response: str) -> DebuggerAnalysis:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse failed at character {exc.pos}") from exc

    return analysis_from_dict(payload)


def analysis_from_dict(payload: dict[str, Any], source: str = "llm") -> DebuggerAnalysis:
    if not isinstance(payload, dict):
        raise ValueError("top-level response must be a JSON object")

    suspected = payload.get("suspected_location")
    if not isinstance(suspected, dict):
        raise ValueError("suspected_location must be an object")

    issue_summary = _required_string(payload, "issue_summary")
    root_cause = _required_string(payload, "root_cause")
    suggested_fix = _required_string(payload, "suggested_fix")
    patch_diff = payload.get("patch_diff", "")
    if patch_diff is None:
        patch_diff = ""
    if not isinstance(patch_diff, str):
        raise ValueError("patch_diff must be a string")

    regression_test = _required_string(payload, "regression_test")
    confidence = _coerce_confidence(payload.get("confidence"))

    location = SuspectedLocation(
        file=_string_or_unknown(suspected.get("file")),
        function=_string_or_unknown(suspected.get("function")),
    )

    return DebuggerAnalysis(
        issue_summary=issue_summary,
        root_cause=root_cause,
        suspected_location=location,
        suggested_fix=suggested_fix,
        patch_diff=patch_diff.strip(),
        confidence=confidence,
        regression_test=regression_test,
        source=source,
    )


def fallback_analysis(raw_response: str, reason: str) -> DebuggerAnalysis:
    return DebuggerAnalysis(
        issue_summary="The analyzer could not produce structured JSON.",
        root_cause=(
            "The page is still working, but the LLM response could not be parsed or "
            "validated. Review the raw response below, then try again with a shorter "
            "traceback or more focused code context."
        ),
        suspected_location=SuspectedLocation(file="Unknown", function="Unknown"),
        suggested_fix="Retry the analysis with the most relevant traceback frames and nearby code.",
        patch_diff="",
        confidence=0.0,
        regression_test="Once the issue is identified, add one failing test that reproduces the traceback before applying the fix.",
        parsed=False,
        raw_response=raw_response,
        fallback_reason=reason,
        source="fallback",
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_or_unknown(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Unknown"


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number") from exc
    return max(0.0, min(1.0, confidence))


def _is_demo_payload(error_log: str, code_context: str) -> bool:
    return _normalize(error_log) == _normalize(DEMO_ERROR_LOG) and _normalize(
        code_context
    ) == _normalize(DEMO_CODE_CONTEXT)


def _normalize(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines())
