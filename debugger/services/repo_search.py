from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from debugger.services.traceback_parse import FailureClues, parse_failure_clues


ALLOWED_EXTENSIONS = {
    ".py",
    ".html",
    ".jinja",
    ".jinja2",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rb",
    ".php",
    ".rs",
    ".css",
    ".scss",
    ".vue",
    ".svelte",
    ".txt",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".xml",
    ".gradle",
    ".mod",
}
CONFIG_FILE_NAMES = {
    "gemfile",
    "dockerfile",
    "makefile",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "staticfiles",
    "dist",
    "build",
}
COMMON_RELATED_NAMES = {
    "views.py",
    "models.py",
    "urls.py",
    "tests.py",
    "forms.py",
    "admin.py",
    "settings.py",
    "serializers.py",
    "package.json",
    "tsconfig.json",
    "pom.xml",
    "build.gradle",
    "go.mod",
    "gemfile",
    "composer.json",
    "cargo.toml",
}
MAX_FILE_BYTES = 220_000
MAX_FILES_TO_SCAN = 900
MAX_SNIPPETS = 4
SNIPPET_RADIUS = 18
GENERIC_SYMBOLS = {
    "args",
    "kwargs",
    "render",
    "request",
    "response",
    "reverse",
    "get_response",
    "_reverse_with_prefix",
    "<module>",
}


@dataclass(frozen=True)
class CodeSnippet:
    file_path: str
    start_line: int
    end_line: int
    content: str
    score: int
    reason: str

    @property
    def preview(self) -> str:
        lines = self.content.strip().splitlines()
        return "\n".join(lines[:8])


def discover_repo_context(root: Path, traceback_text: str) -> list[CodeSnippet]:
    clues = parse_failure_clues(traceback_text)
    direct_candidates: list[CodeSnippet] = []
    support_candidates: list[CodeSnippet] = []

    for path in iter_source_files(root):
        relative_path = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        score, reasons, anchor_hits = score_file(relative_path, content, clues)
        if score <= 0:
            continue

        line_number = find_best_line(relative_path, content, clues)
        snippet = extract_snippet(
            relative_path=relative_path,
            content=content,
            line_number=line_number,
            score=score,
            reason=", ".join(reasons[:3]),
        )
        if anchor_hits > 0:
            direct_candidates.append(snippet)
        else:
            support_candidates.append(snippet)

    direct_candidates = sorted(direct_candidates, key=lambda item: item.score, reverse=True)
    support_candidates = sorted(support_candidates, key=lambda item: item.score, reverse=True)

    selected = direct_candidates[:MAX_SNIPPETS]
    remaining = MAX_SNIPPETS - len(selected)
    if remaining <= 0:
        return selected

    if len(direct_candidates) <= 1:
        max_support = min(2, remaining)
    elif len(direct_candidates) == 2:
        support_candidates = [snippet for snippet in support_candidates if snippet.score >= 60]
        max_support = min(1, remaining)
    else:
        max_support = 0

    return selected + support_candidates[:max_support]


def parse_traceback_clues(traceback_text: str) -> FailureClues:
    return parse_failure_clues(traceback_text)


def iter_source_files(root: Path):
    scanned = 0
    for path in root.rglob("*"):
        if scanned >= MAX_FILES_TO_SCAN:
            return
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        if path.suffix.lower() not in ALLOWED_EXTENSIONS and path.name.lower() not in CONFIG_FILE_NAMES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        scanned += 1
        yield path


