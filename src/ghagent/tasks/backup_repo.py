"""backup-repo: one complete, restorable copy of the repository — read-only.

Read-only profile: the executor gives this task a scratch checkout and a
report, and makes no remote writes at all. The agent runs `git bundle create`
inside that checkout, verifies the bundle, and explains the result in plain
words. The bundle is an untracked file, so the executor's result export copies
it to the member's chosen folder automatically; a file over the export limit is
named in the summary instead of being copied.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..results import EXPORT_LIMIT_BYTES
from .types import TaskType

# Where the bundle is written inside the scratch checkout, and how it is named.
BUNDLE_DIR = "backup"
BUNDLE_PREFIX = "repo-"
BUNDLE_SUFFIX = ".bundle"

# The result export refuses a single file larger than this, and caps the whole
# export at the same size. The instruction asks the agent to say so in words.
EXPORT_LIMIT_WORDS = "50 MB"


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def bundle_name(day: date | None = None) -> str:
    """The bundle's path inside the checkout, dated YYYYMMDD (UTC)."""
    day = day or _utc_today()
    return f"{BUNDLE_DIR}/{BUNDLE_PREFIX}{day:%Y%m%d}{BUNDLE_SUFFIX}"


def build_instruction(context: dict) -> str:
    """Tell the agent exactly which git commands to run and what to report."""
    context = context or {}
    repo = str(context.get("repo") or "this repository")
    bundle = bundle_name()
    return (
        f"You are making a backup of the GitHub repository {repo}. A backup "
        "here means one single file that holds every branch and every commit, "
        "so the whole project can be rebuilt from it later without GitHub.\n\n"
        "Work only inside the checkout you are standing in, and run these git "
        "commands one at a time, in this order. Run them as plain git commands "
        "with no shell tricks:\n"
        f"1. Create the folder: mkdir -p {BUNDLE_DIR}\n"
        f"2. Write the backup: git bundle create {bundle} --all\n"
        f"3. Check that the backup is sound: git bundle verify {bundle}\n\n"
        "The third command reads the file back and confirms it is complete. If "
        "it reports an error, say so in one plain sentence and stop; never "
        "present an unchecked file as a working backup.\n\n"
        "Then report, in everyday English:\n"
        f"- The file's name, {bundle}, and its size in plain words: give the "
        "number and a comparison a person can picture (for example '12 MB, "
        "about the size of a few photos'). Read the real size from the file; "
        "do not estimate.\n"
        "- How to restore it, spelled out as the exact command: git clone "
        f"{bundle} restored-copy\n"
        "- What the backup holds: every branch and every commit that exists in "
        "this checkout right now, and nothing from GitHub that was never "
        "fetched.\n\n"
        f"The backup file is copied to the person's result folder "
        f"automatically after this task. The copy is limited to {EXPORT_LIMIT_WORDS} "
        f"({EXPORT_LIMIT_BYTES} bytes) per bundle, with the same limit shared "
        "by all exported files: if the bundle is larger than "
        "that, say plainly in the summary that it was too large to be copied "
        "and give its exact size and where it sits on this machine.\n\n"
        "Change no repository files, and push, post or comment nothing: this "
        "task only reads and writes one new file inside the checkout. Finish "
        "with the complete plain-language report in the summary field."
    )


def validate(data: dict) -> None:
    """A backup needs no choices, so every input this task accepts is valid."""
    return None


TASK = TaskType(
    id="backup-repo",
    label="Back up the repository",
    profile="read-only",
    takes="repo",
    help=("Writes one complete backup of the repository — every branch and "
          "commit — into a single file in your result folder, checks that copy "
          "is sound, and tells you in plain words how to restore it. Nothing "
          "on GitHub is changed."),
    fields={},
    build_instruction=build_instruction,
    validate=validate,
)
