"""cleanup-merged: finish the work a merged pull request started.

High-stakes profile: the run reads the pull request the person already merged
and writes the release notes into .gha-drafts/release-notes.md, then stops at
WAITING_ON_YOU. After the person confirms, GitHub's closing links determine
which issues in this repository can close, the same repository's merged branch
can be deleted, and the release is published. Merging itself never happens here,
and the base branch and fork branches are never touched.
"""
from __future__ import annotations

import re

from .types import TaskType

DRAFT_DIR = ".gha-drafts"
DRAFT_FILE = ".gha-drafts/release-notes.md"
NOT_MERGED = "That pull request is not merged yet — merging stays your decision."
PROTECTED_BRANCHES = {"main", "master"}
CLOSING_ISSUES_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    defaultBranchRef { name }
    pullRequest(number:$number) {
      merged baseRefName
      closingIssuesReferences(first:100,after:$cursor) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _reject(message: str) -> Exception:
    from ..web_control import ApiError
    return ApiError(message)


def _clip(text: object, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _values(data) -> dict:
    values = (data or {}).get("values") if isinstance(data, dict) else None
    return values if isinstance(values, dict) else {}


def _slug(title: str, limit: int = 60) -> str:
    """A tag GitHub accepts: lowercase letters, digits and single dashes."""
    text = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return text[:limit].strip("-")


def _is_merged(pull: dict) -> bool:
    merged = str(pull.get("merged", "")).lower() in {"true", "1"} or pull.get("merged") is True
    state = str(pull.get("state") or "").upper()
    return bool(merged or state == "MERGED")


def _closing_issues(gh, repo: str, pull_number: int) -> list[int]:
    """Read GitHub's closing links, never infer permission from body mentions.

    A merge into a release/feature branch does not close the linked issues.
    Cross-repository links stay untouched: this cleanup only acts in its named
    repository. Missing or incomplete evidence refuses all writes.
    """
    try:
        owner, name = repo.split("/", 1)
        numbers: list[int] = []
        cursor = None
        seen = set()
        while True:
            data = gh.graphql(CLOSING_ISSUES_QUERY,
                              {"owner": owner, "name": name,
                               "number": pull_number, "cursor": cursor})
            repository = (data or {}).get("repository") or {}
            pull = repository.get("pullRequest") or {}
            default = (repository.get("defaultBranchRef") or {}).get("name")
            if pull.get("merged") is not True or not default or not pull.get("baseRefName"):
                raise ValueError("Missing merged pull request evidence")
            if pull["baseRefName"] != default:
                return []
            references = pull.get("closingIssuesReferences") or {}
            nodes, page = references.get("nodes"), references.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page, dict):
                raise ValueError("Missing linked issue page")
            if not isinstance(page.get("hasNextPage"), bool):
                raise ValueError("Missing linked issue pagination")
            for issue in nodes:
                issue_repo = (issue.get("repository") or {}).get("nameWithOwner")
                number = issue.get("number")
                if not issue_repo or type(number) is not int or number < 1:
                    raise ValueError("Incomplete linked issue")
                if issue_repo.casefold() == repo.casefold() and number not in numbers:
                    numbers.append(number)
            if not page["hasNextPage"]:
                return numbers
            cursor = page.get("endCursor")
            if not cursor or cursor in seen or len(seen) >= 99:
                raise ValueError("Incomplete linked issue pagination")
            seen.add(cursor)
    except Exception:
        raise ValueError("The pull request's closing issue links could not be verified. "
                         "Nothing was changed on GitHub; try again when GitHub can "
                         "provide the complete merge details.") from None


def _tag_taken(exc: BaseException) -> bool:
    text = str(exc).lower()
    return ("already_exists" in text or "already exists" in text
            or ("422" in text and "tag" in text))


def validate(data) -> None:
    """The pull request is the input; the release tag is optional."""
    raw = (data or {}).get("number") if isinstance(data, dict) else None
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        raise _reject("Choose the merged pull request you want to tidy up after.")
    tag = str(_values(data).get("tag") or "").strip()
    if tag and (any(char.isspace() for char in tag) or tag.startswith("-")):
        raise _reject("A release tag cannot contain spaces or start with a dash. "
                      "Use something like v1.2.0, or leave the tag empty and the "
                      "release title becomes the tag.")


def build_instruction(context: dict) -> str:
    """Tell the agent what to read, what to write, and what stays untouched."""
    context = context or {}
    repo = str(context.get("repo") or "this repository")
    number = context.get("number") or "?"
    tag = _clip(_values(context).get("tag"), 100)
    lines = [
        f"Task: pull request #{number} in {repo} has been merged. Read it, then "
        "write release notes a person who does not read code can understand.",
        "",
        "Read the pull request itself; the checkout holds the base branch, not the "
        "merged commits:",
        f"- gh pr view {number} --repo {repo} --json "
        "title,body,files,mergedAt,baseRefName,headRefName",
        f"- gh pr diff {number} --repo {repo}",
        "- read_file and list_dir for the surrounding code in the checkout.",
        "If one of these commands cannot run, write down what you could not read "
        "and carry on with what you have.",
        "",
        f"Write {DRAFT_FILE}. Its first line is the release title: short and "
        "specific, under ten words, with no version number. Everything after the "
        "first line is the release notes: what changed for the people who use the "
        "software — what it did before, what it does now, and anything they need "
        "to do differently. Use everyday words and short sentences, explain every "
        "technical word in the same sentence it appears, and cite a file and line "
        "number for every claim (for example src/app.py:42). Do not describe "
        "features the pull request does not contain.",
        "",
        "In your summary, name the issues the pull request says it closes (lines "
        "like 'Closes #12'), so the person can check them before confirming. "
        "What happens then, and only after the person confirms: GitHub's linked "
        "closing issues in this repository are closed if the pull request merged "
        "into its default branch. A merged branch belonging to this same repository "
        "can be deleted; branches in forks stay untouched. A release is published with "
        "your title, the tag and these notes.",
        "",
        "Do not post, comment, close, push, delete, or publish anything yourself. "
        "You are only writing files in this checkout.",
    ]
    if tag:
        lines += ["", f"The release tag the person wants is {tag}."]
    return "\n".join(lines)


def apply(context: dict, gh, job) -> dict:
    """One confirmed cleanup: close linked issues, delete a same-repository branch,
    publish the release. Refuses outright unless the pull request is merged."""
    context = context or {}
    repo = str(context.get("repo")
               or getattr(job, "repo_full_name", "") or "").strip()
    try:
        number = int(context.get("number") or getattr(job, "number", 0) or 0)
    except (TypeError, ValueError):
        number = 0
    if not repo or number < 1:
        raise ValueError("The pull request could not be identified, so nothing "
                         "was changed on GitHub.")
    draft = context.get("draft") or {}
    title = _clip(draft.get("title"), 200)
    notes = str(draft.get("body") or "").strip()
    if not title:
        raise ValueError("The release notes have no title yet. Make the draft's "
                         "first line the title, save it, then confirm again.")
    tag = str(_values(context).get("tag") or "").strip() or _slug(title)
    if not tag:
        raise ValueError("The release needs a tag and the title has no letters or "
                         "digits to build one from. Fill in the tag and confirm "
                         "again.")

    pull = gh.rest("GET", f"repos/{repo}/pulls/{number}") or {}
    if not isinstance(pull, dict) or not _is_merged(pull):
        raise ValueError(NOT_MERGED)

    head = str((pull.get("head") or {}).get("ref") or "").strip()
    base = str((pull.get("base") or {}).get("ref") or "").strip()
    references = _closing_issues(gh, repo, number)
    head_repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name")

    closed: list[int] = []
    failed: list[tuple[int, str]] = []
    for issue in references:
        try:
            gh.rest("PATCH", f"repos/{repo}/issues/{issue}",
                    body={"state": "closed"})
            closed.append(issue)
        except Exception as exc:
            failed.append((issue, _clip(exc)))

    deleted = False
    branch_note = ""
    if not head:
        branch_note = " The pull request does not name its head branch, so no branch was deleted."
    elif not head_repo or str(head_repo).casefold() != repo.casefold():
        branch_note = (" Its source repository is a fork or could not be verified, "
                       "so no branch was deleted.")
    elif head in PROTECTED_BRANCHES or head == base:
        branch_note = (f" Its head was {head}, a branch this task never deletes, "
                       "so the branch was left alone.")
    else:
        try:
            gh.rest("DELETE", f"repos/{repo}/git/refs/heads/{head}")
            deleted = True
        except Exception as exc:
            branch_note = (f" The merged branch {head} could not be deleted "
                           f"(gh said: {_clip(exc)}).")

    if failed:
        listed = ", ".join(f"#{issue}" for issue, _ in failed)
        branch_note += (f" {listed} could not be closed (gh said: "
                        f"{failed[0][1]}).")
    branch_text = ("deleted the merged branch" if deleted
                   else "left the merged branch alone")

    try:
        gh.rest("POST", f"repos/{repo}/releases",
                body={"tag_name": tag, "name": title, "body": notes})
    except Exception as exc:
        if _tag_taken(exc):
            return {"result": (f"Closed {len(closed)} issue(s), {branch_text}, but "
                               f"the tag {tag} already exists — no release was "
                               "published. Choose another tag and confirm again.")}
        raise ValueError(
            f"The release could not be published (gh said: {_clip(exc)}). The "
            "issues and the branch were already handled.") from None

    return {"result": (f"Closed {len(closed)} issue(s), {branch_text}, published "
                       f"release {tag}.{branch_note}")}


TASK = TaskType(
    id="cleanup-merged",
    label="Tidy up after a merge",
    profile="high-stakes",
    takes="pr",
    help=("Reads a pull request you already merged, drafts the release notes, "
          "and — only after you confirm — closes its linked issues in this "
          "repository, deletes its merged branch when it belongs to this "
          "repository, and publishes the release. Fork branches stay untouched."),
    fields={"tag": {"label": "Release tag (optional)", "required": False}},
    post_confirm_required=True,
    allows_closed=True,
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
