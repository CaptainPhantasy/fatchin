"""Explain a repository in plain words — a read-only briefing task.

The person may ask one question; with no question the briefing covers what the
repository does, how it is organized, and what changed recently. The executor's
"read-only" profile gives this task a scratch checkout and a report, and makes
no remote writes at all.
"""
from __future__ import annotations

from .types import TaskType

DEFAULT_OVERVIEW = ("what does this repository do, how is it organized, "
                    "and what changed recently")

ASK_AGAIN = ("Ask your question in your own words — one sentence is enough, "
             "or leave it empty for an overview.")


def _question(values: dict) -> str:
    question = values.get("question")
    return str(question).strip() if question is not None else ""


def build_instruction(context: dict) -> str:
    values = context.get("values") or {}
    question = _question(values)
    repo = context.get("repo") or "this repository"
    if question:
        asked = (f'The person asked, in their own words: "{question}"\n'
                 "Answer that question and nothing else.")
    else:
        asked = ("The person asked for an overview, so cover all of this: "
                 f"{DEFAULT_OVERVIEW}.")
    return (
        f"You are writing a short briefing about the GitHub repository {repo} "
        "for someone who has never used GitHub. That person does not read code, "
        "so every sentence must be in everyday English.\n\n"
        f"{asked}\n\n"
        "Read the checkout you are standing in: its files and folders, its "
        "README, and the most recent commits. Stay inside this checkout and "
        "change nothing.\n\n"
        "When you write the briefing:\n"
        "- Back up every claim by naming the file and the line, like "
        "src/example.py:42.\n"
        "- Say what each important folder or file is for, and why a newcomer "
        "would open it.\n"
        "- Explain a GitHub word the first time you use it (repository, "
        "branch, commit, pull request).\n"
        "- Lead with the single most useful sentence, then a few short "
        "sections.\n"
        "- If the checkout does not show something, say you could not see it "
        "instead of guessing.\n\n"
        "Change no files, and post, push or comment nothing: this task only "
        "reads and reports. Finish with the complete briefing in the summary "
        "field."
    )


def validate(data: dict) -> None:
    values = (data or {}).get("values")
    question = values.get("question") if isinstance(values, dict) else None
    text = str(question).strip() if question is not None else ""
    if text and not any(character.isalnum() for character in text):
        from ..web_control import ApiError  # deferred: web_control imports this package
        raise ApiError(ASK_AGAIN)


TASK = TaskType(
    id="explain-repo",
    label="Explain a repository",
    profile="read-only",
    help=("Explains a repository in plain words, with file and line "
          "references — nothing on GitHub is changed."),
    takes="repo",
    fields={"question": {"label": "Your question", "required": False}},
    build_instruction=build_instruction,
    validate=validate,
)
