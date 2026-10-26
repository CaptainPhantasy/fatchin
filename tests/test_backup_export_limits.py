"""Real-git backup export boundaries, without remote or model requests."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ghagent.executor import JobExecutor
from ghagent.interfaces import Job, JobKind
from ghagent.results import export_worktree
from tests import test_executor_flow as flow


class BackupExportLimitsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.worktree = self.home / 'checkout'
        self.worktree.mkdir()
        self.output = self.home / 'results'
        self.output.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-qm', 'initial')
        self.job = Job('job_backup_limits', JobKind.BACKUP_REPO,
                       'owner/repo', 0, 0, 1,
                       notes={'output_dir': str(self.output),
                              'member_home': str(self.home),
                              'changed_files': []})
        self.artifact = self.output / 'gha-worker-job_backup_limits'

    def git(self, *args):
        return subprocess.run(['git', *map(str, args)], cwd=self.worktree,
                              check=True, capture_output=True).stdout

    def sized_file(self, name, size):
        path = self.worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as handle:
            handle.truncate(size)
        return path

    def test_eleven_mb_backup_is_exported(self):
        name = 'backup/repo-20261025.bundle'
        self.sized_file(name, 11_000_000)
        export_worktree(self.job, self.worktree)
        self.assertEqual((self.artifact / 'files' / name).stat().st_size,
                         11_000_000)
        self.assertEqual(self.job.notes['exported_files'], [name])

    def test_backup_at_fifty_mb_boundary_is_exported(self):
        name = 'backup/repo-20261025.bundle'
        self.sized_file(name, 50_000_000)
        export_worktree(self.job, self.worktree)
        self.assertEqual((self.artifact / 'files' / name).stat().st_size,
                         50_000_000)

    def test_oversized_backup_keeps_scratch_for_recovery(self):
        name = 'backup/repo-20261025.bundle'
        path = self.sized_file(name, 50_000_001)
        ops = flow.FakeWT()
        runner = JobExecutor(flow.happy_gh(), flow.FakeQueue(self.job),
                             flow.default_cfg(), flow.FakeAgent(None),
                             worktree_ops=ops)
        runner._cleanup_worktree(self.job, str(self.worktree))
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, 50_000_001)
        self.assertEqual(self.job.notes['export_skipped_files'], [name])
        self.assertIn('Scratch was retained',
                      self.job.notes['result_export_error'])
        self.assertIn('Scratch retained', self.job.notes['worktree_cleanup'])
        self.assertFalse((self.artifact / 'files' / name).exists())
        self.assertFalse(ops.calls)

    def test_multiple_backups_still_share_fifty_mb_total(self):
        first, second = ('backup/repo-20261024.bundle',
                         'backup/repo-20261025.bundle')
        self.sized_file(first, 26_000_000)
        self.sized_file(second, 25_000_000)
        with self.assertRaisesRegex(RuntimeError, 'Scratch was retained'):
            export_worktree(self.job, self.worktree)
        self.assertEqual(self.job.notes['exported_files'], [first])
        self.assertEqual(self.job.notes['export_skipped_files'], [second])
        self.assertTrue((self.artifact / 'files' / first).is_file())
        self.assertFalse((self.artifact / 'files' / second).exists())
        self.assertTrue((self.worktree / second).is_file())

    def test_ordinary_artifacts_keep_ten_mb_limit(self):
        name = 'backup/repo-20261025.bundle'
        self.sized_file(name, 10_000_001)
        self.job.kind = JobKind.FIX_PR
        with self.assertRaisesRegex(RuntimeError, 'Scratch was retained'):
            export_worktree(self.job, self.worktree)
        self.assertEqual(self.job.notes['exported_files'], [])
        self.assertFalse((self.artifact / 'files' / name).exists())

    def test_non_bundle_backup_artifacts_keep_ten_mb_limit(self):
        name = 'backup/notes.txt'
        self.sized_file(name, 10_000_001)
        with self.assertRaisesRegex(RuntimeError, 'Scratch was retained'):
            export_worktree(self.job, self.worktree)
        self.assertEqual(self.job.notes['exported_files'], [])
        self.assertFalse((self.artifact / 'files' / name).exists())

    def test_bundle_names_outside_the_backup_contract_keep_ten_mb_limit(self):
        names = ['repo-20261025.bundle', 'backup/other.bundle',
                 'backup/nested/repo-20261025.bundle']
        for name in names:
            self.sized_file(name, 10_000_001)
        with self.assertRaisesRegex(RuntimeError, 'Scratch was retained'):
            export_worktree(self.job, self.worktree)
        self.assertEqual(self.job.notes['exported_files'], [])
        self.assertCountEqual(self.job.notes['export_skipped_files'], names)

    def test_hidden_files_and_symlink_bundles_stay_excluded(self):
        hidden = 'backup/.private'
        self.sized_file(hidden, 1)
        outside = self.home / 'outside.bundle'
        outside.write_bytes(b'private fixture data')
        name = 'backup/repo-20261025.bundle'
        (self.worktree / name).symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'Scratch was retained'):
            export_worktree(self.job, self.worktree)
        self.assertEqual(self.job.notes['exported_files'], [])
        self.assertCountEqual(self.job.notes['export_skipped_files'],
                              [hidden, name])
        self.assertFalse((self.artifact / 'files' / hidden).exists())
        self.assertFalse((self.artifact / 'files' / name).exists())
        self.assertEqual(outside.read_bytes(), b'private fixture data')

    def test_real_large_bundle_can_be_restored_after_export(self):
        content = os.urandom(11_000_000)
        (self.worktree / 'asset.bin').write_bytes(content)
        self.git('add', 'asset.bin')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'include fixture asset')
        original_head = self.git('rev-parse', 'HEAD')
        name = 'backup/repo-20261025.bundle'
        (self.worktree / 'backup').mkdir()
        self.git('bundle', 'create', name, '--all')
        self.assertGreater((self.worktree / name).stat().st_size, 10_000_000)
        export_worktree(self.job, self.worktree)
        exported = self.artifact / 'files' / name
        self.git('bundle', 'verify', exported)
        restored = self.home / 'restored'
        self.git('clone', '-q', exported, restored)
        restored_head = subprocess.run(['git', 'rev-parse', 'HEAD'],
                                       cwd=restored, check=True,
                                       capture_output=True).stdout
        self.assertEqual(restored_head, original_head)
        self.assertEqual(hashlib.sha256((restored / 'asset.bin').read_bytes()).digest(),
                         hashlib.sha256(content).digest())


if __name__ == '__main__':
    unittest.main()
