from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from debugger.services.repo_search import iter_source_files


@dataclass(frozen=True)
class LanguageProfile:
    language: str = "Unknown"
    framework: str = "Unknown"
    signals: tuple[str, ...] = ()
    is_python: bool = False

    @property
    def badge_language(self) -> str:
        return self.language or "Unknown"

    @property
    def badge_framework(self) -> str:
        return self.framework or "Unknown"


LANGUAGE_EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".go": "Go",
    ".rb": "Ruby",
    ".php": "PHP",
    ".rs": "Rust",
}


def detect_language_profile(root: Path | None, manual_context: str = "") -> LanguageProfile:
    scores: dict[str, int] = {}
    signals: list[str] = []
    file_names: set[str] = set()
    package_json = ""
    java_config_text = ""
    python_text = manual_context.lower()
    has_typescript_file = False

    if root and root.exists():
        for path in iter_source_files(root):
            name = path.name
            lower_name = name.lower()
            file_names.add(name)

            suffix = path.suffix.lower()
            if suffix in {".ts", ".tsx"}:
                has_typescript_file = True

            language = LANGUAGE_EXTENSIONS.get(suffix)
            if language:
                scores[language] = scores.get(language, 0) + 2

            if lower_name in {"pyproject.toml", "requirements.txt", "setup.py", "manage.py"}:
                scores["Python"] = scores.get("Python", 0) + 8
                signals.append(name)
            elif lower_name == "package.json":
                scores["JavaScript"] = scores.get("JavaScript", 0) + 8
                signals.append(name)
                try:
                    package_json = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    package_json = ""
            elif lower_name == "tsconfig.json":
                scores["TypeScript"] = scores.get("TypeScript", 0) + 8
                signals.append(name)
            elif lower_name in {"pom.xml", "build.gradle", "settings.gradle"}:
                scores["Java"] = scores.get("Java", 0) + 8
                signals.append(name)
                try:
                    java_config_text += "\n" + path.read_text(encoding="utf-8", errors="ignore")[:5000].lower()
                except OSError:
                    pass
            elif lower_name == "go.mod":
                scores["Go"] = scores.get("Go", 0) + 8
                signals.append(name)
            elif lower_name == "gemfile":
                scores["Ruby"] = scores.get("Ruby", 0) + 8
                signals.append(name)
            elif lower_name == "composer.json":
                scores["PHP"] = scores.get("PHP", 0) + 8
                signals.append(name)
            elif lower_name == "cargo.toml":
                scores["Rust"] = scores.get("Rust", 0) + 8
                signals.append(name)

            if suffix == ".py":
                try:
                    python_text += "\n" + path.read_text(encoding="utf-8", errors="ignore")[:5000].lower()
                except OSError:
                    pass

    if has_typescript_file and package_json:
        scores["TypeScript"] = scores.get("TypeScript", 0) + 8

    language = max(scores, key=scores.get) if scores else _detect_from_text(manual_context)
    framework = _detect_framework(language, file_names, package_json, python_text, java_config_text, manual_context)
    return LanguageProfile(
        language=language or "Unknown",
        framework=framework or "Unknown",
        signals=tuple(dict.fromkeys(signals[:8])),
        is_python=language == "Python",
    )


def _detect_from_text(text: str) -> str:
    lower = text.lower()
    if "traceback (most recent call last)" in lower or ".py" in lower:
        return "Python"
    if "npm" in lower or "node_modules" in lower or ".js:" in lower or ".ts:" in lower:
        return "JavaScript"
    if "nullpointerexception" in lower or ".java:" in lower:
        return "Java"
    if ".go:" in lower or "panic:" in lower:
        return "Go"
    if "gem " in lower or ".rb:" in lower:
        return "Ruby"
    if ".php" in lower:
        return "PHP"
    if "cargo" in lower or ".rs:" in lower:
        return "Rust"
    return "Unknown"


def _detect_framework(
    language: str,
    file_names: set[str],
    package_json: str,
    python_text: str,
    java_config_text: str,
    manual_context: str,
) -> str:
    text = f"{python_text}\n{java_config_text}\n{manual_context}".lower()

    if language == "Python":
        if "manage.py" in file_names or "django" in text:
            return "Django"
        if "fastapi" in text:
            return "FastAPI"
        if "flask" in text:
            return "Flask"
        return "Unknown"

    if language in {"JavaScript", "TypeScript"}:
        package_text = package_json.lower()
        try:
            parsed = json.loads(package_json) if package_json else {}
            deps = {
                **parsed.get("dependencies", {}),
                **parsed.get("devDependencies", {}),
            }
            package_text += " " + " ".join(deps)
        except json.JSONDecodeError:
            pass
        if "next" in package_text or "next.config" in " ".join(file_names).lower():
            return "Next.js"
        if "react" in package_text:
            return "React"
        if "express" in package_text:
            return "Express"
        return "Unknown"

    if language == "Java":
        if "spring" in text or "pom.xml" in file_names or "build.gradle" in file_names:
            return "Spring Boot" if "spring" in text else "Unknown"
    return "Unknown"
