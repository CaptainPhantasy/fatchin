"""write-docs: refresh the repository's own documentation and open one PR.

Draft profile: the agent reads the real code, edits the README and the docs in
the scratch checkout, and writes the pull request text into
.gha-drafts/pull-request.md. The job stops at WAITING_ON_YOU; nothing is
committed, pushed, or opened until the person chooses Post, and that one action
opens exactly one pull request.

Git authentication comes from the worker environment: the gh credential helper
or a GH_TOKEN already present in the worker's environment. This module never
asks for, stores, or prints a credential. Every failure is raised as
ValueError carrying one plain sentence the person can act on, so the GUI shows
what happened rather than a traceback.
"""
from __future__ import annotations

import os
import re
import subprocess

from .types import TaskType
from ..task_write_guard import (PreWriteRefusal, ensure_prepared_tree,
                               ensure_not_stopped, repository_policy, verify_before_write)

DRAFT_FILE = ".gha-drafts/pull-request.md"
GIT_TIMEOUT_SECONDS = 600.0
GH_TIMEOUT_SECONDS = 120.0
REMOTE = "origin"
BRANCH_PREFIX = "gha/docs-"
COMMIT_MESSAGE = "docs: refresh the repository documentation"

ARTIFACT_DIRS = {".gha-drafts", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".DS_Store")


