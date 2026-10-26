"""Git worktree management for gha-worker.

Plain functions over `git` subprocess calls. Every command is an argv list
(never a shell string), runs with an explicit timeout, and worktree
operations carry -c protocol.file.allow=always so local path remotes work.
"""
from __future__ import annotations

import os
import subprocess

_DEFAULT_TIMEOUT = 120.0


class GitError(Exception):
    """A git command failed (non-zero exit, timeout, or missing binary)."""


def _run_git(cwd: str | None, args: list[str], timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    argv = ["git"]
    if cwd is not None:
        argv += ["-C", cwd]
    argv += args
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timed out after {timeout}s: {args}") from exc
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    except OSError as exc:
        raise GitError(f"git could not run: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    return proc


def _file_protocol_args() -> list[str]:
    return ["-c", "protocol.file.allow=always"]


def add_worktree(
    repo_path: str,
    dest_path: str,
    ref: str | None = None,
    new_branch: str | None = None,
    detach: bool = False,
) -> str:
    """Create a worktree at dest_path from repo_path.

    Variants: detach=True checks out ref (or HEAD) detached; new_branch=name
    creates a local branch name at ref (or HEAD) and checks it out; a bare ref
    checks out that ref as git chooses (branch or detached).
    Returns the destination path.
    """
    if new_branch and detach:
        raise ValueError("new_branch and detach are mutually exclusive")
    args = _file_protocol_args() + ["worktree", "add"]
    if detach:
        args.append("--detach")
    if new_branch:
        args += ["-b", new_branch]
    args.append(os.path.abspath(dest_path))
    if ref:
        args.append(ref)
    _run_git(repo_path, args)
    return os.path.abspath(dest_path)


def fetch_pr_worktree(
    repo_path: str,
    dest_path: str,
    pr_number: int,
    remote: str = "origin",
) -> str:
    """Fetch pull/<N>/head from the remote and create a worktree on local
    branch gha/pr-<N> at the fetched commit. Returns the destination path."""
    pr_number = int(pr_number)
    if pr_number <= 0:
        raise ValueError("pr_number must be a positive int")
    fetch_refspec = f"refs/pull/{pr_number}/head:refs/remotes/{remote}/pr-{pr_number}"
    _run_git(repo_path, _file_protocol_args() + ["fetch", "--no-tags", remote, fetch_refspec], timeout=600.0)
    fetched_ref = f"refs/remotes/{remote}/pr-{pr_number}"
    add_worktree(repo_path, dest_path, ref=fetched_ref, new_branch=f"gha/pr-{pr_number}")
    return os.path.abspath(dest_path)


def commit_all(
    worktree_path: str,
    message: str,
    author_name: str,
    author_email: str,
) -> str:
    """Stage everything and commit with the given author. Returns the new sha.

    When there is nothing to commit, returns the current HEAD sha instead.
    """
    _run_git(worktree_path, ["add", "-A"])
    args = [
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "-m",
        message,
    ]
    try:
        _run_git(worktree_path, args)
    except GitError as exc:
        if "nothing to commit" in (exc.args[0] if exc.args else ""):
            return current_sha(worktree_path)
        raise
    # commit output only carries an abbreviated sha — resolve the real one.
    return current_sha(worktree_path)


def is_clean(worktree_path: str) -> bool:
    proc = _run_git(worktree_path, ["status", "--porcelain"])
    return proc.stdout.strip() == ""


def current_sha(worktree_path: str) -> str:
    proc = _run_git(worktree_path, ["rev-parse", "HEAD"])
    return proc.stdout.strip()


def remove_worktree(repo_path: str, dest_path: str, force: bool = False) -> None:
    args = _file_protocol_args() + ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(os.path.abspath(dest_path))
    _run_git(repo_path, args)


def prune(repo_path: str) -> None:
    _run_git(repo_path, _file_protocol_args() + ["worktree", "prune"])
