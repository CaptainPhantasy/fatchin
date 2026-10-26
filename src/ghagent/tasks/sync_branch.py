"""sync-branch: move one branch's work into another branch, with a person's yes.

Write profile: the run inspects both branches in a scratch checkout and writes
.gha-drafts/plan.md, so the person reads exactly what would move and what could
go wrong before anything happens. The job stops at WAITING_ON_YOU; only the
person's explicit Post action fetches, merges, and pushes.

Git is run as an explicit argv list (never a shell string), with a small fixed
environment: git never stops to ask for a password, because nobody is there to
type one. The merge commit is authored as gha-worker, because a member account
carries no git identity of its own. A merge that conflicts is undone with
`git merge --abort` and the person is told, in one sentence, that a human has
to resolve it. The push is never forced and never writes any remote branch but
the chosen target.
"""
from __future__ import annotations

import os
import subprocess

from .types import TaskType
from ..task_write_guard import (PreWriteRefusal, commit_id, ensure_prepared_tree,
                               ensure_not_stopped, repository_policy, require_remote_refs,
                               verify_before_write)

DRAFT_FILE = ".gha-drafts/plan.md"
REMOTE = "origin"
GIT_TIMEOUT_SECONDS = 600.0
CONFLICT_MESSAGE = "The branches conflict; a person needs to resolve this."
# A member account has no git identity of its own; the merge commit needs one.
IDENTITY = ["-c", "user.name=gha-worker", "-c", "user.email=gha-worker@inference"]


def _reject(message: str) -> Exception:
    from ..web_control import ApiError
    return ApiError(message)


def _values(data, job=None) -> dict:
    values = (data or {}).get("values") if isinstance(data, dict) else None
    if isinstance(values, dict):
        return values
    notes = getattr(job, "notes", None) or {}
    stored = notes.get("values")
    return stored if isinstance(stored, dict) else {}


def _name(values: dict, key: str) -> str:
    value = values.get(key)
    return str(value).strip() if value is not None else ""


