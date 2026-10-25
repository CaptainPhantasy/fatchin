"""Real Git evidence for the commit and files an executor is allowed to publish."""
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from ghagent import executor
from ghagent.executor import JobExecutor
from ghagent.interfaces import AgentResult, Config, Job, JobKind, JobState, RepoConfig
from ghagent.wiring import WorktreeOpsAdapter


class Queue:
    def __init__(self, job):
        self.job = job
    def get(self, job_id):
        return self.job
    def update(self, job):
        self.job = job


class ExecutorReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.checkout = self.root / 'scratch' / 'fix-pr-job_receipt'
        self.checkout.mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.com')
        (self.checkout / 'changed.txt').write_text('before\n')
        (self.checkout / 'unrelated.txt').write_text('unchanged\n')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'baseline')
        self.baseline = self.git('rev-parse', 'HEAD')
        self.repo = RepoConfig('o', 'r', checks=[self.command(
            "from pathlib import Path; assert Path('changed.txt').read_text() == 'after\\n'")])
        self.cfg = Config(state_dir=str(self.root / 'state'),
                          worktree_root=str(self.root / 'scratch'), repos=[self.repo])
        self.job = Job('job_receipt', JobKind.FIX_PR, 'o/r', 1, 0, 1,
                       head_sha=self.baseline, started_commit=self.baseline)
        self.exe = JobExecutor(None, Queue(self.job), self.cfg, None,
                               WorktreeOpsAdapter(self.cfg))
        self.result = AgentResult('claimed success', ['unrelated.txt'], str(self.checkout),
                                  final_commit_sha=self.baseline,
                                  checks=[{'command': 'invented check', 'passed': True}])

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.checkout,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def command(self, code):
        return shlex.join([sys.executable, '-c', code])

    def test_tested_commit_is_actual_candidate_and_paths_are_git_evidence(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        self.assertIsNotNone(verified, self.job.error)
        candidate = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(candidate, self.baseline)
        self.assertEqual(verified[2], candidate)
        self.assertEqual(self.job.notes['tested_commit'], candidate)
        self.assertEqual(self.job.notes['changed_files'], ['changed.txt'])
        self.assertEqual(self.job.notes['checks'][0]['command'], self.repo.checks[0])

    def test_published_summary_and_remote_use_actual_tested_candidate(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        bare = self.root / 'origin.git'
        self.git('init', '--bare', str(bare))
        self.git('remote', 'add', 'origin', str(bare))
        self.exe.gh = SimpleNamespace(pr_view=lambda *args: {
            'state': 'OPEN', 'headRefOid': self.baseline})
        with patch.object(executor.triggers, 'preflight', return_value=
                          executor.triggers.TriggerReport(executor.triggers.SAFE)), \
                patch.object(executor.responder, 'post_pr_comment', return_value=91) as post, \
                patch.object(self.exe, '_cancel_copilot_runs'), \
                patch.object(self.exe, '_cleanup_worktree'):
            self.exe._responding(self.job, {'pr': {'headRefName': 'correction'}},
                                 self.result, str(self.checkout), *verified)
        self.assertEqual(self.job.state, JobState.DONE, self.job.error)
        self.assertEqual(self.git('rev-parse', 'HEAD'), verified[2])
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/correction').split()[0], verified[2])
        self.assertIn('Tested commit: ' + verified[2], post.call_args.args[3])
        self.assertNotIn('Tested commit: ' + self.baseline, post.call_args.args[3])

    def test_changed_after_checks_is_held_before_push(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        (self.checkout / 'changed.txt').write_text('later unverified change\n')
        self.exe.gh = SimpleNamespace(pr_view=lambda *args: {
            'state': 'OPEN', 'headRefOid': self.baseline})
        with patch.object(executor.triggers, 'preflight', return_value=
                          executor.triggers.TriggerReport(executor.triggers.SAFE)), \
                patch.object(executor.responder, 'post_pr_comment') as post:
            self.exe._responding(self.job, {'pr': {'headRefName': 'correction'}},
                                 self.result, str(self.checkout), *verified)
        self.assertEqual(self.job.state, JobState.FAILED)
        self.assertIn('verified candidate changed', self.job.error)
        self.assertNotIn('publish_outcome', self.job.notes)
        post.assert_not_called()

    def test_timed_out_push_with_unreadable_remote_retains_recovery_checkout(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        def unavailable(*args, **kwargs):
            raise ValueError('remote read unavailable')
        self.exe.gh = SimpleNamespace(pr_view=lambda *args: {
            'state': 'OPEN', 'headRefOid': self.baseline}, rest=unavailable)
        local_git = executor._run_git
        def timed_push(args, cwd, timeout=600):
            if args[0] == 'push':
                return 124, '', 'timed out'
            return local_git(args, cwd, timeout)
        self.job.notes['worktree_path'] = str(self.checkout)
        with patch.object(executor.triggers, 'preflight', return_value=
                          executor.triggers.TriggerReport(executor.triggers.SAFE)), \
                patch.object(executor, '_run_git', side_effect=timed_push):
            self.exe._responding(self.job, {'pr': {'headRefName': 'correction'}},
                                 self.result, str(self.checkout), *verified)
        self.assertEqual(self.job.state, JobState.FAILED)
        self.assertEqual(self.job.notes['publish_outcome'], 'uncertain')
        self.assertEqual(self.job.notes['publish_branch'], 'correction')
        self.exe._cleanup_worktree(self.job, str(self.checkout))
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.job.notes['tested_commit'], verified[2])

    def test_timed_out_push_reconciles_exact_remote_commit_without_retry(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        self.git('init', '--bare', str(self.root / 'origin.git'))
        self.git('remote', 'add', 'origin', str(self.root / 'origin.git'))
        self.exe.gh = SimpleNamespace(pr_view=lambda *args: {
            'state': 'OPEN', 'headRefOid': self.baseline},
            rest=lambda *args, **kwargs: {'commit': {'sha': verified[2]}})
        local_git = executor._run_git
        pushes = []
        def timed_after_landing(args, cwd, timeout=600):
            outcome = local_git(args, cwd, timeout)
            if args[0] == 'push':
                pushes.append(args)
                self.assertEqual(outcome[0], 0, outcome[2])
                return 124, '', 'response timed out after remote accepted commit'
            return outcome
        with patch.object(executor.triggers, 'preflight', return_value=
                          executor.triggers.TriggerReport(executor.triggers.SAFE)), \
                patch.object(executor.responder, 'post_pr_comment', return_value=91), \
                patch.object(executor, '_run_git', side_effect=timed_after_landing), \
                patch.object(self.exe, '_cancel_copilot_runs'), \
                patch.object(self.exe, '_cleanup_worktree'):
            self.exe._responding(self.job, {'pr': {'headRefName': 'correction'}},
                                 self.result, str(self.checkout), *verified)
        self.assertEqual(self.job.state, JobState.DONE, self.job.error)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.job.notes['publish_outcome'], 'confirmed')
        self.assertEqual(self.job.produced_commits, [verified[2]])

    def test_unrelated_self_reported_file_cannot_resolve_thread(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        thread = {'id': 'thread-unrelated', '_root_comment_id': 44,
                  'path': 'unrelated.txt', 'viewerCanResolve': True}
        with patch.object(executor.responder, 'reply_inline', return_value=91), \
                patch.object(executor.responder, 'resolve_thread') as resolve:
            self.exe._respond_threads(self.job, {'threads': [thread]}, self.result,
                                      verified[2], True, [])
        resolve.assert_not_called()
        self.assertEqual(self.job.resolved_thread_ids, [])

    def test_fabricated_agent_success_cannot_override_failing_configured_check(self):
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        self.assertIsNone(verified)
        self.assertEqual(self.job.state, JobState.FAILED)
        self.assertFalse(self.job.notes['checks'][0]['passed'])

    def test_check_mutating_prepared_files_invalidates_evidence(self):
        (self.checkout / 'changed.txt').write_text('after\n')
        self.repo.checks = [self.command("from pathlib import Path; Path('changed.txt').write_text('unverified\\n')")]
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        self.assertIsNone(verified)
        self.assertEqual(self.job.state, JobState.FAILED)
        self.assertIn('could not be verified', self.job.error.lower())

    def test_no_configured_checks_never_claims_agent_checks_as_passing(self):
        self.repo.checks = []
        (self.checkout / 'changed.txt').write_text('after\n')
        verified = self.exe._verifying(self.job, self.result, str(self.checkout))
        self.assertIsNone(verified[0])
        self.assertEqual(self.job.notes['checks'], [])

    def test_uncertain_publish_keeps_checkout_for_reconciliation(self):
        self.job.notes.update(worktree_path=str(self.checkout), publish_outcome='uncertain')
        self.exe._cleanup_worktree(self.job, str(self.checkout))
        self.assertTrue(self.checkout.is_dir())
        self.assertIn('uncertain', self.job.notes['worktree_cleanup'].lower())

    def test_standalone_task_checkout_cleanup_uses_only_its_owned_scratch_path(self):
        self.job.kind = JobKind.NEW_PROJECT
        expected = self.checkout.parent / 'new-project-job_receipt'
        self.checkout.rename(expected)
        self.job.notes.update(worktree_path=str(expected), scratch_kind='standalone')
        self.exe._cleanup_worktree(self.job, str(expected))
        self.assertFalse(expected.exists())
        self.assertNotIn('worktree_path', self.job.notes)

    def test_sync_plan_is_prepared_and_saved_before_agent_instruction(self):
        from ghagent.tasks import sync_branch
        self.job.kind = JobKind.SYNC_BRANCH
        draft = self.checkout / '.gha-drafts'
        draft.mkdir()
        (draft / 'plan.md').write_text('Plan\nInspect the pinned branches.\n')
        self.exe._worktree_ops = SimpleNamespace(add_worktree=lambda *args: str(self.checkout))
        events = []
        def prepare(context, gh, job):
            events.append('prepare')
            job.notes['planned_refs'] = {'refs/heads/main': self.baseline}
        persist = self.exe._persist
        def saved(job, state=None, error=''):
            if 'planned_refs' in job.notes:
                events.append('saved')
            persist(job, state, error)
        def instruction(context):
            self.assertIn('saved', events)
            events.append('instruction')
            return 'Explain pinned commits'
        task = SimpleNamespace(id='sync-branch', takes='repo', build_instruction=instruction)
        with patch.object(sync_branch, 'prepare_plan', side_effect=prepare, create=True), \
                patch.object(self.exe, '_persist', side_effect=saved), \
                patch.object(self.exe, '_run_agent', return_value=self.result):
            self.exe._execute_drafts(self.job, task)
        self.assertEqual(self.job.state, JobState.WAITING_ON_YOU, self.job.error)
        self.assertLess(events.index('prepare'), events.index('saved'))
        self.assertLess(events.index('saved'), events.index('instruction'))

    def test_sync_plan_preparation_failure_stops_before_agent(self):
        from ghagent.tasks import sync_branch
        self.job.kind = JobKind.SYNC_BRANCH
        self.exe._worktree_ops = SimpleNamespace(add_worktree=lambda *args: str(self.checkout))
        task = SimpleNamespace(id='sync-branch', takes='repo', build_instruction=lambda _: 'must not build')
        with patch.object(sync_branch, 'prepare_plan', side_effect=ValueError('branch unavailable'), create=True), \
                patch.object(self.exe, '_run_agent') as run:
            self.exe._execute_drafts(self.job, task)
        self.assertEqual(self.job.state, JobState.FAILED)
        self.assertIn('Branch plan preparation failed', self.job.error)
        run.assert_not_called()

    def test_standalone_cleanup_refuses_an_unrelated_directory(self):
        self.job.kind = JobKind.NEW_PROJECT
        self.job.notes.update(worktree_path=str(self.checkout), scratch_kind='standalone')
        self.exe._cleanup_worktree(self.job, str(self.checkout))
        self.assertTrue(self.checkout.exists())
        self.assertIn('worktree_cleanup', self.job.notes)


if __name__ == '__main__':
    unittest.main()
