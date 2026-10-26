"""hygiene-sweep: audit a checkout and report what needs tidying up.

Read-only profile: the executor gives this task a scratch checkout and a
report, and makes no remote writes at all. The findings land in the job's
summary; no file is changed, nothing is committed, pushed, or posted.
"""
from __future__ import annotations

from .types import TaskType

ASK_FOR_A_PLAIN_ANSWER = (
    "Answer in plain words a first-time GitHub user can follow. For every "
    "finding give the file and line (path:line) and one short sentence saying "
    "how to fix it. Findings come first, most serious first."
)


def build_instruction(context: dict) -> str:
    repo = (context or {}).get("repo") or "this repository"
    return (
        f"You are auditing the GitHub repository {repo} for everyday "
        "cleanliness and safety problems. Read the checkout you are standing "
        "in and change nothing.\n\n"
        "Look for exactly these things:\n"
        "1. Leaked secrets in the tree: long token-like strings (a run of 20 or "
        "more letters and digits that looks like a key, password, or access "
        "token), private keys, and any .env file that is committed even though "
        "it should not be. Say which file and line, and never copy the secret "
        "itself into your answer: show only the first four characters.\n"
        "2. Missing basics: no LICENSE file, or no README, or a README that "
        "never says how to install or run the project.\n"
        "3. Dependency pins that look outdated or vulnerable in the manifests "
        "you can read (requirements files, package.json, pyproject.toml, "
        "Gemfile, go.mod and similar): an old major version, a pin with a "
        "known bad version number, or a dependency with no version at all. "
        "Judge only from what is in the checkout: do not install anything and "
        "do not use the network.\n"
        "4. Stale branches you can see with `git branch -a`: branches whose "
        "last commit is far behind the main branch, or branches that look "
        "finished and forgotten. Say the branch name and how you judged it.\n"
        "5. Files that look like debug leftovers: stray debug prints, "
        "*_old.py / *-copy files, commented-out blocks of code, committed "
        "editor folders such as .idea or .vscode, and scratch files at the "
        "top of the tree. Point at the file and line.\n\n"
        f"{ASK_FOR_A_PLAIN_ANSWER}\n"
        "Group the findings by the five headings above, and under each heading "
        "say plainly when you found nothing rather than inventing a problem. "
        "If a file or branch could not be read, say so in one line. Finish "
        "with the complete list in the summary field; this task only reads "
        "and reports."
    )


def validate(data: dict) -> None:
    """A repository-wide audit needs no choices, so every input is valid."""
    return None


TASK = TaskType(
    id="hygiene-sweep",
    label="Check for everyday problems",
    profile="read-only",
    takes="repo",
    help=("Checks a repository for leaked secrets, a missing license or readme, "
          "outdated dependency pins, stale branches, and leftover debug files "
          "— and reports each one with a plain suggested fix."),
    fields={},
    build_instruction=build_instruction,
    validate=validate,
)