def _clip(text: object, limit: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _looks_like_a_branch(name: str) -> bool:
    if not name or name.startswith("-"):
        return False
    if any(char.isspace() for char in name):
        return False
    return ".." not in name and "@{" not in name


def _check_branch_names(values: dict, error=_reject) -> None:
    """Refuse names that would confuse git; `error` shapes the message class."""
    source, target = _name(values, "source"), _name(values, "target")
    for name, label in ((source, "Branch to take changes from"),
                        (target, "Branch to bring up to date")):
        if not name:
            raise error(f"Fill in “{label}” — writing the branch name is enough.")
        if not _looks_like_a_branch(name):
            raise error(f"“{name}” is not a branch name that can be used safely. "
                        "Write it the way GitHub shows it, with no spaces and "
                        "no leading dash, like feature/login.")
    if source == target:
        raise error("The two branches are the same, so there is nothing to "
                    "move. Name the branch the work comes from and the "
                    "different branch that should receive it.")


def build_instruction(context: dict) -> str:
    """Tell the agent what to compare, what to explain, and where the plan goes."""
    context = context or {}
    values = _values(context)
    repo = str(context.get("repo") or "this repository")
    source = _name(values, "source") or "the source branch"
    target = _name(values, "target") or "the target branch"
    return (
        f"Task: bring the branch “{target}” of {repo} up to date with the branch "
        f"“{source}”, and explain to the person exactly what that will move "
        "before anything happens.\n\n"
        "Look before you write. Read-only git commands are fine here (git log, "
        "git diff, git show, git merge-base); do not fetch, merge, checkout, "
        "commit, or push anything yourself. Compare the two branches with "
        f"`git log --oneline origin/{source}..origin/{target}` and "
        f"`git log --oneline origin/{target}..origin/{source}` when those "
        "remote branches are present in the checkout; if one is missing, say so "
        "in one line instead of guessing.\n\n"
        "Your summary is what the person reads first, so it must answer three "
        "questions in everyday English:\n"
        f"1. What will move: how many commits {source} has that {target} does "
        "not, with the commit subjects, and the files they touch.\n"
        f"2. What happens on GitHub: {target} will receive one merge commit "
        f"carrying {source}'s work, and that commit will be pushed straight to "
        f"{target} with no pull request and no review step.\n"
        "3. The risks: the merge may conflict and need a person; a direct push "
        "changes the branch everyone else builds on; anything already on the "
        "target that clashes with the source will be visible in the merge "
        "result. Say plainly which of these apply, and say when you see no "
        "risk at all rather than inventing one.\n\n"
        f"Then write the plan to {DRAFT_FILE}. Its first line is the title: "
        f"“Bring {target} up to date from {source}”. Everything after the first "
        "line is a short numbered list of what will happen, in order: fetch, "
        f"check out {target}, merge {source} into it, push the result — followed "
        "by the risks from point 3 in plain words, and what a person should do "
        "if the merge conflicts.\n\n"
        "Do not merge, commit, or push. The person reads the plan first and "
        "chooses whether it goes ahead."
    )


def validate(data) -> None:
    _check_branch_names(_values(data))


def _git_env() -> dict:
    return {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME") or "",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(args: list[str], cwd: str,
             timeout: float = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run one explicit `git` command inside the scratch checkout, no shell."""
    argv = ["git", *args]
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=_git_env())
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "git did not finish in time"
    except OSError as exc:
        return 127, "", str(exc)


def _worktree(context: dict, job) -> str:
    notes = getattr(job, "notes", None) or {}
    for candidate in (context.get("worktree"), notes.get("worktree_path"),
                      notes.get("worktree")):
        text = str(candidate or "").strip()
        if text and os.path.isdir(text):
            return text
    raise PreWriteRefusal("The scratch checkout for this run is gone, so no branch "
                     "was changed. Run the task again to build a fresh plan.")


def _failed(step: str, out: str, err: str, tail: str,
            error=PreWriteRefusal) -> ValueError:
    return error(f"The {step} did not go through (git said: "
                      f"{_clip(err or out)}). {tail}")


def apply(context: dict, gh, job) -> dict:
    """Fetch, merge one branch into the other, and push the result — on request.

    Called only from the person's explicit Post action. The merge is a real
    merge commit (`--no-ff`) so history shows the combination; a conflict is
    undone immediately and left for a person. `gh` inspects repository
    automation before the push; this task makes no API writes.
    """
    context = context or {}
    ensure_not_stopped(context)
    values = _values(context, job)
    _check_branch_names(values, PreWriteRefusal)
    source, target = _name(values, "source"), _name(values, "target")
    worktree = _worktree(context, job)
    repository_policy(context, job)
    # The plan was written against this local view. Fetch into explicit remote
    # refs (also works with a bare-clone cache), then refuse a changed source.
    rc, out, _ = _run_git(['rev-parse', '--verify', f'{REMOTE}/{source}'], cwd=worktree)
    if rc:
        rc, out, _ = _run_git(['rev-parse', '--verify', f'refs/heads/{source}'], cwd=worktree)
    if rc or not out.strip():
        raise PreWriteRefusal('The source branch in this plan is unknown. Nothing was pushed; '
                              'run the task again to inspect the current branch.')
    planned_source = out.strip()

    rc, out, err = _run_git(["fetch", REMOTE,
                            f'+refs/heads/{source}:refs/remotes/{REMOTE}/{source}',
                            f'+refs/heads/{target}:refs/remotes/{REMOTE}/{target}'], cwd=worktree)
    if rc != 0:
        raise _failed("fetch from GitHub", out, err,
                      f"Nothing on {target} was changed.")

    source_sha = commit_id(_run_git, worktree, f'{REMOTE}/{source}')
    target_sha = commit_id(_run_git, worktree, f'{REMOTE}/{target}')
    if planned_source != source_sha:
        raise PreWriteRefusal('The source branch moved since the plan was written. '
                              'Nothing was pushed; run the task again to review the new commits.')
    rc, out, err = _run_git(["checkout", "--detach", target_sha],
                            cwd=worktree)
    if rc != 0:
        raise _failed(f"checkout of {target}", out, err,
                      f"Nothing was merged and nothing was pushed. Check that "
                      f"{target} exists on GitHub.")

    rc, out, err = _run_git(
        [*IDENTITY, "merge", "--no-ff", source_sha, "-m",
         f"merge {source} into {target}"], cwd=worktree)
    if rc != 0:
        _run_git(["merge", "--abort"], cwd=worktree)
        said = f"{err}\n{out}"
        if rc == 1 or "CONFLICT" in said or "Automatic merge failed" in said:
            raise PreWriteRefusal(CONFLICT_MESSAGE)
        raise _failed(f"merge of {source} into {target}", out, err,
                      f"The merge was undone. Nothing was pushed, so {target} "
                      "on GitHub is unchanged.")

    tree = verify_before_write(context, gh, job, worktree, _run_git, [source, target])
    require_remote_refs(_run_git, worktree, {f'refs/heads/{source}': source_sha,
                                            f'refs/heads/{target}': target_sha})
    tested = ensure_prepared_tree(_run_git, worktree, tree)
    ensure_not_stopped(context)
    rc, out, err = _run_git(["push", REMOTE, f"{tested}:refs/heads/{target}"],
                            cwd=worktree)
    if rc != 0:
        raise _failed(f"push of {target}", out, err,
                      f"The push outcome is uncertain. Check {target} on GitHub "
                      "before starting another task; GitHub may have received "
                      "the merge before the connection failed.", error=ValueError)

    return {"result": f"Moved {source} into {target} and pushed."}


TASK = TaskType(
    id="sync-branch",
    label="Bring a branch up to date",
    profile="write",
    takes="repo",
    post_confirm_required=True,
    help=("Shows exactly which commits one branch would bring into another, "
          "then asks for your explicit confirmation before merging and pushing — and "
          "stops for a person when the branches conflict."),
    fields={
        "source": {"label": "Branch to take changes from", "required": True},
        "target": {"label": "Branch to bring up to date", "required": True},
    },
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
