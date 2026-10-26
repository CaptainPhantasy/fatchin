"""GUI behavior over real temporary files, a fake GitHub boundary and no paid calls."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from unittest.mock import patch
import unittest
from pathlib import Path

from ghagent.config_loader import load_env_file
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.web_control import ApiError, DESTINATION_ERROR, WebControl
from ghagent.worker import Worker
from tests.mock_github import make_client


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.comments = [{'id': 30}, {'id': 10}]
        self.item = {'number': 7, 'title': 'Improve reader', 'state': 'OPEN',
                     'url': 'https://github.com/owner/repo/pull/7', 'headRefOid': 'a' * 40,
                     'headRefName': 'feature/reader', 'updatedAt': '2026-10-19T01:00:00Z'}
        self.error = None

    def rest(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        if path == 'user':
            return {'login': 'test-member', 'id': 42}
        if path.endswith('/comments'):
            return list(self.comments)
        if path.endswith('/branches'):
            return [{'name': 'main'}, {'name': 'preview'}]
        if path == 'user/repos':
            return [{'full_name': 'owner/repo', 'name': 'repo', 'description': None,
                     'default_branch': 'main', 'private': True}]
        if path.endswith('/issues'):
            return [{'number': 8, 'title': 'Issue', 'state': 'open', 'html_url': 'https://github.com/owner/repo/issues/8'},
                    {'number': 7, 'title': 'PR in issue collection', 'state': 'open', 'html_url': 'https://github.com/owner/repo/pull/7', 'pull_request': {}}]
        if path.endswith('/pulls'):
            return [{'number': 7, 'title': 'Improve reader', 'state': 'open',
                     'html_url': self.item['url'], 'head': {'ref': 'feature/reader'}}]
        raise AssertionError(f'Unexpected fake GitHub call: {method} {path}')

    def pr_view(self, repo, number):
        self.calls.append(('pr_view', repo, number))
        return dict(self.item, number=number)

    def issue_view(self, repo, number):
        self.calls.append(('issue_view', repo, number))
        # The real issue view has no PR head fields. Reusing those fields here
        # previously hid broken issue revision deduplication.
        return {**{key: self.item[key] for key in ('title', 'state', 'updatedAt') if key in self.item},
                'number': number, 'body': 'Implement the requested reader change.',
                'url': f'https://github.com/{repo}/issues/{number}'}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.active = True

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[:3] == ['systemctl', '--user', 'show']:
            return subprocess.CompletedProcess(argv, 0, f'ActiveState={"active" if self.active else "inactive"}\nSubState=running\nMainPID=123\n', '')
        if argv[:2] == ['systemctl', '--user']:
            self.active = argv[2] != 'stop'
            return subprocess.CompletedProcess(argv, 0, '', '')
        if 'provider-check' in argv:
            return subprocess.CompletedProcess(argv, 0, 'Provider check passed', '')
        raise AssertionError(f'Unexpected process boundary: {argv}')


class GuiFixture:
    """Shared file/process fixture for direct and HTTP public seam tests."""
    def build_gui(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'alice'
        self.home.mkdir()
        self.output = self.home / 'project-results'
        self.output.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.worktrees = self.root / 'worktrees'
        self.worktrees.mkdir()
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text(
            'github_host = "github.com"\nrest_api_version = "2022-11-28"\n'
            'poll_interval_seconds = 120\ncommand_prefix = "/agent"\nallowed_command_users = [42]\n'
            f'state_dir = {json.dumps(str(self.state))}\nworktree_root = {json.dumps(str(self.worktrees))}\n'
            'max_tool_iterations = 80\nrun_command_timeout_seconds = 600\nrun_command_output_cap = 100000\npoll_repos = true\n'
            '[[repos]]\nowner = "owner"\nname = "repo"\nbase_branch = "main"\nchecks = ["python3 -m unittest"]\nallow_unknown_rulesets = true\n'
            '[model]\nbase_url = "https://provider.example/v1"\nmodel_id = "model-a"\nauth_mode = "env"\n'
            'broker_consumer_id = "existing-consumer"\nmax_calls_per_job = 60\nmax_calls_per_day = 600\n')
        self.env_path = self.root / 'env'
        self.env_path.write_text('GHA_MODEL_API_KEY=sk-existing-test-credential\nUNCHANGED=retained\n')
        self.gh = FakeGitHub()
        self.runner = FakeRunner()
        self.control = WebControl(self.config_path, self.env_path, gh=self.gh, runner=self.runner,
                                  accounts=[{'account': 'alice', 'label': 'Alice', 'path': str(self.home), 'uid': 4000}])

    def task_data(self, **changes):
        value = {'repo': 'owner/repo', 'kind': 'fix-pr', 'number': 7,
                 'output_dir': str(self.output), 'account': 'alice'}
        value.update(changes)
        return value

    def persisted_job(self, **changes):
        value = {'job_id': 'job_test', 'kind': JobKind.FIX_PR, 'repo_full_name': 'owner/repo',
                 'number': 7, 'command_comment_id': 0, 'requested_by': 42,
                 'head_sha': 'a' * 40, 'dedup_key': 'fix-pr:owner/repo:7:test',
                 'notes': {'output_dir': str(self.output), 'member': 'alice'}}
        value.update(changes)
        job = Job(**value)
        self.assertTrue(self.control.queue().add(job))
        return job


class FolderBoundaryTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_destination_is_explicit_and_inside_a_named_project(self):
        with self.assertRaisesRegex(ApiError, '^' + DESTINATION_ERROR.replace('.', r'\.') + '$'):
            self.control.destination({'account': 'alice'})
        for path in (self.home, self.root, self.home / '.config'):
            if path.name == '.config':
                path.mkdir()
            with self.subTest(path=path), self.assertRaises(ApiError):
                self.control.destination({'account': 'alice', 'output_dir': str(path)})
        self.assertEqual(self.control.destination(self.task_data()), str(self.output))

    def test_browser_hides_hidden_and_symlink_folders_and_cannot_escape(self):
        (self.home / '.config').mkdir()
        (self.home / 'escape').symlink_to(self.root, target_is_directory=True)
        items = self.control.folders('alice')['items']
        self.assertEqual([item['name'] for item in items], ['project-results'])
        for path in (self.home / '..', self.home / 'escape'):
            with self.subTest(path=path), self.assertRaises(ApiError) as caught:
                self.control.folders('alice', str(path))
            self.assertEqual(caught.exception.status, 403)

    def test_create_folder_is_deliberate_and_rejects_traversal_or_collision(self):
        created = self.control.create_folder({'account': 'alice', 'parent': str(self.home), 'name': 'My project'})
        self.assertTrue(Path(created['path']).is_dir())
        for name in ('../escape', '.credentials', 'nested/path', 'nested\\path', ''):
            with self.subTest(name=name), self.assertRaises(ApiError):
                self.control.create_folder({'account': 'alice', 'parent': str(self.home), 'name': name})
        with self.assertRaises(ApiError) as caught:
            self.control.create_folder({'account': 'alice', 'parent': str(self.home), 'name': 'My project'})
        self.assertEqual(caught.exception.status, 409)


class ConfigAndCredentialsTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_settings_save_backs_up_and_preserves_other_config_fields(self):
        before = self.config_path.read_bytes()
        revision = self.control.revision()
        result = self.control.save_settings({'revision': revision, 'settings': {'poll_interval_seconds': 60, 'model': {'model_id': 'model-b'}}})
        self.assertTrue(result['restart_required'])
        cfg = self.control.config()
        self.assertEqual((cfg.poll_interval_seconds, cfg.model.model_id), (60, 'model-b'))
        self.assertEqual(cfg.model.broker_consumer_id, 'existing-consumer')
        self.assertEqual(cfg.repos[0].checks, ['python3 -m unittest'])
        self.assertTrue(cfg.repos[0].allow_unknown_rulesets)
        self.assertTrue(any(path.read_bytes() == before for path in self.root.glob('config.toml.bak-gui-*')))
        self.assertNotEqual(self.control.revision(), revision)
        self.assertEqual(self.control.controls().read()['config_revision'], self.control.revision())

    def test_stale_window_cannot_overwrite_a_new_save(self):
        revision = self.control.revision()
        self.control.save_settings({'revision': revision, 'settings': {'poll_interval_seconds': 90}})
        before = self.config_path.read_bytes()
        with self.assertRaises(ApiError) as caught:
            self.control.save_settings({'revision': revision, 'settings': {'poll_interval_seconds': 30}})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_invalid_save_does_not_change_config_or_claim_success(self):
        before = self.config_path.read_bytes()
        invalid = ({'poll_interval_seconds': True}, {'poll_interval_seconds': 0},
                   {'model': {'max_calls_per_job': -1}}, {'model': {'base_url': 'https://user:secret@provider.example/v1'}},
                   {'allowed_command_users': []}, {'unexpected': 'value'}, {'poll_repos': 'false'})
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ApiError):
                self.control.save_settings({'revision': self.control.revision(), 'settings': settings})
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_infrastructure_path_edits_are_explicitly_rejected(self):
        before = self.config_path.read_bytes()
        for key in ('state_dir', 'worktree_root', 'github_host'):
            with self.subTest(key=key), self.assertRaises(ApiError):
                self.control.save_settings({'revision': self.control.revision(), 'settings': {key: '/different-location'}})
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_credential_is_write_only_self_described_and_other_values_survive(self):
        secret = 'sk-new-test-credential-0123456789'
        before = self.env_path.read_bytes()
        response = self.control.credential({'kind': 'model', 'value': secret, 'supplied_by': 'Alice'})
        self.assertNotIn(secret, json.dumps(response))
        self.assertEqual(load_env_file(self.env_path)['GHA_MODEL_API_KEY'], secret)
        self.assertEqual(load_env_file(self.env_path)['UNCHANGED'], 'retained')
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        records = json.loads((self.root / 'credential-records.json').read_text())
        self.assertEqual(records['GHA_MODEL_API_KEY']['supplied_by'], 'Alice')
        for key in ('what', 'does', 'not', 'supplied_at'):
            self.assertTrue(records['GHA_MODEL_API_KEY'][key])
        self.assertNotIn(secret, json.dumps(records))
        self.assertTrue(any(p.read_bytes() == before for p in self.root.glob('env.bak-gui-*')))
        self.assertNotIn(secret, json.dumps(self.control.state()))

    def test_unusual_credential_is_rejected_or_round_trips_without_corruption(self):
        before = self.env_path.read_bytes()
        secret = 'test-key-with-quote"and-backslash\\tail'
        try:
            self.control.credential({'kind': 'model', 'value': secret, 'supplied_by': 'Alice'})
        except ApiError:
            self.assertEqual(self.env_path.read_bytes(), before)
        else:
            self.assertEqual(load_env_file(self.env_path)['GHA_MODEL_API_KEY'], secret)

    def test_bad_credential_input_does_not_replace_existing_values(self):
        before = self.env_path.read_bytes()
        for data in ({'kind': 'model', 'value': 'secret\nOTHER=bad', 'supplied_by': 'Alice'},
                     {'kind': 'model', 'value': 'secret', 'supplied_by': ''},
                     {'kind': 'unknown', 'value': 'secret', 'supplied_by': 'Alice'}):
            with self.subTest(data=data), self.assertRaises(ApiError):
                self.control.credential(data)
            self.assertEqual(self.env_path.read_bytes(), before)

    def test_corrupt_credential_metadata_does_not_change_the_saved_secret(self):
        before = self.env_path.read_bytes()
        records = self.root / 'credential-records.json'
        records.write_text('{corrupt descriptive record')
        with self.assertRaises((RuntimeError, ApiError)):
            self.control.credential({'kind': 'model', 'value': 'test-replacement-key', 'supplied_by': 'Alice'})
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertEqual(records.read_text(), '{corrupt descriptive record')
        self.assertNotIn('config_revision', self.control.controls().read())

    def assert_save_waits_for_restart(self, save, read_saved):
        """Hold a real service request open while another window saves."""
        restart_entered = threading.Event()
        restart_release = threading.Event()
        save_entered = threading.Event()
        save_finished = threading.Event()
        failures = []
        before = read_saved()
        original_runner = self.control.runner

        def blocked_restart(argv, **kwargs):
            if argv[:3] == ['systemctl', '--user', 'restart']:
                restart_entered.set()
                if not restart_release.wait(3):
                    raise RuntimeError('Test did not release the service restart.')
            return original_runner(argv, **kwargs)

        def restart():
            try:
                self.control.service_action({'action': 'restart'})
            except BaseException as error:
                failures.append(error)

        def write_from_other_window():
            save_entered.set()
            try:
                save()
            except BaseException as error:
                failures.append(error)
            finally:
                save_finished.set()

        self.control.runner = blocked_restart
        restart_thread = threading.Thread(target=restart, daemon=True)
        save_thread = threading.Thread(target=write_from_other_window, daemon=True)
        restart_thread.start()
        try:
            self.assertTrue(restart_entered.wait(2), 'Service restart never began.')
            save_thread.start()
            self.assertTrue(save_entered.wait(2), 'Second window never attempted its save.')
            self.assertFalse(save_finished.wait(0.1), 'Save completed before the in-flight restart returned.')
            self.assertEqual(read_saved(), before)
        finally:
            restart_release.set()
            restart_thread.join(3)
            if save_thread.ident is not None:
                save_thread.join(3)
        self.assertFalse(restart_thread.is_alive(), 'Restart request did not finish.')
        self.assertFalse(save_thread.is_alive(), 'Save request did not finish.')
        self.assertEqual(failures, [])
        self.assertTrue(save_finished.is_set())
        self.assertNotEqual(read_saved(), before)
        self.assertTrue(self.control.state()['service']['restart_required'])

    def test_settings_saved_during_restart_wait_and_keep_the_new_restart_flag(self):
        revision = self.control.revision()
        other_window = WebControl(self.config_path, self.env_path, gh=self.gh,
                                  runner=self.runner, accounts=self.control.accounts())
        self.assert_save_waits_for_restart(
            lambda: other_window.save_settings({'revision': revision, 'settings': {'poll_interval_seconds': 90}}),
            self.config_path.read_bytes)
        self.assertEqual(self.control.config().poll_interval_seconds, 90)

    def test_credential_saved_during_restart_waits_and_keeps_a_restart_flag(self):
        other_window = WebControl(self.config_path, self.env_path, gh=self.gh,
                                  runner=self.runner, accounts=self.control.accounts())
        self.assert_save_waits_for_restart(
            lambda: other_window.credential({'kind': 'model', 'value': 'test-key-after-restart', 'supplied_by': 'Alice'}),
            self.env_path.read_bytes)
        self.assertEqual(load_env_file(self.env_path)['GHA_MODEL_API_KEY'], 'test-key-after-restart')


class TaskAndWatchTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_enqueue_records_member_and_destination_and_prevents_duplicate_work(self):
        response = self.control.enqueue(self.task_data())
        self.assertEqual(response['job']['notes']['output_dir'], str(self.output))
        self.assertEqual(response['job']['notes']['member'], 'alice')
        self.assertEqual(response['job']['requested_by'], 42)
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.control.queue().all()), 1)

    def test_enqueue_requires_open_item_running_worker_and_applied_config(self):
        self.gh.item['state'] = 'MERGED'
        with self.assertRaises(ApiError):
            self.control.enqueue(self.task_data())
        self.gh.item['state'] = 'OPEN'
        self.runner.active = False
        with self.assertRaises(ApiError):
            self.control.enqueue(self.task_data())
        self.runner.active = True
        self.control.controls().change(lambda value: value.update(config_revision=self.control.revision()))
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.control.queue().all(), [])

    def test_completed_issue_can_be_submitted_after_a_real_issue_revision(self):
        first = self.control.enqueue(self.task_data(kind='implement-issue', number=8))['job']
        self.assertEqual(first['head_sha'], '')
        completed = self.control.queue().get(first['job_id'])
        completed.state = JobState.DONE
        self.control.queue().update(completed)
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data(kind='implement-issue', number=8))
        self.assertEqual(caught.exception.status, 409)
        self.gh.item['updatedAt'] = '2026-10-19T02:00:00Z'
        revised = self.control.enqueue(self.task_data(kind='implement-issue', number=8))['job']
        self.assertNotEqual(revised['dedup_key'], first['dedup_key'])
        self.assertNotEqual(revised['job_id'], first['job_id'])

    def test_github_issue_view_requests_the_revision_used_for_deduplication(self):
        client = make_client()
        timestamp = '2026-10-19T01:00:00Z'
        client.runner.expect_json({'number': 8, 'title': 'Reader', 'state': 'OPEN', 'updatedAt': timestamp})
        item = client.issue_view('owner/repo', 8)
        argv = client.runner.last_call.argv
        requested_fields = argv[argv.index('--json') + 1].split(',')
        self.assertIn('updatedAt', requested_fields)
        self.assertEqual(item['updatedAt'], timestamp)

    def test_stop_is_persisted_and_only_failed_or_stopped_tasks_retry(self):
        job = self.control.enqueue(self.task_data())['job']
        with self.assertRaises(ApiError):
            self.control.job_action(job['job_id'], 'retry')
        stopped = self.control.job_action(job['job_id'], 'stop')['job']
        self.assertEqual(stopped['state'], 'stopped')
        retried = self.control.job_action(job['job_id'], 'retry')['job']
        self.assertNotEqual(retried['job_id'], job['job_id'])
        self.assertEqual(retried['notes']['output_dir'], str(self.output))

    def test_selected_watch_starts_after_existing_comments_and_requires_output(self):
        data = self.task_data(mode='selected')
        self.control.add_watch(data)
        saved = self.control.watches().read()['owner/repo']['7']
        self.assertEqual(saved['last_review_comment_id'], 30)
        self.assertEqual(saved['scope'], 'selected')
        self.assertEqual(saved['output_dir'], str(self.output))
        self.assertEqual(self.control.queue().all(), [])
        data.pop('output_dir')
        with self.assertRaisesRegex(ApiError, 'No destination folder specified'):
            self.control.add_watch(data)

    def test_all_scope_and_selected_watch_are_reported_without_internal_children(self):
        self.control.add_watch(self.task_data(mode='all'))
        self.control.watches().put('owner/repo', 8, {'scope': 'all', 'member': 'alice'})
        self.control.add_watch(self.task_data(mode='selected'))
        watches = self.control.state()['watches']
        self.assertEqual({(w['mode'], w['number']) for w in watches}, {('all', None), ('selected', 7)})

    def test_removing_a_watch_during_automatic_work_stays_removed_after_completion(self):
        for mode in ('selected', 'all'):
            with self.subTest(mode=mode):
                self.control.add_watch(self.task_data(mode=mode))
                if mode == 'all':
                    self.control.watches().put('owner/repo', 7, {'scope': 'all', 'member': 'alice'})
                job = self.persisted_job(job_id=f'job_auto_{mode}', dedup_key=f'auto-{mode}',
                    notes={'output_dir': str(self.output), 'member': 'alice', 'auto_watch': True})
                queue = self.control.queue()
                control = self.control
                class CompleteAfterRemoval:
                    def execute(inner, current):
                        # Removal occurs after execution starts and before it
                        # completes, exactly when a member can remove a watch.
                        control.watches().remove('owner/repo', '*' if mode == 'all' else 7)
                        current.state = JobState.DONE
                        queue.update(current)
                worker = Worker(self.gh, queue, CompleteAfterRemoval(), self.control.config(),
                                env={'GHA_MODEL_API_KEY': 'fake-test-key'})
                worker._drain_jobs()
                self.assertEqual(queue.get(job.job_id).state, JobState.DONE)
                self.assertEqual(control.watches().read(), {})

    def test_issues_picker_excludes_pull_requests(self):
        items = self.control.targets('owner/repo', 'implement-issue')['items']
        self.assertEqual([item['number'] for item in items], [8])

    def test_restart_requires_idle_worker_but_pause_preserves_queued_work(self):
        self.persisted_job()
        with self.assertRaises(ApiError) as caught:
            self.control.service_action({'action': 'restart'})
        self.assertEqual(caught.exception.status, 409)
        self.control.service_action({'action': 'pause'})
        self.assertFalse(self.control.state()['service']['polling_enabled'])
        self.assertEqual(self.control.queue().get('job_test').state, JobState.RECEIVED)
        self.control.service_action({'action': 'resume'})
        self.assertTrue(self.control.state()['service']['polling_enabled'])

    def test_start_does_not_claim_pending_config_applied_to_an_already_running_worker(self):
        self.control.controls().change(lambda value: value.update(config_revision=self.control.revision()))
        try:
            self.control.service_action({'action': 'start'})
        except ApiError:
            pass
        self.assertTrue(self.control.state()['service']['restart_required'])

    def test_provider_probe_requires_explicit_confirmation_and_runs_once(self):
        with self.assertRaises(ApiError):
            self.control.provider_check({})
        self.assertEqual(self.runner.calls, [])
        response = self.control.provider_check({'confirmed': True})
        self.assertTrue(response['ok'])
        self.assertEqual(sum('provider-check' in argv for argv, _ in self.runner.calls), 1)


class TaskFileTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.artifacts = self.output / 'gha-worker-job_test'
        self.artifacts.mkdir()
        (self.output / 'unrelated-private-project.txt').write_text('Not this task')
        (self.artifacts / 'report.json').write_text('{"summary":"Done"}')
        self.persisted_job(notes={'output_dir': str(self.output), 'member': 'alice', 'artifact_dir': str(self.artifacts)})

    def test_task_browser_is_scoped_to_this_task_export(self):
        names = [item['name'] for item in self.control.job_files('job_test')['items']]
        self.assertEqual(names, ['report.json'])
        self.assertEqual(self.control.job_files('job_test', 'report.json')['content'], '{"summary":"Done"}')
        for path in ('../unrelated-private-project.txt', str(self.output / 'unrelated-private-project.txt')):
            with self.subTest(path=path), self.assertRaises(ApiError) as caught:
                self.control.job_files('job_test', path)
            self.assertEqual(caught.exception.status, 403)

    def test_task_preview_blocks_hidden_symlink_escapes_secrets_and_binary(self):
        (self.artifacts / 'escape.txt').symlink_to(self.output / 'unrelated-private-project.txt')
        (self.artifacts / '.env').write_text('PRIVATE=secret')
        (self.artifacts / 'key.pem').write_text('private key')
        (self.artifacts / 'binary.dat').write_bytes(b'\x00\x01')
        for path in ('escape.txt', '.env', 'key.pem', 'binary.dat'):
            with self.subTest(path=path), self.assertRaises(ApiError):
                self.control.job_files('job_test', path)

    def test_task_preview_is_bounded_and_reports_truncation(self):
        (self.artifacts / 'large.txt').write_text('a' * 100100)
        response = self.control.job_files('job_test', 'large.txt')
        self.assertTrue(response['truncated'])
        self.assertLessEqual(len(response['content']), 100000)

    def test_legacy_job_without_destination_cannot_browse_company_state(self):
        self.persisted_job(job_id='job_legacy', dedup_key='legacy', notes={})
        with self.assertRaises(ApiError) as caught:
            self.control.job_files('job_legacy')
        self.assertEqual(caught.exception.status, 404)


class GitHubCredentialCompatibilityTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_gui_uses_saved_github_token_without_forwarding_saved_model_key(self):
        self.env_path.write_text('GH_TOKEN=test-github-credential\nGHA_MODEL_API_KEY=test-model-credential\n')
        control = WebControl(self.config_path, self.env_path, runner=self.runner, accounts=self.control.accounts())
        seen = {}
        def run(argv, **kwargs):
            seen.update(kwargs['env'])
            return subprocess.CompletedProcess(argv, 0, '{"login":"member","id":42}', '')
        with patch.dict('os.environ', {}, clear=True), patch('ghagent.web_control.find_gh', return_value='/fixture/gh'), patch('ghagent.web_control.subprocess.run', side_effect=run):
            response = control.github_status()
        self.assertEqual(response['login'], 'member')
        self.assertEqual(seen['GH_TOKEN'], 'test-github-credential')
        self.assertNotIn('GHA_MODEL_API_KEY', seen)
        self.assertNotIn('test-github-credential', json.dumps(response))

    def test_browser_reconnection_refuses_overriding_saved_github_token(self):
        self.env_path.write_text('GH_TOKEN=test-github-credential\n')
        with patch('ghagent.web_control.subprocess.Popen') as process:
            with self.assertRaises(ApiError) as caught:
                self.control.connect_github()
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('saved GitHub token', str(caught.exception))
        process.assert_not_called()
