"""organize-work: propose a label scheme for a repository's open work.

Draft profile: the run reads the open issues, writes the proposed label list to
.gha-drafts/plan.md, and stops at WAITING_ON_YOU. Only the person's explicit
Post action creates labels, and it creates labels alone: no issue is closed,
reopened, labelled, or edited here, and assigning the labels stays a decision
the person makes with the plan in hand.
"""
from __future__ import annotations

import re

from .types import TaskType

DRAFT_FILE = ".gha-drafts/plan.md"
PLAN_TITLE = "Organise this repository"
LABEL_LINE = re.compile(r"^LABEL:\s*(?P<rest>.+)$")
LABEL_SEPARATOR = re.compile(r"\s+[—–-]\s+")
NAME_LIMIT = 50
DESCRIPTION_LIMIT = 100

# GitHub's own palette, in a fixed order so the same plan always gets the same
# colours. Colour is decoration here; the name and description carry the meaning.
COLORS = ("d73a4a", "0075ca", "0e8a16", "e4e669", "5319e7", "fbca04",
          "bfd4f2", "c2e0c6", "d4c5f9", "f9d0c4", "fef2c0", "c5def5")


def _clip(text: object, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_instruction(context: dict) -> str:
    context = context or {}
    repo = str(context.get("repo") or "this repository")
    return (
        f"Task: propose a label scheme for the open work in {repo}, so a person "
        "can see at a glance what kind of work each issue is.\n\n"
        "Start by reading every open issue. Run `gh issue list --repo "
        f"{repo} --state open --limit 200 --json number,title,body,labels` with "
        "the run_command tool; if that command is not available to you, read the "
        "same issues through the GitHub API with the tools you have. Read the "
        "titles and bodies yourself: what an issue is really about decides which "
        "label fits it.\n\n"
        f"Write your proposal into {DRAFT_FILE}. Its first line is exactly: "
        f"{PLAN_TITLE}\n\n"
        "After that first line, write one line per proposed label, in exactly "
        "this shape:\n"
        "LABEL: <name> — <one-line description>\n"
        "Keep the list short: six to twelve labels that fit this repository's "
        "real work. Use ordinary lower-case words joined by hyphens, such as "
        "needs-triage, and make every description one short sentence saying when "
        "the label is used, in everyday English.\n\n"
        "After the label list, add a section whose heading line is exactly "
        "`Assigned:`. Under it, list each label with the issue numbers that fit "
        "it, one label per line, like `bug: #12, #31`. Every open issue appears "
        "under at least one label, and every number you list comes from an issue "
        "you actually read — never invent an issue number.\n\n"
        "You write this plan file and nothing else. Do not create labels, and do "
        "not close, reopen, label, or edit any issue: assigning labels stays with "
        "the person reading the plan. When the plan is written, stop."
    )


def validate(data: dict) -> None:
    """A tidy-up plan needs no choices, so every input is valid."""
    return None


def _labels_from_body(body: object) -> list[tuple[str, str]]:
    """Read the proposed labels out of the draft's `LABEL:` lines, in order."""
    labels: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in str(body or "").splitlines():
        text = line.strip()
        if text.startswith(("- ", "* ")):
            text = text[2:].lstrip()
        match = LABEL_LINE.match(text)
        if not match:
            continue
        parts = LABEL_SEPARATOR.split(match.group("rest").strip(), maxsplit=1)
        name = parts[0].strip()[:NAME_LIMIT]
        description = (parts[1].strip() if len(parts) > 1 else "")[:DESCRIPTION_LIMIT]
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        labels.append((name, description))
    return labels


def _existing_labels(gh, repo: str) -> set[str]:
    try:
        listed = gh.rest("GET", f"repos/{repo}/labels",
                         body={"per_page": 100}, paginate=True) or []
    except Exception as exc:
        raise ValueError(
            "The labels already on this repository could not be read (GitHub "
            f"said: {_clip(exc)}). Nothing was created; choose Post again once "
            "the connection works.") from None
    names: set[str] = set()
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]).casefold())
    return names


def apply(context: dict, gh, job) -> dict:
    """Create the plan's labels, skipping every label the repository has."""
    context = context or {}
    repo = str(context.get("repo")
               or getattr(job, "repo_full_name", "") or "").strip()
    if not repo:
        raise ValueError("The repository could not be identified, so no label "
                         "was created.")
    draft = context.get("draft") or {}
    labels = _labels_from_body(draft.get("body"))
    if not labels:
        raise ValueError("This plan lists no labels yet. Make each label line "
                         "read “LABEL: name — description”, save the plan, then "
                         "choose Post again.")
    existing = _existing_labels(gh, repo)
    created: list[str] = []
    for order, (name, description) in enumerate(labels):
        if name.casefold() in existing:
            continue
        try:
            gh.rest("POST", f"repos/{repo}/labels",
                    body={"name": name, "description": description,
                          "color": COLORS[order % len(COLORS)]})
        except Exception as exc:
            raise ValueError(
                f"GitHub refused the label “{name}” (it said: {_clip(exc)}). "
                f"{len(created)} label(s) were created before it; the rest were "
                "not, and no issue was touched.") from None
        created.append(name)
    return {"result": f"Created {len(created)} labels. Assigning labels to "
                      "issues stays with you; the plan lists every match."}


TASK = TaskType(
    id="organize-work",
    label="Organise the labels",
    profile="draft",
    takes="repo",
    help=("Reads every open issue, proposes a short set of labels that fits the "
          "work, and shows which issues each label fits — creating the labels "
          "only when you say so."),
    fields={},
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