def score_file(relative_path: str, content: str, clues: FailureClues) -> tuple[int, list[str], int]:
    path_lower = relative_path.lower()
    basename = Path(relative_path).name
    content_lower = content.lower()
    path_parts = {part.lower() for part in Path(relative_path).parts}
    is_template_file = "/templates/" in path_lower or path_lower.startswith("templates/")
    is_test_file = "/test" in path_lower or "/spec" in path_lower or basename.lower() == "tests.py"
    score = 0
    reasons: list[str] = []
    anchor_hits = 0
    support_hits = 0

    for clue_file in clues.file_names:
        clue_lower = clue_file.lower()
        if clue_lower == basename.lower() or path_lower.endswith(clue_lower):
            score += 140
            anchor_hits += 1
            _append_reason(reasons, f"traceback file match: {clue_file}")
        elif clue_lower in path_lower:
            score += 85
            anchor_hits += 1
            _append_reason(reasons, f"traceback path match: {clue_file}")

    for key in (relative_path, basename):
        if key in clues.line_numbers:
            score += 60
            anchor_hits += 1
            _append_reason(reasons, f"traceback line match: {clues.line_numbers[key]}")
            break

    for template in clues.template_names:
        if template.lower() in path_lower or Path(template).name.lower() == basename.lower():
            score += 145
            anchor_hits += 1
            _append_reason(reasons, f"template name match: {template}")

    if basename.lower() in COMMON_RELATED_NAMES:
        score += 8
        support_hits += 1

    if is_template_file:
        score += 14
        support_hits += 1
        _append_reason(reasons, "template candidate")

    if is_test_file and clues.test_names:
        score += 18
        support_hits += 1
        _append_reason(reasons, "related test file")

    for term in clues.module_terms:
        term_lower = term.lower()
        if term_lower in path_parts:
            score += 16
            support_hits += 1
            _append_reason(reasons, f"module path match: {term}")

    for symbol in clues.symbols:
        symbol_lower = symbol.lower()
        if symbol_lower in GENERIC_SYMBOLS:
            continue
        if re.search(rf"\b{re.escape(symbol_lower)}\b", content_lower):
            if is_test_file and not clues.test_names:
                score += 10
                support_hits += 1
                _append_reason(reasons, f"helper referenced in tests: {symbol}")
            else:
                score += 28
                anchor_hits += 1
                _append_reason(reasons, f"contains symbol: {symbol}")

    for test_name in clues.test_names:
        if test_name.lower() in content_lower or test_name.lower() in path_lower:
            score += 42
            anchor_hits += 1
            _append_reason(reasons, f"test match: {test_name}")

    for package in clues.package_terms:
        if package.lower() in content_lower or package.lower() in path_lower:
            score += 18
            support_hits += 1
            _append_reason(reasons, f"package match: {package}")

    if clues.exception_type and clues.exception_type.lower() in content_lower:
        score += 8
        support_hits += 1
        _append_reason(reasons, f"exception mention: {clues.exception_type}")

    exception_lower = clues.exception_type.lower()
    if exception_lower in {"noreversematch", "templateerror", "templatedoesnotexist"}:
        if basename.lower() == "urls.py":
            score += 26
            support_hits += 1
            _append_reason(reasons, "URL config candidate")
        if is_template_file and clues.template_names:
            score += 26
            support_hits += 1
            _append_reason(reasons, "template candidate for routing failure")
        if "{% url" in content_lower or "reverse(" in content_lower:
            score += 24
            support_hits += 1
            _append_reason(reasons, "contains reverse() / {% url %}")

    if exception_lower in {"improperlyconfigured", "operationalerror", "programmingerror"}:
        if basename.lower() in {"settings.py", "models.py"} or "/migrations/" in path_lower:
            score += 35
            support_hits += 1
            _append_reason(reasons, "framework configuration candidate")

    if is_test_file and not clues.test_names and anchor_hits == 0:
        return 0, [], 0

    if anchor_hits == 0 and score < 40:
        return 0, [], 0

    if is_test_file and not clues.test_names and not any(reason.startswith("traceback") for reason in reasons):
        score -= 26

    if anchor_hits == 0 and score < 30:
        return 0, [], 0

    score += min(anchor_hits, 3) * 10 + min(support_hits, 2) * 4

    return score, reasons, anchor_hits


def find_best_line(relative_path: str, content: str, clues: FailureClues) -> int:
    basename = Path(relative_path).name
    for key in (relative_path, basename):
        if key in clues.line_numbers:
            return clues.line_numbers[key]

    lines = content.splitlines()
    for index, line in enumerate(lines, start=1):
        lowered = line.lower()
        if any(symbol.lower() in lowered for symbol in clues.symbols):
            return index
        if clues.exception_type and clues.exception_type.lower() in lowered:
            return index
    return 1


def extract_snippet(
    relative_path: str,
    content: str,
    line_number: int,
    score: int,
    reason: str,
) -> CodeSnippet:
    lines = content.splitlines()
    if not lines:
        return CodeSnippet(relative_path, 1, 1, "", score, reason)

    safe_line = max(1, min(line_number, len(lines)))
    start = max(1, safe_line - SNIPPET_RADIUS)
    end = min(len(lines), safe_line + SNIPPET_RADIUS)
    snippet_lines = []
    for number in range(start, end + 1):
        snippet_lines.append(f"{number:>4}: {lines[number - 1]}")

    return CodeSnippet(
        file_path=relative_path,
        start_line=start,
        end_line=end,
        content="\n".join(snippet_lines),
        score=score,
        reason=reason or "Relevant repository file",
    )


def render_snippets_context(snippets: list[CodeSnippet], manual_context: str = "") -> str:
    sections: list[str] = []
    if snippets:
        sections.append("Auto-discovered repository context:")
        for snippet in snippets:
            sections.append(
                "\n".join(
                    [
                        f"--- {snippet.file_path}:{snippet.start_line}-{snippet.end_line}",
                        f"Reason: {snippet.reason}",
                        "```",
                        snippet.content,
                        "```",
                    ]
                )
            )

    manual_context = manual_context.strip()
    if manual_context:
        sections.append(
            "\n".join(
                [
                    "Optional extra context from user:",
                    "```",
                    manual_context,
                    "```",
                ]
            )
        )

    return "\n\n".join(sections).strip()


def _strip_repo_prefix(file_path: str) -> str:
    parts = [part for part in file_path.split("/") if part]
    for marker in ("site-packages", "src", "app"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return "/".join(parts[index + 1 :])
    if len(parts) > 3:
        return "/".join(parts[-3:])
    return "/".join(parts)


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)
