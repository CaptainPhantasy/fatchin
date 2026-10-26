"""GitHubClient — thin wrapper around the gh CLI (interfaces.GitHubClientProto).

Stdlib only. Every command is built as an argv list (never a shell string, so
untrusted repo names, titles, or query text are never interpolated into a
shell). All error text that could echo gh output is passed through redact_text
first; token material is never logged and never returned.

Runner contract
---------------
GitHubClient takes an injectable runner: a callable

    runner(argv, input=None) -> subprocess.CompletedProcess-like object

with .returncode, .stdout, .stderr. ``argv`` is the gh argument list WITHOUT
the binary name (e.g. ["api", "--method", "GET", "/user"]); the default runner
prepends the gh binary it resolves from /usr/local/bin/gh or
PATH. ``input`` is the text fed to gh's stdin (used for ``--input -`` JSON
bodies; None otherwise). Tests inject fakes and therefore never need gh
installed and never touch the network.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Protocol
from urllib.parse import quote

DEFAULT_GH_PATH = "gh"
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120.0

Runner = Callable[..., Any]

PR_VIEW_FIELDS = (
    "number,url,state,title,body,headRefOid,headRefName,baseRefName,"
    "baseRefOid,headRepository,headRepositoryOwner,isCrossRepository,"
    "maintainerCanModify"
)
ISSUE_VIEW_FIELDS = "number,url,state,title,body,updatedAt"
PR_CHECK_FIELDS = "name,state,bucket,link,workflow,event"
RUN_VIEW_FIELDS = "databaseId,headSha,event,status,conclusion,jobs,url"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GitHubError(Exception):
    """Base class for all gh failures. Carries the exit code and the
    REDACTED stderr gh produced (never raw token material)."""

    def __init__(self, message: str, *, exit_code: int | None = None,
                 stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class AuthError(GitHubError):
    """HTTP 401/403 or gh is not logged in."""


class NotFound(GitHubError):
    """HTTP 404."""


class ValidationError(GitHubError):
    """HTTP 400/422."""


class RateLimited(GitHubError):
    """Rate limited; retry_after_seconds set when gh surfaced Retry-After."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None,
                 **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class ServerError(GitHubError):
    """HTTP 5xx from GitHub."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Credential-shaped strings seen in gh stderr/output are cut down to their
# recognizable prefix plus *** before any text is logged or attached to an
# error. Over-redaction is acceptable; leaking is not.
_REDACT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(gh[pousr]_)\S+"), r"\1***"),
    (re.compile(r"(github_pat_)\S+"), r"\1***"),
    (re.compile(r"(sk-)\S+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 ***"),
)


def redact_text(text: str) -> str:
    """Replace credential-shaped strings (any GitHub token prefix, sk-,
    bearer ...) with a starred-out prefix. Safe to call on any text."""
    result = text or ""
    for pattern, replacement in _REDACT_RULES:
        result = pattern.sub(replacement, result)
    return result


# ---------------------------------------------------------------------------
# Failure classification (from exit code + stderr, since gh prints the HTTP
# status in stderr)
# ---------------------------------------------------------------------------

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_RETRY_AFTER_RE = re.compile(r"(?i)retry-after:\s*(\d+)")


def classify_gh_failure(exit_code: int, stderr: str) -> GitHubError:
    """Map a failed gh invocation to the error taxonomy.

    Order matters: rate-limit responses arrive as HTTP 403 with rate-limit
    wording (and sometimes Retry-After), and must surface as RateLimited,
    not AuthError.
    """
    raw = stderr or ""
    red = redact_text(raw)
    lowered = raw.lower()

    retry = _RETRY_AFTER_RE.search(raw)
    if retry or "rate limit" in lowered or "abuse detection" in lowered:
        seconds = int(retry.group(1)) if retry else None
        suffix = f" retry after {seconds}s" if seconds is not None else ""
        return RateLimited(
            f"GitHub rate limit hit ({red}).{suffix}",
            retry_after_seconds=seconds, exit_code=exit_code, stderr=red)

    status_match = _HTTP_STATUS_RE.search(raw)
    status = int(status_match.group(1)) if status_match else None
    if status in (401, 403):
        return AuthError(f"GitHub auth failed ({red})", exit_code=exit_code, stderr=red)
    if status == 404:
        return NotFound(f"Not found ({red})", exit_code=exit_code, stderr=red)
    if status in (400, 422):
        return ValidationError(f"Request rejected ({red})", exit_code=exit_code, stderr=red)
    if status is not None and 500 <= status <= 599:
        return ServerError(f"GitHub server error ({red})", exit_code=exit_code, stderr=red)
    if "gh auth login" in lowered:
        return AuthError(f"gh is not authenticated ({red})", exit_code=exit_code, stderr=red)
    return GitHubError(
        f"gh failed with exit code {exit_code}: {red}",
        exit_code=exit_code, stderr=red)


# ---------------------------------------------------------------------------
# gh discovery + default runner
# ---------------------------------------------------------------------------

def find_gh() -> str:
    """Locate the gh binary without touching the network or requiring it to
    exist at import time. Raises GitHubError when absent."""
    if os.path.isfile(DEFAULT_GH_PATH) and os.access(DEFAULT_GH_PATH, os.X_OK):
        return DEFAULT_GH_PATH
    found = shutil.which("gh")
    if found:
        return found
    raise GitHubError(
        f"gh CLI not found: looked for {DEFAULT_GH_PATH} and for 'gh' on PATH")


def build_env(host: str) -> dict[str, str]:
    """Subprocess env for gh: a copy of os.environ with GH_HOST pinned to the
    configured host. Never logged."""
    env = dict(os.environ)
    env["GH_HOST"] = host
    return env


class _LoggerProto(Protocol):
    def info(self, message: str) -> None: ...


class GitHubClient:
    """implements interfaces.GitHubClientProto"""

    def __init__(self, host: str = "github.com", api_version: str = "2022-11-28",
                 runner: Runner | None = None, logger: _LoggerProto | None = None) -> None:
        self.host = host
        self.api_version = api_version
        self._env = build_env(host)
        # Injected runner: no gh lookup happens at all (tests need no gh).
        # None: resolve lazily on first use via the default runner.
        self._runner = runner
        self._logger = logger

    # -- plumbing -----------------------------------------------------------

    @property
    def runner(self) -> Runner:
        return self._runner if self._runner is not None else self._default_run

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        safe = redact_text(message)
        info = getattr(self._logger, "info", None)
        if callable(info):
            info(safe)
        elif callable(self._logger):
            self._logger(safe)

    def _default_run(self, argv: list[str], input: str | None = None) -> Any:
        gh = find_gh()
        cmd = [gh, *argv]
        self._log(f"$ {' '.join(cmd[:2])} ...")
        try:
            return subprocess.run(
                cmd, input=input, capture_output=True, text=True,
                env=self._env, timeout=DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(
                f"gh timed out after {DEFAULT_SUBPROCESS_TIMEOUT_SECONDS:.0f}s "
                f"running {argv[0]!r}") from exc
        except OSError as exc:
            raise GitHubError(f"could not execute gh ({gh}): {exc}") from exc

    def _run(self, argv: list[str], input: str | None = None) -> Any:
        proc = self.runner(argv, input)
        returncode = getattr(proc, "returncode", None)
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        if returncode != 0:
            raise classify_gh_failure(returncode, stderr)
        return stdout

    def _run_json(self, argv: list[str], context: str) -> Any:
        stdout = self._run(argv)
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            excerpt = redact_text(stdout[:200])
            raise GitHubError(
                f"gh returned non-JSON output for {context}: {excerpt!r}") from exc

    # -- core REST / GraphQL ------------------------------------------------

    def rest(self, method: str, path: str, body: dict | None = None,
             paginate: bool = False) -> Any:
        """One REST call. Always passes an explicit --method (GET for reads).
        Write bodies go to gh's stdin as JSON via --input -. With paginate=True
        gh runs --paginate --slurp, whose stdout is a JSON array of pages; the
        page arrays are flattened into one list."""
        method = method.upper()
        argv = ["api", "--method", method,
                "-H", f"X-GitHub-Api-Version: {self.api_version}"]
        payload = None
        if body is not None:
            if method in ("GET", "HEAD"):
                # Query parameters, never a request body: gh forbids
                # --paginate with --input, and GET bodies are not a REST
                # concept anyway.
                sep = "&" if "?" in path else "?"
                pairs = "&".join(
                    f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
                    for k, v in body.items())
                path = f"{path}{sep}{pairs}"
            else:
                payload = json.dumps(body)
                argv += ["-H", "Content-Type: application/json", "--input", "-"]
        if paginate:
            argv += ["--paginate", "--slurp"]
        argv.append(path)
        self._log(f"gh api {method} {path}" + (" (paginated)" if paginate else ""))
        stdout = self._run(argv, input=payload)
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            excerpt = redact_text(stdout[:200])
            raise GitHubError(
                f"gh api returned non-JSON output for {path}: {excerpt!r}") from exc
        if not paginate:
            return parsed
        if not isinstance(parsed, list):
            raise GitHubError(
                f"gh api --slurp returned a non-array payload for {path}")
        flat: list[Any] = []
        for page in parsed:
            if isinstance(page, list):
                flat.extend(page)
            else:
                flat.append(page)
        return flat

    def graphql(self, query: str, variables: dict | None = None,
                paginate_path: str | None = None) -> Any:
        """One GraphQL call. Variables: str -> -f name=value, int/bool ->
        -F name=value (gh types them), anything else -> -f with JSON encoding.
        With paginate_path, gh pages via --paginate --slurp and the lists found
        under data.<paginate_path> are concatenated into one merged response."""
        argv = ["api", "graphql", "-f", f"query={query}"]
        for name, value in (variables or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                argv += ["-F", f"{name}={'true' if value else 'false'}"]
            elif isinstance(value, int):
                argv += ["-F", f"{name}={value}"]
            elif isinstance(value, str):
                argv += ["-f", f"{name}={value}"]
            else:
                argv += ["-f", f"{name}={json.dumps(value)}"]
        if paginate_path:
            argv += ["--paginate", "--slurp"]
        self._log("gh api graphql")
        stdout = self._run(argv)
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            excerpt = redact_text(stdout[:200])
            raise GitHubError(
                f"gh api graphql returned non-JSON output: {excerpt!r}") from exc
        if paginate_path:
            # gh --paginate --slurp yields an array of full envelopes.
            if not isinstance(parsed, list):
                raise GitHubError("gh api graphql --slurp returned a non-array payload")
            merged: list[Any] = []
            for page in parsed:
                page_data = page.get("data", {}) if isinstance(page, dict) else {}
                node: Any = page_data
                for part in paginate_path.split("."):
                    node = node.get(part, {}) if isinstance(node, dict) else {}
                if isinstance(node, list):
                    merged.extend(node)
            nested: dict = {"data": {}}
            cursor = nested["data"]
            parts = paginate_path.split(".")
            for part in parts[:-1]:
                cursor[part] = {}
                cursor = cursor[part]
            cursor[parts[-1]] = merged
            return nested["data"]
        # Plain call: return the unwrapped data object; GraphQL-level errors
        # are failures, not empty results.
        if not isinstance(parsed, dict):
            raise GitHubError("gh api graphql returned a non-object payload")
        if parsed.get("errors"):
            excerpt = redact_text(json.dumps(parsed["errors"], default=str)[:300])
            raise GitHubError(f"GraphQL errors: {excerpt}")
        return parsed.get("data")

    # -- typed views ----------------------------------------------------------

    def pr_view(self, repo: str, number: int) -> dict:
        return self._run_json(
            ["pr", "view", str(number), "--repo", repo, "--json", PR_VIEW_FIELDS],
            context=f"pr view {repo}#{number}")

    def issue_view(self, repo: str, number: int) -> dict:
        return self._run_json(
            ["issue", "view", str(number), "--repo", repo, "--json", ISSUE_VIEW_FIELDS],
            context=f"issue view {repo}#{number}")

    def pr_checks(self, repo: str, number: int) -> list[dict]:
        """Commit status/check runs for a PR. gh exits 8 when checks are still
        pending; that is a successful data return (stdout is parsed anyway)."""
        argv = ["pr", "checks", str(number), "--repo", repo,
                "--json", PR_CHECK_FIELDS]
        proc = self.runner(argv, None)
        if proc.returncode not in (0, 8):
            raise classify_gh_failure(proc.returncode, getattr(proc, "stderr", "") or "")
        try:
            return json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            excerpt = redact_text((proc.stdout or "")[:200])
            raise GitHubError(
                f"gh pr checks returned non-JSON output for {repo}#{number}: "
                f"{excerpt!r}") from exc

    def run_view(self, repo: str, run_id: int) -> dict:
        return self._run_json(
            ["run", "view", str(run_id), "--repo", repo, "--json", RUN_VIEW_FIELDS],
            context=f"run view {repo}#{run_id}")

    # -- identity -------------------------------------------------------------

    def current_login(self) -> str:
        data = self.rest("GET", "/user")
        return str(data.get("login", ""))

    def current_user_id(self) -> int:
        """GitHub numeric user id as an exact int. GitHub ids already exceed
        2^31 and will exceed 2^53; Python ints are exact, so the value is
        never round-tripped through a float."""
        data = self.rest("GET", "/user")
        return coerce_github_id(data.get("id"))


def coerce_github_id(value: Any) -> int:
    """Accept only exact representations of the id gh printed: a JSON int
    (json.loads is exact at any magnitude) or a digit string. Floats are
    rejected outright — float(2**53 + 1) silently rounds, and GitHub ids will
    exceed 2^53; the value is never round-tripped through one."""
    if isinstance(value, bool):
        raise GitHubError(f"GitHub id is not an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    raise GitHubError(f"GitHub id is not an integer: {redact_text(repr(value))}")
