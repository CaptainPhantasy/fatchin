"""Tests for the wiring adapters (stdlib only)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ghagent.interfaces import Config
from ghagent.wiring import EnvKeyBearer, WorktreeOpsAdapter


def sh(argv, cwd=None):
    rc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if rc.returncode != 0:
        raise RuntimeError(f"{argv}: {rc.stderr[:200]}")
    return rc.stdout.strip()


class CommitAllArtifactTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        sh(["git", "init", "-q", "-b", "main", str(self.wt)])
        sh(["git", "-C", str(self.wt), "config", "user.name", "t"])
        sh(["git", "-C", str(self.wt), "config", "user.email", "t@local"])

    def adapter(self) -> WorktreeOpsAdapter:
        return WorktreeOpsAdapter(Config(state_dir=str(self.tmp),
                                         worktree_root=str(self.tmp)))

    def test_commit_all_excludes_build_artifacts(self):
        (self.wt / "real.py").write_text("x = 1\n")
        cache = self.wt / "__pycache__"
        cache.mkdir()
        (cache / "real.cpython-314.pyc").write_bytes(b"\x00\x01binary")
        (self.wt / ".pytest_cache").mkdir()
        (self.wt / ".pytest_cache" / "CACHEDIR.TAG").write_text("tag\n")

        sha = self.adapter().commit_all(str(self.wt), "worker fix")
        self.assertTrue(sha)
        names = sh(["git", "-C", str(self.wt), "show", "--name-only",
                    "--format=", "HEAD"]).splitlines()
        self.assertIn("real.py", names)
        self.assertNotIn("__pycache__/real.cpython-314.pyc", names)
        self.assertNotIn(".pytest_cache/CACHEDIR.TAG", names)
        # unstaged, not deleted: the working tree keeps the artifact file
        self.assertTrue((cache / "real.cpython-314.pyc").exists())

    def test_commit_all_nothing_to_commit_returns_empty(self):
        (self.wt / "base.txt").write_text("seed\n")
        sh(["git", "-C", str(self.wt), "add", "-A"])
        sh(["git", "-C", str(self.wt), "commit", "-q", "-m", "seed"])
        self.assertEqual(self.adapter().commit_all(str(self.wt), "empty"), "")

    def test_current_sha_and_is_clean(self):
        (self.wt / "a.txt").write_text("a\n")
        self.assertFalse(self.adapter().is_clean(str(self.wt)))
        sha = self.adapter().commit_all(str(self.wt), "one")
        self.assertTrue(self.adapter().is_clean(str(self.wt)))
        self.assertEqual(self.adapter().current_sha(str(self.wt)), sha)


class EnvKeyBearerTest(unittest.TestCase):
    def test_missing_key_raises_named_error(self):
        bearer = EnvKeyBearer({})
        with self.assertRaises(RuntimeError) as ctx:
            bearer()
        self.assertIn("GHA_MODEL_API_KEY", str(ctx.exception))

    def test_present_key_returned_without_logging(self):
        bearer = EnvKeyBearer({"GHA_MODEL_API_KEY": "sk-test"})
        self.assertEqual(bearer(), "sk-test")


class BareCacheRefreshTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / 'remote'
        sh(['git', 'init', '-q', '-b', 'main', str(self.remote)])
        sh(['git', '-C', str(self.remote), 'config', 'user.name', 'Fixture'])
        sh(['git', '-C', str(self.remote), 'config', 'user.email', 'fixture@local'])
        self.commit('first')
        remote = self.remote

        class LocalAdapter(WorktreeOpsAdapter):
            def _git(self, argv, cwd=None, timeout=300):
                if argv[:2] == ['clone', '--bare']:
                    argv = [*argv[:2], str(remote), argv[-1]]
                return super()._git(argv, cwd=cwd, timeout=timeout)

        self.adapter = LocalAdapter(Config(state_dir=str(self.root / 'state'),
                                           worktree_root=str(self.root / 'scratch')))

    def commit(self, text):
        (self.remote / 'version.txt').write_text(text)
        sh(['git', '-C', str(self.remote), 'add', 'version.txt'])
        sh(['git', '-C', str(self.remote), 'commit', '-q', '-m', text])

    def test_next_task_uses_new_remote_commit_and_prunes_deleted_branch(self):
        sh(['git', '-C', str(self.remote), 'branch', 'retired'])
        first = self.adapter.add_worktree(str(self.root / 'first'), 'main', 'owner/repo')
        self.assertEqual((Path(first) / 'version.txt').read_text(), 'first')
        self.commit('second')
        sh(['git', '-C', str(self.remote), 'branch', '-D', 'retired'])
        second = self.adapter.add_worktree(str(self.root / 'second'), 'main', 'owner/repo')
        self.assertEqual((Path(second) / 'version.txt').read_text(), 'second')
        cache = self.adapter._clone_dir('owner/repo')
        refs = sh(['git', '-C', str(cache), 'for-each-ref', '--format=%(refname)', 'refs/heads'])
        self.assertNotIn('refs/heads/retired', refs)

    def test_failed_refresh_refuses_a_task_instead_of_using_stale_code(self):
        self.adapter.add_worktree(str(self.root / 'first'), 'main', 'owner/repo')
        self.remote.rename(self.root / 'unavailable-remote')
        with self.assertRaisesRegex(RuntimeError, 'fetch .*failed'):
            self.adapter.add_worktree(str(self.root / 'second'), 'main', 'owner/repo')
        self.assertFalse((self.root / 'second').exists())


if __name__ == "__main__":
    unittest.main()
