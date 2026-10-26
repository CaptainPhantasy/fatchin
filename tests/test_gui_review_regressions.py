"""Independent backend review regressions; isolated files and process fakes only."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import unittest
from unittest.mock import patch

from ghagent import cli
from ghagent import control_store
from ghagent.executor import JobExecutor
from ghagent.interfaces import Job, JobKind, JobState, ModelConfig, RepoConfig
from ghagent.model_client import ModelClient, ModelError
from ghagent.wiring import WorktreeOpsAdapter, make_coding_agent_factory
from ghagent.worker import Worker
from ghagent.web_control import ApiError
from tests.test_executor_flow import FakeWT, happy_gh
from tests.test_web_control import GuiFixture


class GuiReviewRegressionTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.wt = self.root / 'review-worktree'
        self.wt.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Isolated review')
        self.git('config', 'user.email', 'review@local')
        (self.wt / 'existing.py').write_text('answer = 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial fixture')
        self.start = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.wt, capture_output=True,
                              text=True, check=True).stdout.strip()

    def job(self):
        job = Job('job_review', JobKind.FIX_PR, 'o/r', 12, 0, 42,
                  head_sha=self.start, started_commit=self.start,
                  notes={'output_dir': str(self.output), 'member': 'alice',
                         'member_home': str(self.home)})
        self.control.queue().add(job)
        return job

    def assert_untracked_work_survives_failure(self, filename):
        class FailingAfterWriteModel:
            calls = 0
            def chat(inner, messages, tools=None):
                inner.calls += 1
                if inner.calls == 1:
                    return {'content': None, 'tool_calls': [{
                        'id': 'create-new-file', 'type': 'function', 'function': {
                            'name': 'write_file',
                            'arguments': json.dumps({'path': filename, 'content': 'Useful unfinished work\n'})}}]}
                raise RuntimeError('Simulated model adapter failure after a successful file write')

        model = FailingAfterWriteModel()
        wt = self.wt
        class RealCleanup(FakeWT):
            def remove_worktree(inner, path):
                self.assertEqual(path, str(wt))
                shutil.rmtree(path)  # This test created the entire temporary checkout.

        cfg = self.control.config()
        cfg.repos = [RepoConfig('o', 'r', checks=['python3 -m unittest'])]
        gh = happy_gh()
        gh.pr['headRefOid'] = self.start
        executor = JobExecutor(gh, self.control.queue(), cfg,
                               make_coding_agent_factory(cfg, model),
                               worktree_ops=RealCleanup(path=str(wt), sha=self.start))
        result = executor.execute(self.job())
        self.assertEqual(result.state, JobState.FAILED)
        self.assertIn('Simulated model adapter failure', result.error)
        self.assertEqual(model.calls, 2)
        exported = self.output / 'gha-worker-job_review/files' / filename
        preserved = exported.is_file() or (wt / filename).is_file()
        self.assertTrue(preserved, 'Agent-created work was neither exported nor retained before cleanup.')

    def test_untracked_work_survives_an_unexpected_agent_failure(self):
        self.assert_untracked_work_survives_failure('new-feature.txt')

    def test_untracked_hidden_config_is_retained_when_it_cannot_be_exported(self):
        self.assert_untracked_work_survives_failure('.gitignore')

    def test_worktree_adapter_reports_failure_when_both_remove_attempts_fail(self):
        adapter = WorktreeOpsAdapter(self.control.config())
        calls = []
        def failed_git(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', 'fixture: checkout is locked')
        adapter._git = failed_git
        with self.assertRaises(RuntimeError):
            adapter.remove_worktree(str(self.wt))
        self.assertEqual(len(calls), 2)

    def test_cli_once_cannot_execute_while_the_shared_daemon_lock_is_held(self):
        queue = self.control.queue()
        executions = []
        class Executor:
            def execute(inner, job):
                executions.append(job.job_id)
                job.state = JobState.DONE
                queue.update(job)
        executor = Executor()
        gh = happy_gh()
        holder = Worker(gh, queue, executor, self.control.config())
        holder.acquire_single_instance_lock()
        self.addCleanup(holder.close)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), patch.dict(os.environ, {}, clear=True):
            result = cli.main(['once', '--config', str(self.config_path), '--env-file', str(self.env_path),
                               '--repo', 'owner/repo', '--kind', 'fix-pr', '--number', '12'],
                              component_factory=lambda cfg, env: (gh, queue, executor))
        self.assertEqual(result, 3, 'The one-shot command must report the shared worker as busy.')
        self.assertEqual(executions, [])
        self.assertEqual(queue.all(), [])

    def test_shared_shutdown_event_stops_before_tools_returned_by_a_model_request(self):
        shutdown = threading.Event()
        class ModelReturningAfterShutdown:
            calls = 0
            def chat(inner, messages, tools=None):
                inner.calls += 1
                shutdown.set()  # The CLI signal handler sets this worker event.
                return {'content': None, 'tool_calls': [
                    {'id': 'late-write', 'type': 'function', 'function': {
                        'name': 'write_file', 'arguments': json.dumps({
                            'path': 'should-not-run-after-shutdown.txt', 'content': 'late tool action'})}},
                    {'id': 'finish', 'type': 'function', 'function': {
                        'name': 'finish', 'arguments': json.dumps({'summary': 'Finished', 'changed_files': []})}}]}
        model = ModelReturningAfterShutdown()
        cfg = self.control.config()
        cfg.repos = [RepoConfig('o', 'r', checks=['python3 -c "raise SystemExit(0)"'])]
        executor = JobExecutor(happy_gh(), self.control.queue(), cfg,
                               make_coding_agent_factory(cfg, model),
                               worktree_ops=FakeWT(path=str(self.wt), sha=self.start))
        executor.stop_event = shutdown
        job = self.job()
        executor._working(job, {'pr': {'headRefName': 'feature'}, 'work_items': []})
        self.assertEqual(model.calls, 1)
        self.assertFalse((self.wt / 'should-not-run-after-shutdown.txt').exists(),
                         'A shared shutdown request was ignored before the next tool action.')
        self.assertEqual(self.control.queue().get(job.job_id).state, JobState.STOPPED)


class DeviceConnectionReviewTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def wait_for(self, predicate):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail('Isolated connection thread did not reach its expected state.')

    def exercise_connection(self, exit_code):
        release = threading.Event()
        timers = []
        class Output:
            closed = False
            def __iter__(inner):
                yield 'First copy your one-time code: ABCD-EFGH\n'
                if not release.wait(3):
                    raise RuntimeError('Test did not release the fake sign-in process.')
            def close(inner):
                inner.closed = True
        class Process:
            stdout = Output()
            def wait(inner):
                return exit_code
            def terminate(inner):
                release.set()
        process = Process()
        class Timer:
            cancelled = False
            def __init__(inner, seconds, action):
                self.assertEqual(seconds, 900)
                timers.append(inner)
            def start(inner):
                pass
            def cancel(inner):
                inner.cancelled = True

        with patch.dict(os.environ, {}, clear=True), patch('ghagent.web_control.find_gh', return_value='/fake/gh'), \
                patch('ghagent.web_control.subprocess.Popen', return_value=process) as popen, \
                patch('ghagent.web_control.threading.Timer', Timer):
            try:
                self.control.connect_github()
                self.wait_for(lambda: self.control.connection_status().get('code') == 'ABCD-EFGH')
                waiting = self.control.connection_status()
                self.assertEqual(waiting['status'], 'waiting')
                self.assertEqual(waiting['verification_uri'], 'https://github.com/login/device')
                self.control.connect_github()
                self.assertEqual(popen.call_count, 1, 'A second click must reuse the waiting connection.')
                release.set()
                self.wait_for(lambda: self.control.connection_status()['status'] != 'waiting')
                final = self.control.connection_status()
                self.assertNotIn('code', final)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(timers[0].cancelled)
                self.assertIn('--web', popen.call_args.args[0])
                return final
            finally:
                release.set()

    def test_device_sign_in_exposes_code_and_reaps_successful_process(self):
        self.assertEqual(self.exercise_connection(0)['status'], 'finished')

    def test_device_sign_in_clears_code_after_declined_authorization(self):
        self.assertEqual(self.exercise_connection(1)['status'], 'error')


class SaveFailureReviewTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def fail_restart_marker(self):
        original = control_store.atomic_json
        def write(path, value):
            if path.name == 'gui-control.json':
                raise OSError('Fixture: state volume refused the restart marker write')
            return original(path, value)
        return patch('ghagent.control_store.atomic_json', side_effect=write)

    def test_failed_restart_marker_write_does_not_partially_save_configuration(self):
        before = self.config_path.read_bytes()
        with self.fail_restart_marker(), self.assertRaises((OSError, ApiError)):
            self.control.save_settings({'revision': self.control.revision(),
                                        'settings': {'poll_interval_seconds': 90}})
        self.assertEqual(self.config_path.read_bytes(), before,
                         'The request failed but the configuration was replaced without a pending restart marker.')

    def test_failed_restart_marker_write_does_not_partially_replace_a_credential(self):
        before = self.env_path.read_bytes()
        records = self.root / 'credential-records.json'
        records.write_text(json.dumps({'GHA_MODEL_API_KEY': {'what': 'Existing credential description'}}))
        before_record = records.read_bytes()
        with self.fail_restart_marker(), self.assertRaises((OSError, ApiError)):
            self.control.credential({'kind': 'model', 'value': 'review-new-test-key', 'supplied_by': 'Alice'})
        self.assertEqual(self.env_path.read_bytes(), before,
                         'The request failed but the saved credential changed without a pending restart marker.')
        self.assertEqual(records.read_bytes(), before_record)


class ModelErrorPrivacyReviewTests(unittest.TestCase):
    def test_upstream_errors_cannot_echo_the_actual_bearer_credential(self):
        secret = 'fixture-provider-secret-without-a-standard-prefix'
        for failure in ('http-auth-error', 'invalid-success-schema'):
            with self.subTest(failure=failure):
                def response(request, **kwargs):
                    if failure == 'http-auth-error':
                        raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {},
                            io.BytesIO(('Invalid API key: ' + secret).encode()))
                    return io.BytesIO(json.dumps({'provider_error': secret}).encode())
                client = ModelClient(ModelConfig(auth_mode='env'), lambda: secret, urlopen=response)
                with self.assertRaises(ModelError) as caught:
                    client.chat([{'role': 'user', 'content': 'Isolated fixture'}])
                self.assertNotIn(secret, str(caught.exception),
                                 'Upstream response text exposed the model authentication credential.')


class InputValidationReviewTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_fractional_task_number_is_rejected_instead_of_targeting_another_pr(self):
        with self.assertRaises(ApiError):
            self.control.enqueue(self.task_data(number=3.75))
        self.assertEqual(self.control.queue().all(), [])
