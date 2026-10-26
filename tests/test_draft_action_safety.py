"""Draft decisions serialize against real durable state; remote writes are fake."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ghagent import tasks
from ghagent.interfaces import JobKind, JobState
from ghagent.web_control import ApiError, WebControl
from tests.test_phase0 import inject
from ghagent.tasks.types import TaskType
from tests.test_web_control import GuiFixture


class DraftActionSafetyTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.applied = []
        self.register_task()
        self.persisted_job(
            kind=JobKind.TRIAGE_ISSUES, state=JobState.WAITING_ON_YOU,
            notes={'drafts': [{'name': 'one', 'title': 'First issue',
                               'body': 'First words.'}],
                   'output_dir': str(self.output), 'member': 'alice'})

    def tearDown(self):
        tasks.types._REGISTRY = None

    def register_task(self, *, apply=None, confirm=False):
        def default_apply(context, gh, job):
            self.applied.append(context)
            return {'result': 'Posted ' + context['draft']['name']}
        inject(TaskType(id='triage-issues', label='Turn words into issues',
                        profile='draft', takes='repo', help='Creates issue drafts.',
                        apply=apply or default_apply,
                        post_confirm_required=confirm))

    def action(self, action='post', name='one', control=None, **kwargs):
        return (control or self.control).draft_action(
            'job_test', {'action': action, 'name': name, **kwargs})

    def stored(self):
        return self.control.queue().get('job_test')

    def update_notes(self, **notes):
        job = self.stored()
        job.notes.update(notes)
        self.control.queue().update(job)

    def second_draft(self):
        self.update_notes(drafts=self.stored().notes['drafts'] + [
            {'name': 'two', 'title': 'Second issue', 'body': 'Second words.'}])

    def other_control(self):
        return WebControl(self.config_path, self.env_path, gh=self.gh,
                          runner=self.runner, accounts=self.control._accounts)

    def assert_conflict(self, action='post', **kwargs):
        with self.assertRaises(ApiError) as caught:
            self.action(action, **kwargs)
        self.assertEqual(caught.exception.status, 409)
        return str(caught.exception)

    def test_post_cannot_be_replayed_after_completion(self):
        self.action()
        before = self.stored().to_json()
        self.assert_conflict()
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.stored().to_json(), before)

    def test_discarded_job_cannot_post_or_edit(self):
        self.action('discard')
        before = self.stored().to_json()
        self.assert_conflict()
        self.assert_conflict('edit', body='Should never be saved.')
        self.assertEqual(self.applied, [])
        self.assertEqual(self.stored().to_json(), before)

    def test_only_waiting_jobs_accept_any_draft_action(self):
        for state in JobState:
            if state is JobState.WAITING_ON_YOU:
                continue
            with self.subTest(state=state):
                # STOPPED is deliberately authoritative in JobQueue.update.
                raw = json.loads((self.state / 'jobs.json').read_text())
                raw['jobs']['job_test']['state'] = state.value
                (self.state / 'jobs.json').write_text(json.dumps(raw))
                before = self.stored().to_json()
                for action in ('post', 'edit', 'discard'):
                    self.assert_conflict(action, body='New words.')
                self.assertEqual(self.stored().to_json(), before)
        self.assertEqual(self.applied, [])

    def test_posted_draft_cannot_be_edited_or_reposted_while_others_wait(self):
        self.second_draft()
        self.action()
        self.assertEqual(self.stored().state, JobState.WAITING_ON_YOU)
        self.assert_conflict()
        self.assert_conflict('edit', body='Changed after posting.')
        self.assertEqual([p['name'] for p in self.stored().notes['posted']], ['one'])
        self.assertEqual(self.stored().notes['drafts'][0]['body'], 'First words.')
        self.assertEqual(len(self.applied), 1)

    def test_unposted_drafts_can_still_be_edited_and_posted_individually(self):
        self.second_draft()
        self.action()
        self.action('edit', name='two', body='Improved second words.')
        result = self.action(name='two')
        self.assertEqual(result['job']['state'], 'done')
        self.assertEqual([p['name'] for p in result['job']['notes']['posted']],
                         ['one', 'two'])
        self.assertEqual(self.applied[1]['draft']['body'], 'Improved second words.')
        self.assertNotIn('posting', result['job']['notes'])

    def test_discard_preserves_successful_partial_results_and_describes_them(self):
        self.second_draft()
        self.action()
        result = self.action('discard', name='two')
        self.assertEqual(result['job']['state'], 'stopped')
        self.assertEqual([p['name'] for p in result['job']['notes']['posted']], ['one'])
        self.assertIn('already posted', result['hint'].lower())
        self.assertNotIn('Nothing was posted', result['job']['error'])
        self.assert_conflict(name='two')
        self.assertEqual(len(self.applied), 1)

    def test_existing_unresolved_claim_blocks_post_edit_and_discard(self):
        self.second_draft()
        self.update_notes(posting='one', posted=[{'name': 'two', 'result': 'Already done'}])
        before = self.stored().to_json()
        for action in ('post', 'edit', 'discard'):
            self.assert_conflict(action, body='Cannot change an uncertain post.')
        self.assertEqual(self.applied, [])
        self.assertEqual(self.stored().to_json(), before)

    def test_apply_failure_retains_claim_and_refuses_replay_after_restart(self):
        for error in (ValueError('Connection ended after sending.'),
                      ApiError('Reply could not be read.', 503),
                      RuntimeError('Connection ended after sending.')):
            with self.subTest(error=type(error).__name__):
                # Each subcase starts from a separate saved draft job.
                self.build_gui()
                self.persisted_job(
                    kind=JobKind.TRIAGE_ISSUES, state=JobState.WAITING_ON_YOU,
                    notes={'drafts': [{'name': 'one', 'body': 'Words.'}]})
                calls = []
                def apply(context, gh, job):
                    calls.append(context)
                    self.assertEqual(self.stored().notes.get('posting'), 'one')
                    raise error
                self.register_task(apply=apply)
                with self.assertRaises(ApiError):
                    self.action()
                self.assertEqual(self.stored().notes.get('posting'), 'one')
                self.assertNotIn('posted', self.stored().notes)
                self.assertIn('check GitHub', self.stored().notes['verification'])
                self.assert_conflict(control=self.other_control())
                self.assertEqual(len(calls), 1)

    def test_exception_credential_shapes_are_not_returned_or_saved(self):
        secret = 'sk-' + 's' * 32
        def apply(context, gh, job):
            raise ValueError('Provider refused ' + secret)
        self.register_task(apply=apply)
        message = self.assert_conflict()
        self.assertNotIn(secret, message)
        self.assertNotIn(secret, self.stored().to_json())
        self.assertIn('sk-***', message)

    def test_failed_second_post_preserves_first_success(self):
        self.second_draft()
        self.action()
        def fail(context, gh, job):
            raise ValueError('The remote outcome is unknown.')
        self.register_task(apply=fail)
        with self.assertRaises(ApiError):
            self.action(name='two')
        job = self.stored()
        self.assertEqual(job.notes['posting'], 'two')
        self.assertEqual([p['name'] for p in job.notes['posted']], ['one'])
        self.assert_conflict(name='two')

    def test_confirmation_requires_an_explicit_boolean(self):
        self.register_task(confirm=True)
        for value in (None, False, 'false', 'true', 1, [], {}):
            with self.subTest(value=value):
                self.assert_conflict(confirm=value)
                self.assertNotIn('posting', self.stored().notes)
        self.action(confirm=True)
        self.assertEqual(len(self.applied), 1)

    def test_apply_receives_configuration_from_disk_not_request(self):
        self.action(config={'repos': [], 'github_host': 'untrusted.example'})
        cfg = self.applied[0]['config']
        self.assertEqual(cfg.github_host, 'github.com')
        self.assertEqual(cfg.repos[0].full_name, 'owner/repo')
        self.assertEqual(cfg.repos[0].checks, ['python3 -m unittest'])

    def test_claim_must_be_durable_before_any_remote_write(self):
        from ghagent.queue import JobQueue
        with patch.object(JobQueue, 'update', side_effect=OSError('Storage failed.')):
            with self.assertRaises(OSError):
                self.action()
        self.assertEqual(self.applied, [])
        self.assertNotIn('posting', self.stored().notes)

    def test_success_not_saved_keeps_durable_claim_and_prevents_replay(self):
        from ghagent.queue import JobQueue
        update = JobQueue.update
        saves = []
        def save(queue, job):
            saves.append(job.to_json())
            if len(saves) == 2:
                raise OSError('Storage failed after sending.')
            return update(queue, job)
        with patch.object(JobQueue, 'update', save):
            with self.assertRaises(OSError):
                self.action()
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.stored().notes.get('posting'), 'one')
        self.assert_conflict(control=self.other_control())
        self.assertEqual(len(self.applied), 1)

    def test_proven_prewrite_refusal_is_retryable_without_losing_partial_results(self):
        self.second_draft()
        self.action()
        module = types.ModuleType('ghagent.task_write_guard')
        class PreWriteRefusal(ValueError):
            pass
        module.PreWriteRefusal = PreWriteRefusal
        calls = []
        def apply(context, gh, job):
            calls.append(context)
            if len(calls) == 1:
                raise PreWriteRefusal('Verification failed before posting. Fix the checks and retry.')
            return {'result': 'Posted after verification.'}
        self.register_task(apply=apply)
        with patch.dict(sys.modules, {'ghagent.task_write_guard': module}):
            self.assertIn('Verification failed', self.assert_conflict(name='two'))
            job = self.stored()
            self.assertEqual(job.state, JobState.WAITING_ON_YOU)
            self.assertNotIn('posting', job.notes)
            self.assertEqual([p['name'] for p in job.notes['posted']], ['one'])
            self.action('edit', name='two', body='Corrected second draft.')
            self.assertEqual(self.action(name='two')['job']['state'], 'done')
        self.assertEqual(len(calls), 2)
        self.assertEqual([p['name'] for p in self.stored().notes['posted']], ['one', 'two'])

    def test_broken_optional_guard_does_not_make_unknown_failures_retryable(self):
        module = types.ModuleType('ghagent.task_write_guard')
        def missing_attribute(name):
            raise RuntimeError('Guard failed to import.')
        module.__getattr__ = missing_attribute
        def apply(context, gh, job):
            raise ValueError('Unknown remote outcome.')
        self.register_task(apply=apply)
        with patch.dict(sys.modules, {'ghagent.task_write_guard': module}):
            self.assert_conflict()
        self.assertEqual(self.stored().notes.get('posting'), 'one')
        self.assert_conflict()

    def test_stop_during_claim_prevents_remote_apply(self):
        from ghagent.queue import JobQueue
        update = JobQueue.update
        def stop_then_update(queue, job):
            queue.stop_job(job.job_id)
            return update(queue, job)
        with patch.object(JobQueue, 'update', stop_then_update):
            self.assert_conflict()
        self.assertEqual(self.stored().state, JobState.STOPPED)
        self.assertEqual(self.applied, [])

    def test_slow_post_does_not_block_another_jobs_draft(self):
        self.persisted_job(
            job_id='job_other', dedup_key='another-task',
            kind=JobKind.TRIAGE_ISSUES, state=JobState.WAITING_ON_YOU,
            notes={'drafts': [{'name': 'other', 'body': 'Independent words.'}]})
        entered, release = threading.Event(), threading.Event()
        def apply(context, gh, job):
            self.applied.append(context)
            if job.job_id == 'job_test':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release the post.')
            return {'result': 'Posted ' + context['draft']['name']}
        self.register_task(apply=apply)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.action)
            try:
                self.assertTrue(entered.wait(5))
                result = self.other_control().draft_action(
                    'job_other', {'action': 'post', 'name': 'other'})
                self.assertEqual(result['job']['state'], 'done')
                self.assertEqual(self.stored().notes.get('posting'), 'one')
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)['job']['state'], 'done')
        self.assertEqual(len(self.applied), 2)

    def test_independent_process_cannot_post_while_a_request_owns_the_job(self):
        entered, release = threading.Event(), threading.Event()
        def apply(context, gh, job):
            self.applied.append(context)
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Test did not release the post.')
            return {'result': 'Posted once.'}
        self.register_task(apply=apply)
        child = """