def _clip(text: object, limit: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _values(context: dict, job) -> dict:
    values = (context or {}).get("values")
    if isinstance(values, dict):
        return values
    notes = getattr(job, "notes", None) or {}
    stored = notes.get("values")
    return stored if isinstance(stored, dict) else {}


def build_instruction(context: dict) -> str:
    """Tell the agent what to read, what to write, and where the draft goes."""
    context = context or {}
    repo = str(context.get("repo") or "this repository")
    focus = _clip(_values(context, None).get("focus"), 500)
    lines = [
        f"Task: write the documentation {repo} is missing, so a person who has "
        "never used this project can start working with it.",
        "",
        "Read the actual code before writing a word. Open the source files, the "
        "tests, the build and configuration files, and the README if there is "
        "one. Every instruction you write must match what the code really does; "
        "when the code and the old documentation disagree, the code wins and "
        "the documentation is corrected.",
        "",
        "Write or refresh:",
        "- README.md: what the project does in one plain paragraph, who it is "
        "for, what to install, the exact commands to set it up and run it, how "
        "to run the tests, and where the other documentation lives.",
        "- Any documentation the repository needs and does not have: a short "
        "setup guide, a configuration reference, or a troubleshooting section, "
        "when the code shows they would help. Do not invent features.",
        "",
        "How to write:",
        "- Everyday English, short sentences, and every technical word explained "
        "in the same sentence it appears.",
        "- Setup steps a first-time user can follow exactly, in order, with the "
        "real command names from this checkout. Name the file and line you took "
        "each fact from, like src/setup.py:12, in your own reasoning.",
        "- Leave working code alone: change documentation files only.",
        "",
        f"Where the pull request text goes: write {DRAFT_FILE}. Its first line is "
        "the pull request title: short and specific, no 'docs:' prefix. "
        "Everything after the first line is the pull request body: what "
        "documentation changed, why each change was needed, and how a reader "
        "can check it. Write it for someone who does not read code.",
        "",
        "Do not commit, push, or open anything yourself. The person reads the "
        "draft first and decides whether it goes to GitHub.",
    ]
    if focus:
        lines += ["", f"The person asks you to pay particular attention to: {focus}"]
    return "\n".join(lines)


def validate(data: dict) -> None:
    """The focus note is optional, so every input this task accepts is valid."""
    return None


def _run_git(args: list[str], cwd: str,
             timeout: float = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run one explicit `git` command inside the scratch checkout.

    Tests patch this function (or subprocess.run); nothing else here spawns git.
    """
    argv = ["git", *args]
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "git did not finish in time"
    except OSError as exc:
        return 127, "", str(exc)


def _is_artifact(path: str) -> bool:
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    if any(part in ARTIFACT_DIRS for part in parts):
        return True
    return any(str(path).endswith(suffix) for suffix in ARTIFACT_SUFFIXES)


def _worktree(context: dict, job) -> str:
    notes = getattr(job, "notes", None) or {}
    for candidate in (context.get("worktree"), notes.get("worktree_path"),
                      notes.get("worktree")):
        text = str(candidate or "").strip()
        if text and os.path.isdir(text):
            return text
    raise PreWriteRefusal("The scratch checkout for this run is gone, so there was "
                     "nothing to commit. Run the task again to rebuild the "
                     "documentation.")


def _base_branch(context: dict, values: dict, job) -> str:
    notes = getattr(job, "notes", None) or {}
    for candidate in (context.get("base_branch"), values.get("base_branch"),
                      notes.get("base_branch")):
        text = str(candidate or "").strip()
        if text:
            return text
    return "main"


def _branch_name(job) -> str:
    job_id = str(getattr(job, "job_id", "") or "")
    tail = job_id.rsplit("_", 1)[-1] if job_id else ""
    clean = re.sub(r"[^A-Za-z0-9.-]+", "", tail) or re.sub(
        r"[^A-Za-z0-9.-]+", "", job_id)
    return BRANCH_PREFIX + ((clean or "run")[-20:])


def _staged_paths(worktree: str) -> list[str]:
    rc, out, err = _run_git(["diff", "--cached", "--name-only", "-z"], cwd=worktree)
    if rc != 0:
        raise PreWriteRefusal("The staged documentation files could not be read "
                              f"(git said: {_clip(err or out)}). Nothing was pushed.")
    return [path for path in out.split("\0") if path]


def _unstage_artifacts(worktree: str) -> None:
    artifacts = [path for path in _staged_paths(worktree) if _is_artifact(path)]
    if artifacts:
        rc, out, err = _run_git(["reset", "-q", "HEAD", "--", *artifacts], cwd=worktree)
        if rc != 0:
            raise PreWriteRefusal("Worker scratch files could not be excluded from "
                                  "the documentation commit (git said: "
                                  f"{_clip(err or out)}). Nothing was pushed.")


def _open_pull_request(repo: str, base: str, branch: str, title: str,
                       body: str) -> dict:
    """Create the pull request with gh; git auth comes from the environment."""
    from .. import responder  # lazy: importing responder pulls in gh plumbing

    path = responder._write_temp(body or title)
    try:
        rc, out, err = responder._run_gh(
            ["pr", "create", "--repo", repo, "--base", base, "--head", branch,
             "--title", title, "--body-file", path],
            timeout=GH_TIMEOUT_SECONDS)
    finally:
        responder._cleanup_temp(path)
    if rc != 0:
        raise ValueError(
            "The documentation was committed, but GitHub refused to open the "
            f"pull request (gh said: {_clip(err or out)}). The branch "
            f"{branch} is already on GitHub; open the pull request from the "
            "repository page, or run the task again once the connection works.")
    match = re.search(r"/pull/(\d+)", out or "")
    if not match:
        return {"result": f"Opened a pull request from {branch}, but its "
                          "number could not be read from GitHub's reply. Open "
                          "the repository's pull requests to confirm it."}
    return {"result": f"Opened pull request #{int(match.group(1))} — {title}"}


def apply(context: dict, gh, job) -> dict:
    """Commit the documentation, push one branch, open one pull request.

    git auth comes from the worker environment (the gh credential helper or
    GH_TOKEN); a missing credential surfaces as the plain errors below, never
    as a traceback. The push is explicit and never forced.
    """
    context = context or {}
    ensure_not_stopped(context)
    values = _values(context, job)
    repo = str(context.get("repo")
               or getattr(job, "repo_full_name", "") or "").strip()
    if not repo:
        raise PreWriteRefusal("The repository could not be identified, so nothing "
                         "was pushed and no pull request was opened.")
    draft = context.get("draft") or {}
    title = _clip(draft.get("title"), 200)
    body = str(draft.get("body") or "").strip()
    if not title:
        raise PreWriteRefusal("This pull request has no title yet. Make the draft's "
                         "first line the title, save it, then choose Post again.")
    worktree = _worktree(context, job)
    base = _base_branch(context, values, job)
    branch = _branch_name(job)
    repository_policy(context, job)

    rc, out, err = _run_git(["add", "-A"], cwd=worktree)
    if rc != 0:
        raise PreWriteRefusal("The changed files could not be staged (git said: "
                         f"{_clip(err or out)}). Nothing was pushed.")
    _unstage_artifacts(worktree)
    if not [path for path in _staged_paths(worktree) if not _is_artifact(path)]:
        raise PreWriteRefusal("There is nothing to commit: the documentation in "
                         "this checkout already matches what is there. Nothing "
                         "was pushed and no pull request was opened.")

    tree = verify_before_write(context, gh, job, worktree, _run_git, [base])
    ensure_not_stopped(context)
    rc, out, err = _run_git(
        ["commit", "-m", COMMIT_MESSAGE, "-m",
         f"Written by FatChin job {getattr(job, 'job_id', '') or 'unknown'}."],
        cwd=worktree)
    if rc != 0:
        raise PreWriteRefusal("The documentation could not be committed (git said: "
                         f"{_clip(err or out)}). Nothing was pushed.")
    tested = ensure_prepared_tree(_run_git, worktree, tree)
    ensure_not_stopped(context)
    rc, out, err = _run_git(["push", REMOTE, f"{tested}:refs/heads/{branch}"],
                            cwd=worktree)
    if rc != 0:
        raise ValueError("The documentation was committed on this machine, but "
                         f"the branch could not be pushed to GitHub with a confirmed result (git said: "
                         f"{_clip(err or out)}). No pull request was opened; "
                         "the push outcome is uncertain. Check the branch on "
                         "GitHub before starting another task.")
    ensure_not_stopped(context, after_write=True)
    return _open_pull_request(repo, base, branch, title, body)


TASK = TaskType(
    id="write-docs",
    label="Write the documentation",
    profile="draft",
    help="Reads the real code, writes or refreshes the README and the setup "
         "steps a first-time user needs, and drafts one pull request you can "
         "open or throw away.",
    takes="repo",
    fields={"focus": {"label": "Anything to focus on? (optional)",
                      "required": False}},
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
