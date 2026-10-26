"""Draft collection must not read files outside the task worktree.

The coding agent can run commands that create symbolic links inside its
checkout. A draft read through a link would put a local file's contents into
a job record and a GitHub comment.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ghagent.executor import JobExecutor


class DraftCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worktree = self.root / 'checkout'
        self.worktree.mkdir()
        self.drafts = self.worktree / '.gha-drafts'
        self.drafts.mkdir()

    def test_regular_draft_is_collected(self):
        (self.drafts / 'plan.md').write_text('Plan\nDo the work')
        collected = JobExecutor._collect_drafts(self.worktree)
        self.assertEqual([d['name'] for d in collected], ['plan'])
        self.assertEqual(collected[0]['title'], 'Plan')

    def test_symlinked_draft_file_is_ignored(self):
        secret = self.root / 'outside.md'
        secret.write_text('PRIVATE KEY MATERIAL')
        (self.drafts / 'plan.md').write_text('Plan\nSafe')
        (self.drafts / 'leak.md').symlink_to(secret)
        collected = JobExecutor._collect_drafts(self.worktree)
        self.assertEqual([d['name'] for d in collected], ['plan'])

    def test_symlinked_draft_folder_is_ignored(self):
        outside = self.root / 'elsewhere'
        outside.mkdir()
        (outside / 'plan.md').write_text('Plan\nOutside')
        self.drafts.rmdir()
        self.drafts.symlink_to(outside)
        self.assertEqual(JobExecutor._collect_drafts(self.worktree), [])


if __name__ == '__main__':
    unittest.main()