import sys
from ghagent.web_control import ApiError, WebControl
control = WebControl(sys.argv[1], sys.argv[2], gh=object())
try:
    control.draft_action('job_test', {'action': 'post', 'name': 'one'})
except ApiError as exc:
    if exc.status == 409 and 'in progress' in str(exc):
        sys.exit(0)
raise SystemExit('An independent process was allowed to claim the same draft.')
"""
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.action)
            try:
                self.assertTrue(entered.wait(5))
                child_env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH='src')
                result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning',
                                         '-c', child, str(self.config_path), str(self.env_path)],
                                        capture_output=True, text=True, env=child_env, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.applied), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)['job']['state'], 'done')

    def test_parallel_requests_cannot_change_or_repeat_an_inflight_post(self):
        entered, release = threading.Event(), threading.Event()
        def apply(context, gh, job):
            self.applied.append(context)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release the post.')
            return {'result': 'Posted once.'}
        self.register_task(apply=apply)
        other = self.other_control()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.action)
            try:
                self.assertTrue(entered.wait(5))
                # This independently constructed controller models a second
                # server process. These return immediately instead of waiting
                # for the HTTP request that currently owns the draft.
                for action in ('post', 'edit', 'discard'):
                    second = pool.submit(self.action, action, control=other,
                                         body='Concurrent edit.')
                    with self.assertRaises(ApiError) as caught:
                        second.result(timeout=2)
                    self.assertEqual(caught.exception.status, 409)
                self.assertEqual(len(self.applied), 1)
                self.assertEqual(self.stored().notes['posting'], 'one')
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)['job']['state'], 'done')
        self.assertEqual(len(self.applied), 1)
        self.assert_conflict(control=other)


if __name__ == '__main__':
    unittest.main()
