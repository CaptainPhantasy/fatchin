"""Tests for ghagent.worktree — real git operations in a tempfile fixture.

Builds a real repository (git init + initial commit) and a bare remote with a
refs/pull/N/head ref; everything stays on local disk.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ghagent.worktree import (
    GitError,
    add_worktree,
    commit_all,
    current_sha,
    fetch_pr_worktree,
    is_clean,
    prune,
    remove_worktree,
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, check=check,
    )


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="ghagent-worktree-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        self.repo = os.path.join(base, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "user.email", "test@example.com")
        with open(os.path.join(self.repo, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("fixture repo\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-m", "init")
        self.first_sha = current_sha(self.repo)

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    # -- add / commit / is_clean / remove / prune ---------------------------------

    def test_add_worktree_branch_commit_remove_prune(self):
        wt = os.path.join(self.base, "wt1")
        returned = add_worktree(self.repo, wt, new_branch="feature")
        self.assertEqual(returned, wt)
        self.assertTrue(os.path.isdir(wt))
        head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(head, "feature")
        self.assertTrue(is_clean(wt))
        self.assertEqual(current_sha(wt), self.first_sha)

        self._write(os.path.join(wt, "README.md"), "changed in worktree\n")
        self.assertFalse(is_clean(wt))
        sha = commit_all(wt, "fix: adjust wording", "A U Thor", "author@example.com")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(sha, current_sha(wt))
        self.assertTrue(is_clean(wt))

        # Nothing to commit: returns the current sha instead of failing.
        self.assertEqual(commit_all(wt, "empty commit", "A U Thor", "author@example.com"), sha)

        remove_worktree(self.repo, wt)
        self.assertFalse(os.path.exists(wt))
        prune(self.repo)  # must be a clean no-op after removal
        # The feature branch survives in the repo.
        branches = _git(self.repo, "branch", "--list", "feature").stdout
        self.assertIn("feature", branches)

    def test_add_worktree_detached(self):
        wt = os.path.join(self.base, "wt2")
        add_worktree(self.repo, wt, detach=True)
        head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(head, "HEAD")  # detached
        self.assertEqual(current_sha(wt), self.first_sha)
        remove_worktree(self.repo, wt)

    def test_add_worktree_new_branch_at_explicit_ref(self):
        wt = os.path.join(self.base, "wt3")
        add_worktree(self.repo, wt, ref=self.first_sha, new_branch="topic")
        self.assertEqual(current_sha(wt), self.first_sha)
        head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(head, "topic")
        remove_worktree(self.repo, wt)

    def test_add_worktree_rejects_conflicting_options(self):
        with self.assertRaises(ValueError):
            add_worktree(self.repo, os.path.join(self.base, "wt4"),
                         new_branch="b", detach=True)

    def test_remove_dirty_worktree_requires_force(self):
        wt = os.path.join(self.base, "wt5")
        add_worktree(self.repo, wt, new_branch="dirty")
        self._write(os.path.join(wt, "README.md"), "uncommitted\n")
        with self.assertRaises(GitError):
            remove_worktree(self.repo, wt)
        self.assertTrue(os.path.isdir(wt))
        remove_worktree(self.repo, wt, force=True)
        self.assertFalse(os.path.exists(wt))

    # -- fetch_pr_worktree ------------------------------------------------------------

    def test_fetch_pr_worktree_from_local_remote(self):
        remote = os.path.join(self.base, "remote.git")
        os.makedirs(remote)
        _git(remote, "init", "--bare")
        _git(self.repo, "remote", "add", "origin", remote)
        _git(self.repo, "push", "origin", "main")

        # Second commit becomes the "PR head".
        self._write(os.path.join(self.repo, "README.md"), "PR changes\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-m", "pr work")
        pr_head = current_sha(self.repo)
        # The PR head must exist in the remote before refs/pull/42/head can
        # point at it.
        _git(self.repo, "push", "origin", "main")
        _git(remote, "update-ref", "refs/pull/42/head", pr_head)

        wt = os.path.join(self.base, "pr42")
        fetch_pr_worktree(self.repo, wt, 42)
        self.assertTrue(os.path.isdir(wt))
        head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(head, "gha/pr-42")
        self.assertEqual(current_sha(wt), pr_head)
        remove_worktree(self.repo, wt)

    def test_fetch_pr_worktree_rejects_bad_pr_number(self):
        with self.assertRaises(ValueError):
            fetch_pr_worktree(self.repo, os.path.join(self.base, "nope"), 0)

    # -- error paths ---------------------------------------------------------------------

    def test_commit_all_on_non_repo_raises_git_error(self):
        not_a_repo = os.path.join(self.base, "not-a-repo")
        os.makedirs(not_a_repo)
        with self.assertRaises(GitError):
            commit_all(not_a_repo, "message", "A U Thor", "author@example.com")

    def test_is_clean_on_non_repo_raises_git_error(self):
        with self.assertRaises(GitError):
            is_clean(self.base)  # base dir is not a repository

    def test_current_sha_on_non_repo_raises_git_error(self):
        with self.assertRaises(GitError):
            current_sha(self.base)


if __name__ == "__main__":
    unittest.main()
