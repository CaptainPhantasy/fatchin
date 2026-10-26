"""triage-issues: turn one person's words into ready-to-post GitHub issues.

Draft profile: the run writes candidate issues as files in .gha-drafts/ and
stops at WAITING_ON_YOU. Only the person's explicit Post action creates an
issue, one issue per approved draft. Nothing here closes, edits, or deletes.
"""
from __future__ import annotations

from .types import TaskType

DRAFT_DIR = ".gha-drafts"
EMPTY_WORDS = "Describe the problem in your own words — one sentence is enough."


def _reject(message: str) -> Exception:
    from ..web_control import ApiError
    return ApiError(message)


def _values(data) -> dict:
    values = (data or {}).get("values") if isinstance(data, dict) else None
    return values if isinstance(values, dict) else {}


def validate(data) -> None:
    words = str(_values(data).get("words") or "").strip()
    if not words:
        raise _reject(EMPTY_WORDS)


def build_instruction(context: dict) -> str:
    context = context or {}
    words = str((context.get("values") or {}).get("words") or "").strip()
    repo = context.get("repo") or "this repository"
    return (
        f"Task: turn one person's message into ready-to-post issues for {repo}.\n"
        f"Here are their words, exactly as written:\n---\n{words}\n---\n\n"
        "Start by reading the repository so the issues fit what is really there: "
        "the README, the folder layout, and the files the words point at. Cite "
        "every claim with its file and line (for example `src/app.py:42`) and "
        "explain it in words a first-time GitHub user can follow.\n\n"
        "Split the words into small, separate pieces of work: one issue per "
        "problem or request. Write only what the person actually described — do "
        "not add improvements they did not ask for. If their words describe a "
        "single problem, write a single file.\n\n"
        f"Write each proposed issue as its own Markdown file in {DRAFT_DIR}/. "
        "The first line of the file is the issue title: short and specific, with "
        "no 'Issue:' prefix. Everything after the first line is the body, and "
        "every body says what is wrong, the steps that show the problem, and one "
        "sentence stating what 'done' means so the person can check it.\n\n"
        "Do not post, close, edit, or delete anything on GitHub. You are only "
        "writing files in this checkout; the person decides what gets posted."
    )


def apply(context: dict, gh, job) -> dict:
    context = context or {}
    draft = context.get("draft") or {}
    title = str(draft.get("title") or "").strip()
    body = str(draft.get("body") or "").strip()
    if not title:
        raise _reject("This issue has no title yet. Make the draft's first line "
                      "the title, save it, then choose Post again.")
    created = gh.rest("POST", f"repos/{context.get('repo')}/issues",
                      body={"title": title, "body": body}) or {}
    number = created.get("number") if isinstance(created, dict) else None
    if not number:
        return {"result": f"GitHub accepted “{title}” but did not return its "
                          "number. Open the repository to confirm it before "
                          "posting this draft again."}
    shown = str(created.get("title") or title) if isinstance(created, dict) else title
    return {"result": f"Created issue #{number} — {shown}"}


TASK = TaskType(
    id="triage-issues",
    label="Turn your words into issues",
    profile="draft",
    takes="repo",
    help=("Reads your words, checks the repository for context, and writes each "
          "piece of work as a separate issue you can read and change before "
          "anything is posted."),
    fields={"words": {"label": "Your words", "required": True}},
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
