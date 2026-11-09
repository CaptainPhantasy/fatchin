"""Waiting branch plans retain their exact reviewed versions across cache refreshes."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from ghagent.tasks import sync_branch
from ghagent.task_write_guard import PreWriteRefusal
from tests import test_task_write_guard as guard_fixture


class BranchPlanSnapshotTests(TestCase):
    def repository(self):
        fixture = guard_fixture.RealRepositoryTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_source_and_target_cannot_change_when_another_task_refreshes_cache(self):
        for branch in ('develop', 'main'):
            with self.subTest(branch=branch):
                r = self.repository()
                cache = r.root / 'cache.git'
                checkout = r.root / 'waiting-plan'
                r.git('clone', '--bare', str(r.remote), str(cache), cwd=r.root)
                r.git('worktree', 'add', '--detach', str(checkout), 'main', cwd=cache)
                context = r.context(worktree=str(checkout))
                job = r.job(sync_branch, accept=True)
                sync_branch.prepare_plan(context, r.gh, job)
                saved = dict(job.notes['planned_refs'])
                instruction = sync_branch.build_instruction(context)
                for version in saved.values():
                    self.assertIn(version, instruction)
                r.git('checkout', branch, cwd=r.seed)
                (r.seed / 'later.txt').write_text('Added after the plan\n')
                r.git('add', '.', cwd=r.seed)
                r.git('commit', '-m', 'unreviewed later commit', cwd=r.seed)
                r.git('push', 'origin', branch, cwd=r.seed)
                # A second task refreshes this shared bare cache while the plan waits.
                r.git('fetch', 'origin', '--prune', '+refs/heads/*:refs/heads/*', cwd=cache)
                self.assertNotEqual(r.git('rev-parse', branch, cwd=cache),
                                    saved[f'refs/heads/{branch}'])
                before = r.branches()
                with self.assertRaisesRegex(PreWriteRefusal, 'source or target branch moved'):
                    sync_branch.apply(context, r.gh, job)
                self.assertEqual(r.branches(), before)
                self.assertEqual(job.notes['planned_refs'], saved)

    def test_old_plan_without_saved_versions_cannot_fetch_or_merge(self):
        r = self.repository()
        job = r.job(sync_branch, accept=True)
        job.notes.pop('planned_refs', None)
        with patch.object(sync_branch, '_run_git') as git:
            with self.assertRaisesRegex(PreWriteRefusal, 'no saved source and target versions'):
                sync_branch.apply(r.context(), r.gh, job)
        git.assert_not_called()
