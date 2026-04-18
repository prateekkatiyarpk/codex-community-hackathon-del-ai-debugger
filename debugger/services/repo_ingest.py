from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from debugger.services.language_detect import LanguageProfile, detect_language_profile
from debugger.services.repo_search import CodeSnippet, discover_repo_context, render_snippets_context


MAX_ZIP_BYTES = 30 * 1024 * 1024
MAX_UNZIPPED_BYTES = 60 * 1024 * 1024
MAX_ZIP_MEMBERS = 2500
CONTEXT_LIMIT = 60_000
IGNORED_TOP_LEVEL_NAMES = {".ds_store", "__macosx"}
GITHUB_API_ROOT = "https://api.github.com"


@dataclass(frozen=True)
class RepositoryWorkspace:
    source: str
    repo_label: str
    analysis_root: Path | None
    execution_root: Path | None
    errors: list[str]

    @property
    def has_repo_input(self) -> bool:
        return self.source in {"zip", "github"}


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
            return "GitHub repo"
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
    github_token: str = "",
    uploaded_zip=None,
    manual_context: str = "",
) -> RepositoryContext:
    with prepare_repository_workspace(
        github_url=github_url,
        github_token=github_token,
        uploaded_zip=uploaded_zip,
    ) as workspace:
        return build_repository_context_from_workspace(
            workspace,
            error_log=error_log,
            manual_context=manual_context,
        )


def build_repository_context_from_workspace(
    workspace: RepositoryWorkspace,
    *,
    error_log: str,
    manual_context: str = "",
) -> RepositoryContext:
    errors = list(workspace.errors)
    snippets: list[CodeSnippet] = []
    language_profile = detect_language_profile(None, f"{error_log}\n{manual_context}")

    if workspace.analysis_root and workspace.analysis_root.exists():
        if error_log.strip():
            snippets = discover_repo_context(workspace.analysis_root, error_log)
        language_profile = detect_language_profile(workspace.analysis_root, manual_context)

    if workspace.source == "manual" and not snippets:
        combined_context = manual_context.strip()
    else:
        combined_context = render_snippets_context(snippets, manual_context)
        if not combined_context:
            combined_context = manual_context.strip()

    return RepositoryContext(
        source=workspace.source,
        repo_label=workspace.repo_label,
        language_profile=language_profile,
        snippets=snippets,
        errors=errors,
        combined_context=combined_context[:CONTEXT_LIMIT],
    )


