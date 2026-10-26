"""Command phrase parsing and authorization for gha-worker (stdlib only).

PARSE RULES
-----------
parse_command scans a comment body line by line. Lines inside ``` code fences
are never commands (a ``` line toggles fence state). A command must appear as
the FIRST non-whitespace token(s) of a line: the prefix (default "/agent",
case-sensitive, configurable) followed by one of the verbs fix / implement /
status / stop, case-sensitive. A phrase mentioned mid-sentence ("please
/agent fix this") is not a command. The first recognized command wins; a
comment carries at most one.

"/agent fix" and "/agent implement" may carry one argument right after the
verb: "<owner/repo>#<num>" or "#<num>". Any other trailing words are ignored.
"/agent status" and "/agent stop" accept no argument.

Kind/target agreement: "fix" only applies to a PR target and "implement" only
to an issue target; a mismatch yields None. "status" and "stop" apply to both.

AUTHORIZATION (the choice documented per the brief)
---------------------------------------------------
This module uses the SEPARATE-HELPER option: ignore_own() and is_authorized()
are distinct functions. ignore_own() answers "is this the worker's own output
(by login, case-insensitive) — skip it entirely", while is_authorized()
answers "does this comment's ACTUAL author numeric id appear in the allowed
list". is_authorized never reads the body for @mentions (a quoted or flattered
mention of an allowed login never authorizes — the id is the only signal) and
returns False for the worker's own logins even if their id somehow appears in
the allowed list. Callers should call ignore_own() first and skip; the False
from is_authorized for an own login is defense in depth, not the ignore
signal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ghagent.interfaces import COMMENT_MARKER, JobKind

_VERB_KINDS: dict[str, JobKind] = {
    "fix": JobKind.FIX_PR,
    "implement": JobKind.IMPLEMENT_ISSUE,
    "status": JobKind.STATUS,
    "stop": JobKind.STOP,
}

_ARGUMENT_RE = re.compile(
    r"^(?:(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+))?#(?P<num>[0-9]+)$")

_PR_TARGETS = frozenset({"pr", "pull", "pull_request", "pullrequest"})


@dataclass
class CommandRequest:
    kind: JobKind
    repo_full_name: str | None   # None = same repo as the comment's target
    number: int
    raw: str


def normalize_target_kind(target_kind: str) -> str:
    """Accept 'pr' (also pull/pull_request) or 'issue', case-insensitive."""
    value = str(target_kind).strip().lower()
    if value in _PR_TARGETS:
        return "pr"
    if value == "issue":
        return "issue"
    raise ValueError(
        f"target_kind must be 'pr' or 'issue', got {target_kind!r}")


def parse_command(body_text: str, target_kind: str, target_number: int | None,
                  *, command_prefix: str = "/agent") -> CommandRequest | None:
    """Extract the first command phrase from a comment body, or None.

    target_kind is what the comment sits on ("pr" or "issue"); target_number
    is that PR/issue's number. A command without an explicit "#num" argument
    inherits target_number, so a comment on a numberless target cannot produce
    a command (returns None).
    """
    target = normalize_target_kind(target_kind)
    if not body_text:
        return None

    in_fence = False
    for raw_line in body_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        tokens = stripped.split()
        if len(tokens) < 2:
            continue
        if tokens[0] != command_prefix:      # case-sensitive, first token only
            continue
        kind = _VERB_KINDS.get(tokens[1])    # verb case-sensitive
        if kind is None:
            continue
        if kind is JobKind.FIX_PR and target != "pr":
            continue
        if kind is JobKind.IMPLEMENT_ISSUE and target != "issue":
            continue

        repo_full_name: str | None = None
        number: int | None = int(target_number) if target_number is not None else None
        if kind in {JobKind.FIX_PR, JobKind.IMPLEMENT_ISSUE} and len(tokens) >= 3:
            match = _ARGUMENT_RE.match(tokens[2])
            if match:
                if match.group("owner"):
                    repo_full_name = f"{match.group('owner')}/{match.group('name')}"
                number = int(match.group("num"))
        if number is None:
            continue
        return CommandRequest(kind=kind, repo_full_name=repo_full_name,
                              number=number, raw=stripped)
    return None


def is_own_comment(body_text: str) -> bool:
    """True when the body carries the worker's hidden COMMENT_MARKER."""
    return COMMENT_MARKER in (body_text or "")


def ignore_own(comment: dict, own_logins: set[str] | frozenset[str]) -> bool:
    """True when this comment is the worker's own output and must be skipped
    entirely, judged ONLY by the author login (case-insensitive) against
    own_logins. Body text is not consulted here; use is_own_comment() for the
    marker check."""
    user = comment.get("user") or {}
    login = str(user.get("login", "")).lower()
    return login in {str(l).lower() for l in own_logins}


def is_authorized(comment: dict, allowed_user_ids: list[int],
                  own_logins: set[str] | frozenset[str] = frozenset()) -> bool:
    """True only when the comment's ACTUAL author numeric id (user.id) is in
    allowed_user_ids. Display names, @mentions in the body, and bot-flipped
    login strings never authorize — the id is the whole test. The worker's own
    logins never authorize, even by id. See module docstring for the
    ignore_own()/is_authorized() split."""
    if ignore_own(comment, own_logins):
        return False
    user = comment.get("user") or {}
    raw_id = user.get("id")
    if isinstance(raw_id, bool):
        return False
    try:
        author_id = int(raw_id)  # exact for ints and integral floats
    except (TypeError, ValueError):
        return False
    return author_id in {int(a) for a in allowed_user_ids}


def dedup_key_for(kind: JobKind, repo: str, number: int,
                  head_sha_or_revision: str) -> str:
    """Same format Job.dedup_key documents: "kind:repo:number:revision" —
    kind is the JobKind value (e.g. "fix-pr"); the revision is the PR head
    sha for PR jobs or an issue revision (e.g. the commanding comment id) for
    issues."""
    kind_value = kind.value if isinstance(kind, JobKind) else str(kind)
    return f"{kind_value}:{repo}:{number}:{head_sha_or_revision}"
