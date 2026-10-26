"""Learning HTTP/control integration, isolated records and model transport only."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from ghagent.control_store import AuthorityStore
from ghagent.interfaces import JobKind, JobState
from ghagent.tasks import triage_issues
from tests import test_task_lifecycle as lifecycle
from tests import test_web_http as http_fixture
from tests.test_web_control import GuiFixture


class LearningControls(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def start_data(self, **changes):
        value = {'action': 'start', 'request_id': str(uuid4()), 'mode': 'guide',
                 'lesson': 'git-basics', 'repo': 'owner/repo', 'ref': '', 'path': '',
                 'account': 'alice', 'output_dir': str(self.output)}
        value.update(changes)
        return value

    def test_destination_is_required_before_creating_any_session(self):
        from ghagent.web_control import ApiError, DESTINATION_ERROR
        with self.assertRaisesRegex(ApiError, DESTINATION_ERROR):
            self.control.learning_action(self.start_data(output_dir=''))
        self.assertFalse((self.state / 'learning-v1').exists())

    def test_creation_retries_resume_same_session_and_export_stays_in_bound_folder(self):
        data = self.start_data()
        first = self.control.learning_action(data)['session']
        second = self.control.learning_action(data)['session']
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.control.learning_get(first['id'])['session']['id'], first['id'])
        exported = self.control._export_lesson(first, '# Lesson\nReal evidence\n')
        path = Path(exported['path'])
        self.assertTrue(path.is_relative_to(self.output))
        self.assertEqual(path.read_text(), '# Lesson\nReal evidence\n')
        next_export = self.control._export_lesson(first, 'Second')
        self.assertNotEqual(next_export['path'], str(path))
        self.assertEqual(path.read_text(), '# Lesson\nReal evidence\n')

    def test_export_rejects_swapped_symlink_destination(self):
        from ghagent.web_control import ApiError
        session = self.control.learning_action(self.start_data())['session']
        (self.output / ('fatchin-learn-' + session['id'])).symlink_to(self.home)
        with self.assertRaises(ApiError):
            self.control._export_lesson(session, 'Do not escape')

    def test_export_parent_replaced_after_validation_cannot_write_outside_member_space(self):
        from ghagent.web_control import ApiError
        session = self.control.learning_action(self.start_data())['session']
        outside = self.root / 'outside-member-space'; outside.mkdir()
        original = self.control.destination
        def swapped(data):
            path = original(data)
            self.output.rename(self.home / 'preserved-results')
            self.output.symlink_to(outside)
            return path
        with patch.object(self.control, 'destination', swapped):
            with self.assertRaises(ApiError):
                self.control._export_lesson(session, 'Must remain in member space')
        self.assertEqual(list(outside.iterdir()), [])

    def test_learning_origin_handoff_binds_project_account_folder_and_draft_profile(self):
        from ghagent.web_control import ApiError
        session = self.control.learning_action(self.start_data())['session']
        request = self.task_data(kind='triage-issues', words='Improve setup',
                                 learning_session_id=session['id'])
        queued = self.control.enqueue(request)['job']
        self.assertEqual(queued['notes']['learning_session_id'], session['id'])
        self.assertEqual(queued['notes']['request']['learning_session_id'], session['id'])
        with self.assertRaises(ApiError):
            self.control.enqueue({**request, 'kind': 'fix-pr'})
        other = self.home / 'another'; other.mkdir()
        with self.assertRaises(ApiError):
            self.control.enqueue({**request, 'output_dir': str(other)})

    def test_model_factory_keeps_endpoint_and_shared_ledger_without_changing_credentials(self):
        before = self.env_path.read_bytes()
        observed = {}
        class Client:
            def __init__(self, cfg, bearer, **kw):
                observed.update(endpoint=cfg.base_url, model=cfg.model_id,
                                scope=kw['job_scope'], attempts=kw['max_attempts'],
                                counter=kw['day_counter_path'])
        with patch('ghagent.model_client.ModelClient', Client):
            with self.control._learning_model('learn-session'):
                pass
        self.assertEqual(observed, {'endpoint': 'https://provider.example/v1', 'model': 'model-a',
                                    'scope': 'learn-session', 'attempts': 1,
                                    'counter': str(self.state / 'model-call-counts.json')})
        self.assertEqual(self.env_path.read_bytes(), before)


class LearningAuthority(unittest.TestCase):
    def test_existing_auto_authority_cannot_publish_a_learning_origin_draft(self):
        from dataclasses import replace
        fixture = lifecycle.TaskLifecycleTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        job = fixture.job(JobKind.TRIAGE_ISSUES)
        job.notes['learning_session_id'] = 'learning-bound-session'
        fixture.queue.update(job)
        AuthorityStore(Path(fixture.cfg.state_dir) / 'authority.json').set_mode(job.repo_full_name, 'auto')
        calls = []
        task = replace(triage_issues.TASK, apply=lambda *args: calls.append(args))
        executor, _ = fixture.executor(lifecycle.LocalTransport([lifecycle.completed(job.kind)]))
        with patch('ghagent.executor.tasks.get', return_value=task):
            returned = executor.execute(job)
        self.assertIs(returned.state, JobState.WAITING_ON_YOU)
        self.assertEqual(calls, [])
        self.assertTrue(returned.notes['drafts'])


    def test_auto_posting_stops_between_actual_issue_drafts(self):
        fixture = lifecycle.TaskLifecycleTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        job = fixture.job(JobKind.TRIAGE_ISSUES)
        AuthorityStore(Path(fixture.cfg.state_dir) / 'authority.json').set_mode(job.repo_full_name, 'auto')
        writes = []
        class GitHub:
            def rest(self, method, path, **kwargs):
                if method != 'POST':
                    raise AssertionError('Unexpected read in issue apply')
                writes.append(path)
                fixture.queue.stop_job(job.job_id)
                return {'number': len(writes), 'title': 'Saved issue'}
        reply = lifecycle.message(
            lifecycle.tool('write_file', path='.gha-drafts/a.md', content='First issue\nDetails.'),
            lifecycle.tool('write_file', path='.gha-drafts/b.md', content='Second issue\nDetails.'),
            lifecycle.tool('finish', summary='Drafted both issues.', changed_files=[]))
        executor, _ = fixture.executor(lifecycle.LocalTransport([reply]))
        executor.gh = GitHub()
        with patch('ghagent.executor.tasks.get', return_value=triage_issues.TASK):
            result = executor.execute(job)
        self.assertIs(result.state, JobState.STOPPED)
        self.assertEqual(writes, ['repos/owner/repo/issues'])
        self.assertEqual(len(fixture.queue.get(job.job_id).notes['posted']), 1)


class LearningHttp(GuiFixture, unittest.TestCase):
    setUp = http_fixture.GuiHttpTests.setUp
    stop_server = http_fixture.GuiHttpTests.stop_server
    request = http_fixture.GuiHttpTests.request

    def test_new_routes_use_existing_host_origin_gate_and_return_real_session(self):
        data = {'action': 'start', 'request_id': str(uuid4()), 'mode': 'guide',
                'lesson': 'git-basics', 'account': 'alice', 'output_dir': str(self.output)}
        status, _, _ = self.request('POST', '/api/learn', data, {'Origin': 'https://attacker.example'})
        self.assertEqual(status, 403)
        status, _, body = self.request('POST', '/api/learn', data)
        self.assertEqual(status, 200, body)
        session = json.loads(body)['session']
        status, _, body = self.request('GET', '/api/learn?id=' + session['id'])
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['session']['id'], session['id'])
        status, _, body = self.request('POST', '/api/learn', {**data, 'request_id': 'invalid'})
        self.assertEqual(status, 400, body)
        self.assertNotIn(b'sk-existing-test-credential', body)
