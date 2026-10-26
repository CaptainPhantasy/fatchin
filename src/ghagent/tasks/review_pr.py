"""Review a pull request: explain it in plain words, draft one comment.

Profile "draft": the agent reads the pull request and writes the exact review
comment into .gha-drafts/review.md; the job stops at WAITING_ON_YOU. Nothing
reaches GitHub until the person chooses Post, and the only review the apply
step can create is a COMMENT — never an approval, never a change request.
"""
from __future__ import annotations

from .types import TaskType

DRAFT_FILE = ".gha-drafts/review.md"
NOTE_LIMIT = 500


def _clean_note(value: object) -> str:
    return " ".join(str(value or "").split())[:NOTE_LIMIT]


def build_instruction(context: dict) -> str:
    """Tell the agent what to find out, in words, and where to put it."""
    context = context or {}
    values = context.get("values") or {}
    repo = str(context.get("repo") or "this repository")
    number = context.get("number") or "?"
    note = _clean_note(values.get("note"))
    lines = [
        f"Task: review pull request #{number} in {repo} for a person who cannot "
        "read a diff.",
        "",
        "Find out two things:",
        "1. What this pull request changes, in plain words: what the software did "
        "before, what it does after, and who notices the difference. Name the "
        "files that carry the change and cite them as path:line.",
        "2. Whether the repository's checks pass: say which passed, which failed, "
        "and which had not finished. If you cannot read the checks, say so plainly "
        "instead of guessing.",
        "",
        "How to look. The checkout holds the base branch, not the pull request's "
        "own commits, so read the pull request itself:",
        f"- gh pr view {number} --repo {repo} --json title,body,files,commits",
        f"- gh pr diff {number} --repo {repo}",
        f"- gh pr checks {number} --repo {repo}",
        "- read_file and list_dir for the surrounding code in the checkout.",
        "If one of these commands cannot run, write down what you could not read "
        "and carry on with what you have.",
        "",
    ]
    if note:
        lines += [f"The person asks you to pay particular attention to: {note}", ""]
    lines += [
        "Where the answers go:",
        "- The plain-words explanation belongs in your final summary (the finish "
        "tool); the person reads that directly.",
        f"- {DRAFT_FILE} holds the exact review comment that would be posted, and "
        "nothing else: the first line is a short title (under ten words), the rest "
        "is the comment itself. Address what the person asked about, point at "
        "path:line for every claim, and offer at most three concrete things to "
        "look at or improve. If the change is fine as it stands, say that plainly.",
        "- Never approve or request changes; this task only ever posts a comment.",
        "",
        "Write for a first-time GitHub user: everyday words, short sentences, any "
        "technical term explained in the same sentence it appears, and a file and "
        "line number for every claim.",
    ]
    return "\n".join(lines)


def validate(data: dict) -> None:
    """The only required input is the pull request itself; the note is free."""
    raw = (data or {}).get("number")
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        raise ValueError("Choose the pull request you want reviewed.")


def apply(context: dict, gh, job) -> dict:
    """Post the approved draft as one COMMENT review. Never approves."""
    context = context or {}
    repo = str(context.get("repo") or getattr(job, "repo_full_name", "") or "")
    number = int(getattr(job, "number", 0) or context.get("number") or 0)
    body = str((context.get("draft") or {}).get("body") or "").strip()
    if not repo or number < 1:
        raise ValueError("The pull request could not be identified, so nothing "
                         "was posted.")
    if not body:
        raise ValueError("The review comment is empty, so nothing was posted.")
    gh.rest("POST", f"repos/{repo}/pulls/{number}/reviews",
            body={"body": body, "event": "COMMENT"})
    return {"result": f"Posted review comment on PR #{number}"}


TASK = TaskType(
    id="review-pr",
    label="Review a pull request",
    profile="draft",
    help="Explains what a pull request changes in everyday words, says whether "
         "its checks pass, and drafts one review comment you can post or throw "
         "away.",
    takes="pr",
    fields={
        "note": {
            "label": "Anything to focus on? (optional)",
            "required": False,
        },
    },
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
