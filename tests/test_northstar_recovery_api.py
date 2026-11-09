"""Recovery controls exercised with real temporary stores and Git files."""
from __future__ import annotations

import copy
import contextlib
import http.client
import json
import os
import threading
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from ghagent.interfaces import JobKind, JobState
from ghagent.recovery_control import RecoveryControls
from ghagent.web_control import ApiError, WebControl
from tests.test_web_control import GuiFixture


RecoveryWebControl = type(
    "RecoveryWebControl",
    (WebControl,) if issubclass(WebControl, RecoveryControls) else (RecoveryControls, WebControl),
    {},
)


class RecoveryAPITests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.control.__class__ = RecoveryWebControl

    def job(self, **changes):
        return self.persisted_job(state=JobState.DONE, **changes)

    def test_archive_restore_preserve_original_state_error_and_receipts(self):
        job = self.job(error='Original error', notes={'member': 'alice', 'output_dir': str(self.output),
            'posted': [{'name': 'draft', 'result': 'Created issue #4'}], 'checks': [{'exit': 1}]})
        original = self.control.queue().get(job.job_id)
        reply = self.control.job_recovery(job.job_id, {'action': 'archive', 'reason': 'Handled outside the app'})
        self.assertEqual(reply['job']['state'], 'done')
        self.assertEqual(reply['job']['error'], original.error)
        self.assertEqual(reply['job']['notes']['posted'], original.notes['posted'])
        self.assertTrue(reply['job']['notes']['archived_at'])
        restored = self.control.job_recovery(job.job_id, {'action': 'restore'})['job']
        self.assertNotIn('archived_at', restored['notes'])
        self.assertEqual(restored['notes']['archive_history'][0]['reason'], 'Handled outside the app')
        self.assertEqual(restored['state'], 'done')
        self.assertEqual(self.gh.calls, [])

    def test_archive_rejects_active_and_waiting_decision(self):
        for state in (JobState.WORKING, JobState.WAITING_ON_YOU):
            with self.subTest(state=state):
                job = self.persisted_job(job_id='job_' + state.value.replace('-', '_'), dedup_key=str(state), state=state)
                with self.assertRaisesRegex(ApiError, 'finish or stop'):
                    self.control.job_recovery(job.job_id, {'action': 'archive'})
                self.assertEqual(self.control.queue().get(job.job_id).state, state)

    def test_uncertain_post_requires_human_confirmation_and_keeps_marker(self):
        job = self.persisted_job(state=JobState.WAITING_ON_YOU, notes={'member': 'alice',
            'posting': 'issue-1', 'phase': 'posting-unconfirmed',
            'drafts': [{'name': 'issue-1', 'title': 'Fix', 'body': 'Fix the problem'}]})
        with self.assertRaisesRegex(ApiError, 'confirm'):
            self.control.job_recovery(job.job_id, {'action': 'retire-post', 'outcome': 'posted'})
        reply = self.control.job_recovery(job.job_id, {'action': 'retire-post', 'outcome': 'unknown', 'confirmed': True})
        self.assertEqual(reply['job']['state'], 'stopped')
        notes = reply['job']['notes']
        self.assertEqual(notes['posting'], 'issue-1')
        self.assertEqual(notes['post_recovery']['reported_by'], 'user')
        self.assertFalse(notes['post_recovery']['observed'])
        self.assertEqual(notes['post_recovery']['outcome'], 'unknown')
        with self.assertRaisesRegex(ApiError, 'not waiting'):
            self.control.draft_action(job.job_id, {'action': 'post'})
        self.assertEqual(self.gh.calls, [])

    def test_uncertain_executor_publication_can_be_retired_without_clearing_evidence(self):
        job = self.persisted_job(state=JobState.FAILED, notes={'member': 'alice',
            'publish_outcome': 'uncertain', 'attempted_commit': 'a' * 40})
        checked = self.control.job_recovery(job.job_id, {'action': 'recheck-post'})
        self.assertEqual(checked['outcome'], 'unknown')
        self.assertIn('cannot prove', checked['message'])
        retired = self.control.job_recovery(job.job_id, {'action': 'retire-post', 'confirmed': True, 'outcome': 'unknown'})
        self.assertEqual(retired['job']['state'], 'stopped')
        self.assertEqual(retired['job']['notes']['publish_outcome'], 'uncertain')
        self.assertEqual(retired['job']['notes']['attempted_commit'], 'a' * 40)
        self.assertFalse(retired['job']['notes']['post_recovery']['observed'])
        self.assertEqual(self.gh.calls, [])

    def test_recheck_only_reads_and_never_claims_remote_authorship(self):
        job = self.persisted_job(kind=JobKind.TRIAGE_ISSUES, state=JobState.WAITING_ON_YOU,
            notes={'posting': 'issue-1', 'drafts': [{'name': 'issue-1', 'title': 'Fix', 'body': 'Text'}]})
        def remote(method, path, **kwargs):
            self.assertEqual(method, 'GET')
            return [{'title': 'Fix', 'body': 'Text', 'html_url': 'https://github.com/owner/repo/issues/10', 'number': 10}]
        self.gh.rest = remote
        reply = self.control.job_recovery(job.job_id, {'action': 'recheck-post'})
        self.assertEqual(reply['outcome'], 'unknown')
        self.assertEqual(reply['job']['state'], 'waiting-on-you')
        self.assertEqual(reply['evidence'][0]['number'], 10)
        self.assertEqual(reply['job']['notes']['posting'], 'issue-1')

    def test_export_existing_work_creates_new_copy_without_changing_old_files(self):
        scratch = self.worktrees / 'job-copy'
        scratch.mkdir()
        def git(*args):
            return subprocess.run(['git', *args], cwd=scratch, capture_output=True, text=True, check=True).stdout.strip()
        git('init'); git('config', 'user.email', 'audit@example.test'); git('config', 'user.name', 'Audit')
        (scratch / 'file.txt').write_text('before\n')
        git('add', 'file.txt'); git('commit', '-m', 'Initial')
        base = git('rev-parse', 'HEAD')
        (scratch / 'file.txt').write_text('after\n')
        (scratch / 'new.txt').write_text('new evidence\n')
        old = self.output / 'gha-worker-job_test';old.mkdir();(old / 'report.md').write_text('Original export')
        job = self.job(head_sha=base, notes={'member': 'alice', 'member_home': str(self.home),
            'output_dir': str(self.output), 'worktree_path': str(scratch), 'changed_files': ['file.txt'],
            'result_export_error': 'Old output failed'})
        reply = self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
        notes = reply['job']['notes'];new = Path(notes['artifact_dir'])
        self.assertEqual((new / 'files/file.txt').read_text(), 'after\n')
        self.assertEqual((new / 'files/new.txt').read_text(), 'new evidence\n')
        self.assertEqual((old / 'report.md').read_text(), 'Original export')
        self.assertTrue((new / 'changes.patch').exists())
        self.assertNotIn('result_export_error', notes)
        self.assertEqual(notes['export_history'][0]['remote_writes'], 0)
        self.assertEqual(reply['job']['state'], 'done')
        self.assertTrue(scratch.exists())
        self.assertEqual(self.gh.calls, [])

    def test_export_refuses_missing_files_without_claiming_success(self):
        job = self.job(notes={'member': 'alice', 'output_dir': str(self.output),
            'worktree_path': str(self.worktrees / 'missing'), 'changed_files': ['file.txt']})
        with self.assertRaisesRegex(ApiError, 'could not be found'):
            self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
        self.assertEqual(self.control.queue().get(job.job_id).notes['output_dir'], str(self.output))

    def test_recovery_uses_recorded_artifact_folder_after_old_parent_disappears(self):
        artifacts = self.home / 'retained-results' / 'gha-worker-job_test'
        artifacts.mkdir(parents=True)
        (artifacts / 'report.md').write_text('Original receipt')
        (artifacts / 'files').mkdir()
        (artifacts / 'files/file.txt').write_text('Saved result')
        job = self.job(notes={'member': 'alice', 'member_home': str(self.home),
            'output_dir': str(self.home / 'old-removed-folder'),
            'artifact_dir': str(artifacts), 'changed_files': ['file.txt']})
        reply = self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
        root = Path(reply['job']['notes']['artifact_dir'])
        self.assertEqual((root / 'files/file.txt').read_text(), 'Saved result')
        self.assertEqual((artifacts / 'report.md').read_text(), 'Original receipt')
        self.assertEqual(reply['job']['notes']['export_history'][-1]['previous_artifact_dir'], str(artifacts))
        self.assertIn('Original receipt', (artifacts / 'report.md').read_text())

    def test_recovery_refuses_artifact_folder_outside_the_member_account(self):
        other = self.root / 'other-person' / 'gha-worker-job_test'
        other.mkdir(parents=True);(other / 'private.txt').write_text('Other member')
        job = self.job(notes={'member': 'alice', 'output_dir': str(self.home / 'missing'),
            'artifact_dir': str(other), 'changed_files': ['private.txt']})
        with self.assertRaisesRegex(ApiError, 'could not be found'):
            self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
        self.assertFalse(list(self.output.iterdir()))

    def test_recovery_never_follows_a_link_in_retained_exports(self):
        artifacts = self.output / 'gha-worker-job_test';artifacts.mkdir()
        outside = self.root / 'private.txt';outside.write_text('Do not copy')
        (artifacts / 'linked.txt').symlink_to(outside)
        (artifacts / 'safe.txt').write_text('Copy this')
        job = self.job(notes={'member': 'alice', 'output_dir': str(self.output),
            'artifact_dir': str(artifacts), 'exported_files': ['safe.txt']})
        reply = self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
        root = Path(reply['job']['notes']['artifact_dir'])
        self.assertEqual((root / 'safe.txt').read_text(), 'Copy this')
        self.assertFalse((root / 'linked.txt').exists())
        self.assertIn('linked.txt', reply['job']['notes']['recovery_copy_skipped'])
        self.assertIn('Some saved files were skipped', reply['job']['notes']['result_export_error'])

    def test_archive_and_export_refuse_busy_job_without_changing_receipts(self):
        from ghagent.fleet_locks import repo_lock
        job = self.job(notes={'member': 'alice', 'output_dir': str(self.output), 'receipt': 'keep'})
        with repo_lock(str(self.state), 'owner/repo', exclusive=True, blocking=False):
            with self.assertRaisesRegex(ApiError, 'still using this project'):
                self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
            # An archive changes no execution or project files and remains usable.
            archived = self.control.job_recovery(job.job_id, {'action': 'archive'})
            self.assertEqual(archived['job']['notes']['receipt'], 'keep')
        self.assertEqual(self.control.queue().get(job.job_id).notes['receipt'], 'keep')

    def test_switching_to_a_new_selected_pr_records_existing_feedback_as_start(self):
        self.control.watches().put('owner/repo', 7, {'scope': 'selected', 'last_review_comment_id': 123})
        reply = self.control.edit_watch({'repo': 'owner/repo', 'mode': 'selected', 'number': 7,
            'new_mode': 'selected', 'new_number': 8, 'confirmed_scope_change': True,
            'account': 'alice', 'output_dir': str(self.output)})
        saved = self.control.watches().read()['owner/repo']
        self.assertEqual(saved['8']['last_review_comment_id'], 30)
        self.assertFalse(saved['7']['enabled'])
        self.assertEqual(saved['7']['last_review_comment_id'], 123)
        self.assertEqual(reply['watch']['number'], 8)

    def test_copy_refuses_a_file_replaced_by_a_link_during_recovery(self):
        artifacts = self.output / 'gha-worker-job_test';artifacts.mkdir()
        source = artifacts / 'file.txt';source.write_text('Original result')
        outside = self.root / 'private.txt';outside.write_text('Do not copy')
        job = self.job(notes={'member': 'alice', 'output_dir': str(self.output),
            'artifact_dir': str(artifacts), 'exported_files': ['file.txt']})
        real_open = os.open
        switched = False
        def racing_open(path, flags, *args, **kwargs):
            nonlocal switched
            if path == 'file.txt' and kwargs.get('dir_fd') is not None and not switched:
                switched = True
                source.unlink();source.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)
        with patch('ghagent.recovery_control.os.open', side_effect=racing_open):
            with self.assertRaisesRegex(ApiError, 'original task'):
                self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'alice', 'output_dir': str(self.output)})
        saved = self.control.queue().get(job.job_id)
        self.assertEqual(saved.notes['output_dir'], str(self.output))
        self.assertEqual(saved.notes['artifact_dir'], str(artifacts))
        self.assertTrue(saved.notes['export_attempts'])
        for result in self.output.glob('recovery-*/gha-worker-*/file.txt'):
            self.assertNotEqual(result.read_text(), 'Do not copy')

    def test_watch_edit_keeps_cursor_updated_during_github_lookup(self):
        self.control.watches().put('owner/repo', 7, {'scope': 'selected', 'last_review_comment_id': 123})
        original = self.gh.pr_view
        def during_lookup(repo, number):
            self.control.watches().put(repo, number, {'last_review_comment_id': 999, 'last_success_at': 'newer-poll'})
            return original(repo, number)
        self.gh.pr_view = during_lookup
        self.control.edit_watch({'repo': 'owner/repo', 'mode': 'selected', 'number': 7,
            'account': 'alice', 'output_dir': str(self.output)})
        saved = self.control.watches().read()['owner/repo']['7']
        self.assertEqual(saved['last_review_comment_id'], 999)
        self.assertEqual(saved['last_success_at'], 'newer-poll')
        self.assertEqual(saved['output_dir'], str(self.output))

    def test_watch_removed_during_lookup_is_not_recreated(self):
        self.control.watches().put('owner/repo', 7, {'scope': 'selected', 'last_review_comment_id': 123})
        original = self.gh.pr_view
        def removed(repo, number):
            self.control.watches().remove(repo, number)
            return original(repo, number)
        self.gh.pr_view = removed
        with self.assertRaisesRegex(ApiError, 'removed'):
            self.control.edit_watch({'repo': 'owner/repo', 'mode': 'selected', 'number': 7,
                'account': 'alice', 'output_dir': str(self.output)})
        self.assertNotIn('owner/repo', self.control.watches().read())

    @contextlib.contextmanager
    def http_api(self):
        from ghagent.web import make_server
        server = make_server(self.control, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        def request(method, path, data=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                body = None if data is None else json.dumps(data)
                headers = {} if data is None else {'Content-Type': 'application/json'}
                connection.request(method, path, body=body, headers=headers)
                reply = connection.getresponse()
                return reply.status, json.loads(reply.read())
            finally:
                connection.close()
        try:
            yield request
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_public_recovery_route_preserves_receipts_and_exposes_saved_result(self):
        job = self.job(error='Original reason', notes={'member': 'alice', 'receipt': 'Original receipt'})
        with self.http_api() as request:
            status, reply = request('POST', '/api/jobs/' + job.job_id + '/recovery', {'action': 'archive'})
            self.assertEqual(status, 200)
            self.assertTrue(reply['job']['notes']['archived_at'])
            status, report = request('GET', '/api/jobs/' + job.job_id + '/report')
            self.assertEqual(status, 200)
            self.assertEqual(report['notes']['receipt'], 'Original receipt')
            self.assertEqual(report['error'], 'Original reason')
            self.assertEqual(report['state'], 'done')
            status, reply = request('POST', '/api/jobs/' + job.job_id + '/recovery', {'action': 'restore'})
            self.assertEqual(status, 200)
            self.assertNotIn('archived_at', reply['job']['notes'])

    def test_public_watch_edit_and_rules_routes_read_or_update_only_intended_records(self):
        self.seed_watch()
        original = self.gh.rest
        def gh(method, path, **kwargs):
            if path.endswith('/rulesets'):
                self.assertEqual(method, 'GET')
                return []
            return original(method, path, **kwargs)
        self.gh.rest = gh
        with self.http_api() as request:
            status, reply = request('POST', '/api/watches/edit', {'repo': 'owner/repo', 'mode': 'all',
                'account': 'alice', 'output_dir': str(self.output)})
            self.assertEqual(status, 200)
            self.assertEqual(reply['watch']['last_review_comment_id'], 99)
            status, reply = request('GET', '/api/rules?repo=owner/repo')
            self.assertEqual(status, 200)
            self.assertEqual(reply['state'], 'verified')
            self.assertEqual(self.control.rules_inspection('owner/repo')['checked_at'], reply['checked_at'])

    def test_export_does_not_change_member_account(self):
        job = self.job()
        with self.assertRaisesRegex(ApiError, 'original member'):
            self.control.job_recovery(job.job_id, {'action': 'export', 'account': 'bob', 'output_dir': str(self.output)})

    def seed_watch(self):
        scope = {'scope': 'all', 'last_review_comment_id': 99, 'last_comment_at': 'original', 'member': 'alice'}
        self.control.watches().put('owner/repo', '*', scope)
        self.control.watches().put('owner/repo', 7, {**scope, 'last_review_comment_id': 123})
        return scope

    def test_watch_destination_edit_preserves_positions_and_updates_children(self):
        self.seed_watch()
        reply = self.control.edit_watch({'repo': 'owner/repo', 'mode': 'all', 'account': 'alice', 'output_dir': str(self.output)})
        saved = self.control.watches().read()['owner/repo']
        self.assertEqual(saved['*']['last_review_comment_id'], 99)
        self.assertEqual(saved['7']['last_review_comment_id'], 123)
        for meta in saved.values():
            self.assertEqual(meta['output_dir'], str(self.output))
            self.assertEqual(meta['member_home'], str(self.home))
        self.assertIn('earlier feedback positions', reply['message'])

    def test_scope_change_keeps_history_and_disables_previous_scope(self):
        self.seed_watch()
        data = {'repo': 'owner/repo', 'mode': 'all', 'new_mode': 'selected', 'new_number': 7,
                'account': 'alice', 'output_dir': str(self.output)}
        with self.assertRaisesRegex(ApiError, 'Confirm'):
            self.control.edit_watch(data)
        self.control.edit_watch({**data, 'confirmed_scope_change': True})
        saved = self.control.watches().read()['owner/repo']
        self.assertFalse(saved['*']['enabled'])
        self.assertTrue(saved['7']['enabled'])
        self.assertEqual(saved['7']['scope'], 'selected')
        self.assertEqual(saved['7']['last_review_comment_id'], 123)
        self.assertEqual(saved['7']['watch_history'][-1]['previous']['last_review_comment_id'], 99)

    def test_watch_health_requires_a_folder_and_fresh_success(self):
        rows = [{'repo': 'owner/repo', 'mode': 'all'},
                {'repo': 'owner/repo', 'mode': 'all', 'member': 'alice', 'member_home': str(self.home), 'output_dir': str(self.output), 'watch_status': 'watching'}]
        result = self.control.watch_health(rows)
        self.assertEqual(result[0]['health'], 'needs-folder')
        self.assertEqual(result[1]['health'], 'not-checked')
        self.runner.active = False
        self.assertEqual(self.control.watch_health([rows[1]])[0]['health'], 'worker-stopped')

    def test_rules_inspection_records_observation_not_safety_claim(self):
        self.gh.rest = lambda method, path, **kwargs: []
        result = self.control.inspect_rules('owner/repo')
        self.assertEqual(result['state'], 'verified')
        self.assertTrue(result['rulesets_readable'])
        self.assertIn('does not mean', result['next_step'])
        self.assertEqual(self.control.rules_inspections()['owner/repo']['checked_at'], result['checked_at'])
        def fail(*args, **kwargs):
            raise RuntimeError('no access sk-private-secret')
        self.gh.rest = fail
        result = self.control.inspect_rules('owner/repo')
        self.assertFalse(result['rulesets_readable'])
        self.assertNotIn('private-secret', result['reason'])
        self.assertIn('no publishing exception', result['next_step'])

    def test_contextual_diagnostics_does_not_run_model_or_project_commands(self):
        self.seed_watch()
        rows = self.control.contextual_diagnostics()
        watches = next(row for row in rows if row['name'] == 'Review watch destinations')
        self.assertEqual(watches['status'], 'warn')
        self.assertEqual(watches['action_id'], 'open-watches')
        self.assertFalse(any('provider-check' in args[0] for args in self.runner.calls))
        self.assertEqual(self.gh.calls, [])


if __name__ == '__main__':
    unittest.main()
