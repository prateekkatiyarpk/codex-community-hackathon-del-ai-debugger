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
MAX_SNIPPETS = 6
SNIPPET_RADIUS = 18


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
    candidates = []

    for path in iter_source_files(root):
        relative_path = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        score, reasons = score_file(relative_path, content, clues)
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
        candidates.append(snippet)

    return sorted(candidates, key=lambda item: item.score, reverse=True)[:MAX_SNIPPETS]


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


def score_file(relative_path: str, content: str, clues: FailureClues) -> tuple[int, list[str]]:
    path_lower = relative_path.lower()
    basename = Path(relative_path).name
    content_lower = content.lower()
    score = 0
    reasons: list[str] = []

    for clue_file in clues.file_names:
        clue_lower = clue_file.lower()
        if clue_lower == basename.lower() or path_lower.endswith(clue_lower):
            score += 120
            reasons.append(f"filename match: {clue_file}")
        elif clue_lower in path_lower:
            score += 65
            reasons.append(f"path match: {clue_file}")

    for template in clues.template_names:
        if template.lower() in path_lower or Path(template).name.lower() == basename.lower():
            score += 130
            reasons.append(f"template match: {template}")

    if (
        basename.lower() in COMMON_RELATED_NAMES
        or "/templates/" in path_lower
        or path_lower.startswith("templates/")
        or "/test" in path_lower
        or "/spec" in path_lower
    ):
        score += 18
        reasons.append("common framework file")

    for term in clues.module_terms:
        term_lower = term.lower()
        if term_lower in path_lower:
            score += 18
            reasons.append(f"module path match: {term}")

    for symbol in clues.symbols:
        symbol_lower = symbol.lower()
        if re.search(rf"\b{re.escape(symbol_lower)}\b", content_lower):
            score += 22
            reasons.append(f"symbol match: {symbol}")

    for test_name in clues.test_names:
        if test_name.lower() in content_lower or test_name.lower() in path_lower:
            score += 30
            reasons.append(f"test match: {test_name}")

    for package in clues.package_terms:
        if package.lower() in content_lower or package.lower() in path_lower:
            score += 20
            reasons.append(f"package match: {package}")

    if clues.exception_type and clues.exception_type.lower() in content_lower:
        score += 15
        reasons.append(f"exception mention: {clues.exception_type}")

    return score, reasons


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
