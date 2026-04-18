from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FailureClues:
    file_names: set[str]
    line_numbers: dict[str, int]
    template_names: set[str]
    exception_type: str
    package_terms: set[str]
    module_terms: set[str]
    symbols: set[str]
    test_names: set[str]
    raw_error_tokens: set[str]


def parse_failure_clues(error_log: str) -> FailureClues:
    file_names: set[str] = set()
    line_numbers: dict[str, int] = {}
    package_terms: set[str] = set()
    module_terms: set[str] = set()
    symbols: set[str] = set()
    template_names: set[str] = set()
    test_names: set[str] = set()
    raw_error_tokens: set[str] = set()

    _parse_python_frames(error_log, file_names, line_numbers, module_terms, symbols)
    _parse_generic_file_lines(error_log, file_names, line_numbers, module_terms)
    _parse_test_nodeids(error_log, file_names, line_numbers, module_terms, test_names)
    _parse_templates_and_components(error_log, file_names, template_names)
    _parse_symbols_and_tests(error_log, symbols, test_names)
    _parse_packages(error_log, package_terms)

    exception_type = _extract_exception_type(error_log)
    if exception_type:
        raw_error_tokens.add(exception_type)

    return FailureClues(
        file_names={value for value in file_names if value},
        line_numbers=line_numbers,
        template_names={value for value in template_names if value},
        exception_type=exception_type,
        package_terms={value for value in package_terms if value},
        module_terms={value for value in module_terms if value},
        symbols={value for value in symbols if value and value != "<module>"},
        test_names={value for value in test_names if value},
        raw_error_tokens={value for value in raw_error_tokens if value},
    )


def fallback_evidence(clues: FailureClues, inspected_files: list[str], language: str, framework: str) -> list[str]:
    evidence: list[str] = []
    if clues.exception_type:
        evidence.append(f"Failure log includes {clues.exception_type}.")
    if clues.file_names:
        evidence.append("Log references files such as " + ", ".join(sorted(clues.file_names)[:3]) + ".")
    if inspected_files:
        evidence.append("Repository search inspected " + ", ".join(inspected_files[:3]) + ".")
    if language != "Unknown":
        evidence.append(f"Repository signals indicate {language}.")
    if framework != "Unknown":
        evidence.append(f"Framework signals indicate {framework}.")
    if not evidence:
        evidence.append("The diagnosis is based on the supplied failure log and any available context.")
    return evidence[:5]


def _parse_python_frames(
    error_log: str,
    file_names: set[str],
    line_numbers: dict[str, int],
    module_terms: set[str],
    symbols: set[str],
) -> None:
    for match in re.finditer(r'File "([^"]+)", line (\d+)(?:, in ([\w_<>]+))?', error_log):
        file_path = match.group(1).replace("\\", "/")
        line_number = int(match.group(2))
        symbol = match.group(3)
        _record_file(file_path, line_number, file_names, line_numbers, module_terms)
        if symbol:
            symbols.add(symbol)


def _parse_generic_file_lines(
    error_log: str,
    file_names: set[str],
    line_numbers: dict[str, int],
    module_terms: set[str],
) -> None:
    patterns = [
        r"((?:[\w.-]+/)*[\w.-]+\.(?:py|js|jsx|ts|tsx|java|go|rb|php|rs|html|css|json|yaml|yml|toml)):(\d+)",
        r"at .*?\(?((?:[\w.-]+/)*[\w.-]+\.(?:js|jsx|ts|tsx|java|go|rb|php|rs)):(\d+):\d+\)?",
        r"([A-Za-z0-9_.-]+\.(?:py|js|jsx|ts|tsx|java|go|rb|php|rs|html)) line (\d+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, error_log):
            _record_file(match.group(1), int(match.group(2)), file_names, line_numbers, module_terms)


def _parse_test_nodeids(
    error_log: str,
    file_names: set[str],
    line_numbers: dict[str, int],
    module_terms: set[str],
    test_names: set[str],
) -> None:
    pattern = r"((?:[\w.-]+/)*[\w.-]+\.(?:py|js|jsx|ts|tsx|java|go|rb|php|rs))::([A-Za-z0-9_]+)"
    for match in re.finditer(pattern, error_log):
        _record_file(match.group(1), 1, file_names, line_numbers, module_terms)
        test_names.add(match.group(2))


def _parse_templates_and_components(
    error_log: str,
    file_names: set[str],
    template_names: set[str],
) -> None:
    for match in re.finditer(r"['\"]([^'\"]+\.(?:html|txt|jinja|jinja2|jsx|tsx|vue|svelte))['\"]", error_log):
        template_name = match.group(1).replace("\\", "/")
        template_names.add(template_name)
        file_names.add(Path(template_name).name)


def _parse_symbols_and_tests(error_log: str, symbols: set[str], test_names: set[str]) -> None:
    symbol_patterns = [
        r"\bin ([A-Za-z_][A-Za-z0-9_<>]*)",
        r"Reverse for ['\"]([^'\"]+)['\"]",
        r"ReferenceError: ([A-Za-z_$][A-Za-z0-9_$]*)",
        r"NameError: name ['\"]([^'\"]+)['\"]",
        r"Cannot find module ['\"]([^'\"]+)['\"]",
        r"undefined method [`']([^`']+)",
        r"undefined function ([A-Za-z_][A-Za-z0-9_]*)",
    ]
    for pattern in symbol_patterns:
        for match in re.finditer(pattern, error_log):
            symbols.add(match.group(1))

    for match in re.finditer(r"\b(test_[A-Za-z0-9_]+|[A-Za-z0-9_]+Test|it\(['\"]([^'\"]+))", error_log):
        test_names.add(match.group(1))
        if len(match.groups()) > 1 and match.group(2):
            test_names.add(match.group(2))


def _parse_packages(error_log: str, package_terms: set[str]) -> None:
    package_patterns = [
        r"No module named ['\"]([^'\"]+)['\"]",
        r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]",
        r"Cannot find module ['\"]([^'\"]+)['\"]",
        r"package ([A-Za-z0-9_./-]+) is not in std",
        r"Could not find gem ['\"]([^'\"]+)['\"]",
    ]
    for pattern in package_patterns:
        for match in re.finditer(pattern, error_log):
            package_terms.add(match.group(1).split(".")[0])


def _extract_exception_type(error_log: str) -> str:
    patterns = [
        r"^([\w.]+(?:Error|Exception|DoesNotExist|NoReverseMatch|Warning))(?::|\s)",
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b",
        r"\b(NullPointerException|TypeError|ReferenceError|RuntimeException|AssertionError|ImportError)\b",
    ]
    for line in reversed(error_log.strip().splitlines()):
        stripped = line.strip()
        for pattern in patterns:
            match = re.search(pattern, stripped)
            if match:
                return match.group(1).split(".")[-1]
    return ""


def _record_file(
    file_path: str,
    line_number: int,
    file_names: set[str],
    line_numbers: dict[str, int],
    module_terms: set[str],
) -> None:
    normalized = _strip_repo_prefix(file_path.replace("\\", "/"))
    basename = Path(normalized).name
    file_names.update({basename, normalized})
    line_numbers[basename] = line_number
    line_numbers[normalized] = line_number

    for part in Path(normalized).parts:
        if part and part not in {".", ".."} and "." not in part:
            module_terms.add(part)


def _strip_repo_prefix(file_path: str) -> str:
    parts = [part for part in file_path.split("/") if part]
    for marker in ("site-packages", "node_modules", "src", "app"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return "/".join(parts[index + 1 :])
    if len(parts) > 4:
        return "/".join(parts[-4:])
    return "/".join(parts)