@contextmanager
def prepare_repository_workspace(
    *,
    github_url: str = "",
    github_token: str = "",
    uploaded_zip=None,
) -> Iterator[RepositoryWorkspace]:
    source = "manual"
    repo_label = "Failure log and optional extra context"
    analysis_root: Path | None = None
    execution_root: Path | None = None
    errors: list[str] = []
    temp_dir: tempfile.TemporaryDirectory[str] | None = None

    try:
        if uploaded_zip:
            source = "zip"
            repo_label = getattr(uploaded_zip, "name", "Uploaded ZIP")
            try:
                temp_dir = tempfile.TemporaryDirectory(prefix="ai-debugger-zip-")
                analysis_root = Path(temp_dir.name)
                _extract_uploaded_zip(uploaded_zip, analysis_root)
                execution_root = _preferred_execution_root(analysis_root)
            except RepoIngestError as exc:
                errors.append(str(exc))
                analysis_root = None
                execution_root = None
        elif github_url.strip():
            source = "github"
            repo_label = github_url.strip()
            try:
                temp_dir = tempfile.TemporaryDirectory(prefix="ai-debugger-github-")
                analysis_root, repo_label = _download_github_repo(
                    github_url.strip(),
                    Path(temp_dir.name),
                    github_token=github_token.strip(),
                )
                execution_root = _preferred_execution_root(analysis_root)
            except RepoIngestError as exc:
                errors.append(str(exc))
                analysis_root = None
                execution_root = None

        yield RepositoryWorkspace(
            source=source,
            repo_label=repo_label,
            analysis_root=analysis_root,
            execution_root=execution_root,
            errors=errors,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def validate_github_repo_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    owner, repo, _branch = _parse_github_url(value)
    return f"https://github.com/{owner}/{repo}"


def _extract_uploaded_zip(uploaded_zip, destination: Path) -> None:
    if getattr(uploaded_zip, "size", 0) and uploaded_zip.size > MAX_ZIP_BYTES:
        raise RepoIngestError("ZIP upload is too large. Keep it under 30 MB for this demo.")

    try:
        uploaded_zip.seek(0)
    except (AttributeError, OSError):
        pass

    try:
        _safe_extract_zip(uploaded_zip, destination)
    except zipfile.BadZipFile as exc:
        raise RepoIngestError("That file is not a valid ZIP archive.") from exc


def _download_github_repo(
    github_url: str,
    temp_root: Path,
    *,
    github_token: str = "",
) -> tuple[Path, str]:
    owner, repo, branch = _parse_github_url(github_url)
    branch = branch or _fetch_default_branch(owner, repo, github_token=github_token)
    repo_root = temp_root / "repo"
    repo_root.mkdir()

    archive_path = temp_root / "repo.zip"
    _download_github_zip(owner, repo, branch, archive_path, github_token=github_token)

    with archive_path.open("rb") as archive:
        _safe_extract_zip(archive, repo_root)

    return repo_root, f"{owner}/{repo}"


def _preferred_execution_root(root: Path) -> Path:
    current = root
    while True:
        children = [
            child
            for child in current.iterdir()
            if child.name.lower() not in IGNORED_TOP_LEVEL_NAMES
        ]
        dirs = [child for child in children if child.is_dir()]
        files = [child for child in children if child.is_file()]
        if len(dirs) == 1 and not files:
            current = dirs[0]
            continue
        return current


def _parse_github_url(url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise RepoIngestError("Enter a GitHub repository URL like https://github.com/owner/repo.")

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


def _fetch_default_branch(owner: str, repo: str, *, github_token: str = "") -> str:
    api_url = f"{GITHUB_API_ROOT}/repos/{owner}/{repo}"
    try:
        with _open_url(api_url, timeout=10, github_token=github_token) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RepoIngestError(_github_http_error_message(exc, github_token=github_token, action="read")) from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RepoIngestError("Could not reach GitHub. Check the URL or try ZIP upload.") from exc

    branch = payload.get("default_branch")
    if not branch:
        raise RepoIngestError("Could not determine the GitHub repo's default branch.")
    return branch


def _download_github_zip(
    owner: str,
    repo: str,
    branch: str,
    archive_path: Path,
    *,
    github_token: str = "",
) -> None:
    quoted_branch = urllib.parse.quote(branch, safe="")
    zip_url = f"{GITHUB_API_ROOT}/repos/{owner}/{repo}/zipball/{quoted_branch}"
    try:
        with _open_url(zip_url, timeout=20, github_token=github_token) as response, archive_path.open("wb") as target:
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
    except urllib.error.HTTPError as exc:
        raise RepoIngestError(_github_http_error_message(exc, github_token=github_token, action="download")) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise RepoIngestError("Could not download that GitHub repo archive. Try ZIP upload instead.") from exc


def _open_url(url: str, timeout: int, *, github_token: str = ""):
    request = _build_github_request(url, github_token=github_token)
    return urllib.request.urlopen(request, timeout=timeout)


def _build_github_request(url: str, *, github_token: str = ""):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-debugger-demo",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return urllib.request.Request(url, headers=headers)


def _github_http_error_message(exc: urllib.error.HTTPError, *, github_token: str, action: str) -> str:
    code = getattr(exc, "code", None)
    headers = getattr(exc, "headers", {}) or {}
    token_present = bool(github_token)
    action_phrase = "read that GitHub repo" if action == "read" else "download that GitHub repo archive"

    if code == 401:
        return "GitHub access token was rejected. Check the token value and confirm it can read this repo."

    if code == 403 and headers.get("X-RateLimit-Remaining") == "0":
        return "GitHub rate limit was reached while fetching this repo. Try again later, add a token, or use ZIP upload."

    if code == 403:
        if token_present:
            return "GitHub access token does not have permission to read this repo. Check the token scope or use ZIP upload."
        return "GitHub denied access to this repo. If it is private, add a GitHub access token or use ZIP upload."

    if code == 404:
        if token_present:
            return "Could not access that GitHub repo. Check the URL and confirm the token has read access."
        return "Could not read that GitHub repo. Check the URL. If it is private, add a GitHub access token or try ZIP upload."

    return f"Could not {action_phrase}. Try again later or use ZIP upload."


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
