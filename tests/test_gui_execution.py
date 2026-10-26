"""Real git exports and cancellation boundaries without network or paid calls."""
import json
import subprocess
import tempfile
import unittest
import shutil
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ghagent.coding_agent import CodingAgent
from ghagent.interfaces import Config, Job, JobKind, JobState
from ghagent.queue import JobQueue
from ghagent.results import export_worktree
from ghagent.results import grant_member_access
from tests.test_coding_agent import FakeModel, resp, tool_call
from tests import test_executor_flow as flow
from ghagent import executor, responder
from ghagent.wiring import build_model_client, make_coding_agent_factory
from ghagent.model_client import SpendLimitExceeded


class GuiExecutionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wt = self.root / 'worktree'
        self.wt.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@local')
        (self.wt / 'code.py').write_text('answer = 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.start = self.git('rev-parse', 'HEAD')
        self.cfg = Config(state_dir=str(self.root / 'state'), worktree_root=str(self.wt))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()

    def task(self, stop):
        return {'instruction': 'Fix the answer.', 'worktree_path': str(self.wt),
                'checks': ['python3 -c "raise SystemExit(99)"'], 'should_stop': stop}

    def test_stop_before_request_uses_no_model_or_checks(self):
        model = FakeModel([])
        result = CodingAgent(model, self.cfg).run(self.task(lambda: True))
        self.assertEqual(model.calls, 0)
        self.assertIn('stopped', result.error.lower())
        self.assertEqual(result.checks, [])

    def test_stop_during_response_does_not_execute_returned_tools(self):
        stopped = False
        class Model:
            def chat(inner, messages, tools=None):
                nonlocal stopped
                stopped = True
                return resp(tool_call('write', 'write_file', {'path': 'code.py', 'content': 'overwritten'}))
        result = CodingAgent(Model(), self.cfg).run(self.task(lambda: stopped))
        self.assertEqual((self.wt / 'code.py').read_text(), 'answer = 1\n')
        self.assertEqual(result.model_calls_used, 1)
        self.assertEqual(result.checks, [])

    def test_stale_executor_cannot_undo_stop(self):
        queue = JobQueue(str(self.root / 'state'))
        job = Job('job_one', JobKind.FIX_PR, 'owner/repo', 1, 0, 1, state=JobState.WORKING)
        queue.add(job)
        stale = queue.get(job.job_id)
        queue.stop_job(job.job_id)
        stale.state = JobState.DONE
        stale.notes['agent_summary'] = 'The current step returned.'
        queue.update(stale)
        self.assertEqual(queue.get(job.job_id).state, JobState.STOPPED)
        self.assertEqual(queue.get(job.job_id).notes['agent_summary'], 'The current step returned.')

    def test_stop_during_check_prevents_next_check(self):
        stopped = False
        commands = []
        def check_runner(argv):
            nonlocal stopped
            commands.append(argv)
            stopped = True
            return 0, 'first check finished'
        model = FakeModel([resp(tool_call('done', 'finish', {'summary': 'Done'}))])
        task = self.task(lambda: stopped)
        task['checks'] = ['first-check', 'second-check']
        result = CodingAgent(model, self.cfg, check_runner=check_runner).run(task)
        self.assertEqual(commands, [['first-check']])
        self.assertEqual(len(result.checks), 1)
        self.assertIn('stopped', result.error.lower())

    def test_exports_patch_committed_and_uncommitted_changes_into_unique_folder(self):
        output = self.root / 'results'
        output.mkdir()
        (output / 'personal.txt').write_text('unrelated project data')
        (self.wt / 'code.py').write_text('answer = 2\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fix')
        (self.wt / 'code.py').write_text('answer = 3\n')
        (self.wt / 'new.txt').write_text('new file\n')
        (self.wt / 'escape').symlink_to(output / 'personal.txt')
        job = Job('job_export', JobKind.FIX_PR, 'owner/repo', 1, 0, 1, started_commit=self.start,
                  notes={'output_dir': str(output), 'member': 'test', 'member_home': str(self.root),
                         'changed_files': ['code.py', 'new.txt', '../results/personal.txt', 'escape']})
        with self.assertRaisesRegex(RuntimeError, 'Scratch was retained'):
            export_worktree(job, self.wt)
        artifact = output / 'gha-worker-job_export'
        self.assertEqual((artifact / 'files/code.py').read_text(), 'answer = 3\n')
        self.assertEqual((artifact / 'files/new.txt').read_text(), 'new file\n')
        patch = (artifact / 'changes.patch').read_text()
        self.assertIn('-answer = 1', patch)
        self.assertIn('+answer = 3', patch)
        self.assertFalse((artifact / 'files/escape').exists())
        self.assertEqual((output / 'personal.txt').read_text(), 'unrelated project data')
        report = json.loads((artifact / 'report.json').read_text())
        self.assertEqual(report['notes']['exported_files'], ['code.py', 'new.txt'])
        self.assertIn('escape', report['notes']['export_skipped_files'])

    def test_stop_during_commit_prevents_push(self):
        job = flow.make_job()
        queue = JobQueue(str(self.root / 'state'))
        queue.add(job)
        class StopDuringCommit(flow.FakeWT):
            def commit_all(inner, wt, message):
                queue.stop_job(job.job_id)
                return super().commit_all(wt, message)
        runner = flow.JobExecutor(flow.happy_gh(), queue, flow.default_cfg(),
                                 flow.FakeAgent(flow.agent_result()), worktree_ops=StopDuringCommit())
        git_record, github_record = flow.RecordGit(), flow.RecordGh()
        with patch.object(executor, '_run_git', git_record), patch.object(responder, '_run_gh', github_record):
            returned = runner.execute(job)
        self.assertEqual(returned.state, JobState.STOPPED)
        self.assertEqual(git_record.calls, [])
        self.assertEqual(github_record.calls, [])

    def test_export_failure_retains_scratch_and_records_error(self):
        job = flow.make_job(notes={'output_dir': str(self.root / 'missing'), 'member': 'test', 'member_home': str(self.root)})
        returned, _, wt, _, _, _ = flow.run_job(job, flow.happy_gh())
        self.assertIn('result_export_error', returned.notes)
        self.assertIn('retained', returned.notes['worktree_cleanup'])
        self.assertFalse(any(call[0] == 'remove_worktree' for call in wt.calls))

    def test_destination_symlink_changed_after_queue_is_refused(self):
        output = self.root / 'results'
        output.mkdir()
        job = Job('job_escape', JobKind.FIX_PR, 'owner/repo', 1, 0, 1, started_commit=self.start,
                  notes={'output_dir': str(output), 'member': 'test', 'member_home': str(self.root)})
        output.rmdir()
        output.symlink_to('/tmp', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'member account space'):
            export_worktree(job, self.wt)

    @unittest.skipUnless(shutil.which('setfacl') and shutil.which('getfacl'), 'POSIX access utilities are required for shared output')
    def test_member_access_is_inherited_by_new_result_files(self):
        folder = self.root / 'shared-results'
        folder.mkdir()
        with patch('ghagent.results.os.geteuid', return_value=-1):
            grant_member_access(folder, self.root)
        output = folder / 'report.json'
        output.write_text('test evidence')
        rules = subprocess.run(['getfacl', '-cpn', str(output)], capture_output=True, text=True, check=True).stdout
        self.assertIn(f'user:{self.root.stat().st_uid}:rwx', rules)

    def budget_client(self, daily_limit=2):
        self.cfg.model.auth_mode = 'env'
        self.cfg.model.max_calls_per_job = 1
        self.cfg.model.max_calls_per_day = daily_limit
        client = build_model_client(self.cfg, {'GHA_MODEL_API_KEY': 'test-only-key'})
        sent = []
        def send(request, **kwargs):
            sent.append(request)
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'Inspected without changes.'}}]}).encode())
        client._urlopen = send
        return client, sent

    def test_production_wiring_gives_each_job_its_limit_and_preserves_daily_usage_on_restart(self):
        client, sent = self.budget_client()
        factory = make_coding_agent_factory(self.cfg, client)
        for job_id in ('first_job', 'second_job'):
            runner = factory(str(self.wt), [])
            runner.set_job_context(job_id)
            result = runner.run('Inspect this repository without changing files.')
            self.assertFalse(result.error)
            self.assertEqual(result.model_calls_used, 1)
        self.assertEqual(len(sent), 2)
        restored, sent_after_restart = self.budget_client()
        restored.begin_job('third_job')
        with self.assertRaisesRegex(SpendLimitExceeded, 'daily'):
            restored.chat([{'role': 'user', 'content': 'Blocked by daily allowance'}])
        restored.begin_job('first_job')
        with self.assertRaisesRegex(SpendLimitExceeded, 'job spend'):
            restored.chat([{'role': 'user', 'content': 'Already used this job allowance'}])
        self.assertEqual(sent_after_restart, [])
        usage = json.loads((self.root / 'state/model-call-counts.json').read_text())
        self.assertEqual(usage['count'], 2)
        self.assertEqual(usage['jobs'], {'first_job': 1, 'second_job': 1})

    def test_shared_daily_allowance_is_atomic_between_independent_clients(self):
        clients = [self.budget_client(daily_limit=3) for _ in range(8)]
        def request(item):
            index, (client, _) = item
            client.begin_job(f'parallel_{index}')
            try:
                client.chat([{'role': 'user', 'content': 'One fixture request'}])
                return True
            except SpendLimitExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            succeeded = list(pool.map(request, enumerate(clients)))
        self.assertEqual(sum(succeeded), 3)
        self.assertEqual(sum(len(sent) for _, sent in clients), 3)
        usage = json.loads((self.root / 'state/model-call-counts.json').read_text())
        self.assertEqual(usage['count'], 3)



if __name__ == '__main__':
    unittest.main()
