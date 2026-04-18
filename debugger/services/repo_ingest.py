from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from debugger.services.language_detect import LanguageProfile, detect_language_profile
from debugger.services.repo_search import CodeSnippet, discover_repo_context, render_snippets_context


MAX_ZIP_BYTES = 30 * 1024 * 1024
MAX_UNZIPPED_BYTES = 60 * 1024 * 1024
MAX_ZIP_MEMBERS = 2500
CONTEXT_LIMIT = 60_000


@dataclass(frozen=True)
class RepositoryContext:
    source: str
    repo_label: str
    language_profile: LanguageProfile
    snippets: list[CodeSnippet]
    errors: list[str]
    combined_context: str

    @property
    def source_label(self) -> str:
        if self.source == "zip":
            return "ZIP upload"
        if self.source == "github":
            return "Public GitHub repo"
        return "Manual fallback"

    @property
    def has_repo_input(self) -> bool:
        return self.source in {"zip", "github"}

    @property
    def inspected_files(self) -> list[str]:
        return [snippet.file_path for snippet in self.snippets]

    @property
    def detected_language(self) -> str:
        return self.language_profile.badge_language

    @property
    def detected_framework(self) -> str:
        return self.language_profile.badge_framework


class RepoIngestError(Exception):
    pass


def build_repository_context(
    *,
    error_log: str,
    github_url: str = "",
    uploaded_zip=None,
    manual_context: str = "",
) -> RepositoryContext:
    errors: list[str] = []
    snippets: list[CodeSnippet] = []
    language_profile = detect_language_profile(None, f"{error_log}\n{manual_context}")
    source = "manual"
    repo_label = "Failure log and optional extra context"

    if uploaded_zip:
        source = "zip"
        repo_label = getattr(uploaded_zip, "name", "Uploaded ZIP")
        try:
            snippets, language_profile = _snippets_from_uploaded_zip(
                uploaded_zip,
                error_log,
                manual_context,
            )
        except RepoIngestError as exc:
            errors.append(str(exc))
    elif github_url.strip():
        source = "github"
        repo_label = github_url.strip()
        try:
            snippets, repo_label, language_profile = _snippets_from_github(
                github_url.strip(),
                error_log,
                manual_context,
            )
        except RepoIngestError as exc:
            errors.append(str(exc))

    if source == "manual" and not snippets:
        combined_context = manual_context.strip()
    else:
        combined_context = render_snippets_context(snippets, manual_context)
        if not combined_context:
            combined_context = manual_context.strip()

    return RepositoryContext(
        source=source,
        repo_label=repo_label,
        language_profile=language_profile,
        snippets=snippets,
        errors=errors,
        combined_context=combined_context[:CONTEXT_LIMIT],
    )


def validate_github_repo_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    owner, repo, _branch = _parse_github_url(value)
    return f"https://github.com/{owner}/{repo}"


def _snippets_from_uploaded_zip(
    uploaded_zip,
    error_log: str,
    manual_context: str,
) -> tuple[list[CodeSnippet], LanguageProfile]:
    if getattr(uploaded_zip, "size", 0) and uploaded_zip.size > MAX_ZIP_BYTES:
        raise RepoIngestError("ZIP upload is too large. Keep it under 30 MB for this demo.")

    try:
        uploaded_zip.seek(0)
    except (AttributeError, OSError):
        pass

    with tempfile.TemporaryDirectory(prefix="ai-debugger-zip-") as temp_dir:
        root = Path(temp_dir)
        try:
            _safe_extract_zip(uploaded_zip, root)
        except zipfile.BadZipFile as exc:
            raise RepoIngestError("That file is not a valid ZIP archive.") from exc
        return discover_repo_context(root, error_log), detect_language_profile(root, manual_context)


def _snippets_from_github(
    github_url: str,
    error_log: str,
    manual_context: str,
) -> tuple[list[CodeSnippet], str, LanguageProfile]:
    owner, repo, branch = _parse_github_url(github_url)
    branch = branch or _fetch_default_branch(owner, repo)
    label = f"{owner}/{repo}"

    with tempfile.TemporaryDirectory(prefix="ai-debugger-github-") as temp_dir:
        root = Path(temp_dir)
        archive_path = root / "repo.zip"
        _download_github_zip(owner, repo, branch, archive_path)
        extract_root = root / "repo"
        extract_root.mkdir()
        with archive_path.open("rb") as archive:
            _safe_extract_zip(archive, extract_root)
        return (
            discover_repo_context(extract_root, error_log),
            label,
            detect_language_profile(extract_root, manual_context),
        )


def _parse_github_url(url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise RepoIngestError("Enter a public GitHub repository URL like https://github.com/owner/repo.")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise RepoIngestError("GitHub URL must include both owner and repository name.")

    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    branch = ""
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        branch = "/".join(parts[3:])

    if not owner or not repo:
        raise RepoIngestError("GitHub URL must include both owner and repository name.")
    return owner, repo, branch


def _fetch_default_branch(owner: str, repo: str) -> str:
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        with _open_url(api_url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RepoIngestError("Could not read that public GitHub repo. Check the URL or try ZIP upload.") from exc

    branch = payload.get("default_branch")
    if not branch:
        raise RepoIngestError("Could not determine the GitHub repo's default branch.")
    return branch


def _download_github_zip(owner: str, repo: str, branch: str, archive_path: Path) -> None:
    quoted_branch = urllib.parse.quote(branch, safe="")
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{quoted_branch}"
    try:
        with _open_url(zip_url, timeout=20) as response, archive_path.open("wb") as target:
            total = 0
            while True:
                chunk = response.read(1024 * 128)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise RepoIngestError("GitHub repository archive is too large for this demo.")
                target.write(chunk)
    except RepoIngestError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise RepoIngestError("Could not download the public GitHub repo. Try ZIP upload instead.") from exc


def _open_url(url: str, timeout: int):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-debugger-demo",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _safe_extract_zip(file_obj: BinaryIO, destination: Path) -> None:
    destination = destination.resolve()
    total_uncompressed = 0
    member_count = 0

    with zipfile.ZipFile(file_obj) as archive:
        for member in archive.infolist():
            member_count += 1
            if member_count > MAX_ZIP_MEMBERS:
                raise RepoIngestError("ZIP archive has too many files for this demo.")
            if member.is_dir():
                continue

            total_uncompressed += member.file_size
            if total_uncompressed > MAX_UNZIPPED_BYTES:
                raise RepoIngestError("ZIP archive expands to more than 60 MB.")

            target_path = (destination / member.filename).resolve()
            try:
                target_path.relative_to(destination)
            except ValueError as exc:
                raise RepoIngestError("ZIP archive contains an unsafe file path.") from exc

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target_path.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 128)
                    if not chunk:
                        break
                    target.write(chunk)
