"""GitHub response posting: PR comments, inline review replies, thread resolution.

Every write goes through the `gh` CLI as a subprocess (argv list, never a
shell), so no token material is handled here. Reads go through the injected
GitHubClient (`gh`). Sibling worker modules are never imported at module
level; everything arrives by argument injection.

Comment bodies must start with interfaces.COMMENT_MARKER plus a job-id
metadata line (see build_comment_body), so the worker can recognize and
suppress its own output when scanning conversations.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

from .interfaces import COMMENT_MARKER

API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_PARENT_HOPS = 50

_COMMENT_URL_ID = re.compile(r"#issuecomment-(\d+)\s*$")


class ResponderError(Exception):
    """A GitHub read or write in this module failed or stayed ambiguous."""


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_comment_body(job_id: str, text: str) -> str:
    """Hidden marker + job-id metadata line, then the human-readable text."""
    return f"{COMMENT_MARKER} job_id={job_id} -->\n{text.rstrip()}\n"


def _run_gh(args: list[str], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run `gh <args>` and return (rc, stdout, stderr).

    rc 124 marks a timeout: the outcome is UNCERTAIN (the write may have
    landed), so callers verify by re-reading before any retry. Tests patch
    this function; nothing else here touches the network.
    """
    argv = ["gh", *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out, err = exc.stdout or "", exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err


def _write_temp(text: str) -> str:
    """Write UTF-8 scratch file for a --body-file / -F body=@file argument."""
    fd, path = tempfile.mkstemp(prefix="gha-worker-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except BaseException:
        _cleanup_temp(path)
        raise
    return path


def _cleanup_temp(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _require_marker(body_text: str) -> None:
    if not body_text.startswith(COMMENT_MARKER):
        raise ResponderError(
            "comment body must start with the gha-worker COMMENT_MARKER header "
            "(build it with build_comment_body)")


def post_pr_comment(gh, repo: str, number: int, body_text: str,
                    timeout: float = DEFAULT_TIMEOUT_SECONDS) -> int:
    """Post body_text as a comment on PR/issue `number`; return the comment id.

    The id is parsed from the stdout URL tail when gh prints one; otherwise
    the latest own marker comment is re-read via REST. A timeout (rc 124) is
    treated as uncertain and verified by re-read before any error is raised.
    """
    _require_marker(body_text)
    path = _write_temp(body_text)
    try:
        rc, out, err = _run_gh(
            ["pr", "comment", str(int(number)), "--repo", repo, "--body-file", path],
            timeout=timeout)
        if rc == 0:
            m = _COMMENT_URL_ID.search(out or "")
            if m:
                return int(m.group(1))
        if rc in (0, 124):
            found = _latest_own_comment_id(gh, repo, int(number), body_text)
            if found is not None:
                return found
        raise ResponderError(f"gh pr comment failed (rc={rc}): {_clip(err or out, 400)}")
    finally:
        _cleanup_temp(path)


def _latest_own_comment_id(gh, repo: str, number: int, body_text: str) -> int | None:
    """Re-read the PR/issue comments and return our newest matching one."""
    try:
        comments = gh.rest("GET", f"repos/{repo}/issues/{number}/comments",
                           paginate=True) or []
    except Exception:
        return None
    try:
        login = gh.current_login() or ""
    except Exception:
        login = ""
    first_line = body_text.split("\n", 1)[0]
    best, best_key = None, None
    for idx, c in enumerate(comments):
        if not isinstance(c, dict):
            continue
        body = str(c.get("body") or "")
        if COMMENT_MARKER not in body or first_line not in body:
            continue
        user = c.get("user")
        author = user.get("login") or "" if isinstance(user, dict) else ""
        if login and author and author != login:
            continue
        key = (str(c.get("created_at") or ""), idx)
        if best_key is None or key > best_key:
            best, best_key = c, key
    cid = (best or {}).get("id")
    return int(cid) if isinstance(cid, int) else None


def reply_inline(gh, repo: str, number: int, root_comment_numeric_id: int,
                 body_text: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> int:
    """Reply under a review thread's TOP-LEVEL root comment.

    `root_comment_numeric_id` must be the NUMERIC REST id of a top-level
    review comment — never a GraphQL node id, never a reply id. Use
    resolve_root() first when starting from a comment dict.
    """
    _require_marker(body_text)
    if (isinstance(root_comment_numeric_id, bool)
            or not isinstance(root_comment_numeric_id, int)
            or root_comment_numeric_id <= 0):
        raise ResponderError(
            "reply needs the numeric REST id of a top-level root comment "
            f"(got {root_comment_numeric_id!r}); resolve it with resolve_root first")
    path = _write_temp(body_text)
    try:
        rc, out, err = _run_gh([
            "api", "--method", "POST",
            f"repos/{repo}/pulls/{int(number)}/comments/{root_comment_numeric_id}/replies",
            "-H", f"X-GitHub-Api-Version: {API_VERSION}",
            "-F", f"body=@{path}",
        ], timeout=timeout)
        if rc != 0:
            raise ResponderError(f"gh api reply failed (rc={rc}): {_clip(err or out, 400)}")
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise ResponderError(f"reply response was not JSON: {exc}") from exc
        cid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(cid, int):
            raise ResponderError("reply response did not contain a numeric comment id")
        return cid
    finally:
        _cleanup_temp(path)


def resolve_root(gh, repo: str, number: int, comment_dict: dict) -> dict:
    """Walk in_reply_to_id links up to the top-level root review comment."""
    current = dict(comment_dict or {})
    hops = 0
    while current.get("in_reply_to_id"):
        parent_id = int(current["in_reply_to_id"])
        parent = gh.rest("GET", f"repos/{repo}/pulls/comments/{parent_id}")
        if not isinstance(parent, dict) or "id" not in parent:
            raise ResponderError(f"could not fetch parent review comment {parent_id}")
        current = parent
        hops += 1
        if hops > MAX_PARENT_HOPS:
            raise ResponderError("in_reply_to chain deeper than 50; refusing to walk further")
    return current


_RESOLVE_MUTATION = """mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}"""


def resolve_thread(gh, thread_graphql_id: str) -> dict:
    """Resolve a review thread by its GraphQL id; verify the result."""
    data = gh.graphql(_RESOLVE_MUTATION, {"threadId": thread_graphql_id}) or {}
    thread = ((data.get("resolveReviewThread") or {}).get("thread")) or {}
    if thread.get("isResolved") is not True:
        raise ResponderError(
            f"resolveReviewThread did not confirm resolution for {thread_graphql_id!r}")
    return thread


def can_resolve(thread: dict) -> bool:
    """Pure evidence check: unresolved, and the viewer may resolve it."""
    return bool(thread) and thread.get("isResolved") is False \
        and thread.get("viewerCanResolve") is True
