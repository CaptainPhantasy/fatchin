"""Worker-created dependency directories are not task results.

Task checkouts are linked worktrees, so `<checkout>/.git` is a file. The
dependency preparation step must still find the real ignore file, and result
export must never turn its own artifacts into a failed task.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ghagent.fleet_checks import _exclude_path, _prepare
from ghagent.interfaces import Job, JobKind
from ghagent.config_loader import Config
from ghagent.results import export_worktree


class LinkedCheckoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.scratch = self.home / 'worktrees'
        self.output = self.home / 'results'
        self.output.mkdir()
        seed = self.home / 'seed'
        seed.mkdir()
        self.git('init', '-q', '-b', 'main', cwd=seed)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-qm', 'fixture history', cwd=seed)
        self.cache = self.scratch / '_repos' / 'owner-repo.git'
        self.cache.parent.mkdir(parents=True)
        self.git('clone', '--bare', str(seed), str(self.cache), cwd=self.home)
        self.checkout = self.scratch / 'task'
        self.git('worktree', 'add', '--detach', str(self.checkout), 'main',
                 cwd=self.cache)

    def git(self, *args, cwd):
        return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout

    def job(self):
        return Job('job_export_artifacts', JobKind.BACKUP_REPO,
                   'owner/repo', 0, 0, 1,
                   notes={'output_dir': str(self.output),
                          'member_home': str(self.home),
                          'changed_files': []})

    def test_prepare_ignores_artifacts_in_the_common_git_directory(self):
        exclude = _exclude_path(self.checkout)
        self.assertIsNotNone(exclude)
        self.assertFalse(str(exclude).startswith(str(self.checkout / '.git')))
        self.assertTrue(exclude.is_relative_to(self.cache))
        problem = _prepare(Config(worktree_root=str(self.scratch)),
                           self.checkout, [], lambda: False)
        self.assertEqual(problem, '')
        (self.checkout / '.fatchin-dependencies').mkdir()
        (self.checkout / '.fatchin-dependencies' / 'package.py').write_text('x')
        ignored = subprocess.run(
            ['git', 'check-ignore', '-q', '.fatchin-dependencies/package.py'],
            cwd=self.checkout)
        self.assertEqual(ignored.returncode, 0)

    def test_export_copies_results_and_cleanly_skips_artifacts(self):
        (self.checkout / 'notes.txt').write_text('real result')
        for directory in ('node_modules', '.fatchin-dependencies',
                          '.fatchin-cargo', '.fatchin-go', '.gha-drafts'):
            (self.checkout / directory).mkdir()
            (self.checkout / directory / 'artifact.bin').write_bytes(b'x' * 1024)
        job = self.job()
        export_worktree(job, self.checkout)
        self.assertEqual(job.notes['exported_files'], ['notes.txt'])
        exported = self.output / 'gha-worker-job_export_artifacts' / 'files'
        self.assertEqual((exported / 'notes.txt').read_text(), 'real result')
        for directory in ('node_modules', '.fatchin-dependencies',
                          '.fatchin-cargo', '.fatchin-go', '.gha-drafts'):
            self.assertFalse((exported / directory).exists())
            self.assertTrue(any(name.startswith(directory + '/')
                                for name in job.notes.get('export_skipped_files', [])))


if __name__ == '__main__':
    unittest.main()
