"""Member-visible recovery and truthful watches, using temporary real records."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from ghagent.control_store import WatchStore, atomic_json
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.learning import LearningEngine, LearningError
from ghagent.worker import AUTO_JOBS_PER_PR_PER_DAY, _now_iso
from tests.test_worker_loop import FakeGh, make_worker
from tests.test_web_control import GuiFixture
from tests import test_web_http as http_fixture


class SavedLessonsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'member' / 'reports'
        self.output.mkdir(parents=True)
        self.engine = LearningEngine(self.root / 'state', self.root / 'scratch')

    def start(self, account='member'):
        return self.engine.start(dict(request_id=str(uuid.uuid4()), mode='guide', lesson='git-basics',
                                      account=account, output_dir=str(self.output)))

    def test_another_browser_can_find_saved_lessons_for_the_selected_member(self):
        first = self.start()
        other = self.start('someone-else')
        second = self.start()
        fresh_engine = LearningEngine(self.root / 'state', self.root / 'scratch')
        found = fresh_engine.list_sessions('member')
        self.assertEqual({row['id'] for row in found}, {first['id'], second['id']})
        self.assertNotIn(other['id'], {row['id'] for row in found})
        self.assertTrue(all(row['readable'] for row in found))
        self.assertTrue(all(row['output_dir'] == str(self.output) for row in found))

    def test_unreadable_record_remains_visible_and_bytes_are_preserved(self):
        session = self.start()
        damaged = self.root / 'state' / (str(uuid.uuid4()) + '.json')
        damaged.write_text('{broken record')
        original = damaged.read_bytes()
        rows = self.engine.list_sessions('member')
        self.assertEqual(len(rows), 2)
        error = next(row for row in rows if not row['readable'])
        self.assertEqual(error['status'], 'unavailable')
        self.assertIn('preserved', error['error'])
        self.assertIn('restore', error['next_step'])
        self.assertEqual(damaged.read_bytes(), original)
        self.assertIn(session['id'], {row['id'] for row in rows})

    def test_lesson_directory_read_error_is_not_reported_as_no_saved_lessons(self):
        self.start()
        with patch.object(Path, 'iterdir', side_effect=PermissionError('blocked')):
            with self.assertRaisesRegex(LearningError, 'could not be listed'):
                self.engine.list_sessions('member')

    def test_destination_recovery_keeps_the_lesson_and_receipts(self):
        session = self.start()
        target = self.output.parent / 'new-folder'
        target.mkdir()
        before = self.engine.get(session['id'])
        changed = self.engine.change_destination(session['id'], 'member', str(target), before['revision'])
        self.assertEqual(changed['id'], before['id'])
        self.assertEqual(changed['receipts'], before['receipts'])
        self.assertEqual(changed['step'], before['step'])
        self.assertEqual(changed['output_dir'], str(target))
        self.assertEqual(changed['destination_history'][0]['previous_output_dir'], str(self.output))
        self.assertEqual(changed['destination_history'][0]['source'], 'member-choice')
        self.assertEqual(LearningEngine(self.root / 'state', self.root / 'scratch').get(session['id'])['output_dir'], str(target))

    def test_destination_change_rejects_another_member_stale_revision_and_active_action(self):
        session = self.start()
        with self.assertRaisesRegex(LearningError, 'original member account'):
            self.engine.change_destination(session['id'], 'someone-else', str(self.output), session['revision'])
        with self.assertRaisesRegex(LearningError, 'changed'):
            self.engine.change_destination(session['id'], 'member', str(self.output), 0)
        store = self.engine._store(session['id'])
        state = store.read()
        state['operation'] = {'id': str(uuid.uuid4()), 'action': 'export'}
        atomic_json(store.path, state)
        with self.assertRaisesRegex(LearningError, 'still working'):
            self.engine.change_destination(session['id'], 'member', str(self.output), state['revision'])
        self.assertEqual(store.read(), state)

    def test_destination_change_rejects_a_link_and_preserves_the_old_folder(self):
        session = self.start()
        link = self.output.parent / 'linked-results'
        link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(LearningError, 'symbolic'):
            self.engine.change_destination(session['id'], 'member', str(link), session['revision'])
        self.assertEqual(self.engine.get(session['id'])['output_dir'], str(self.output))


class WatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'member'
        self.output = self.home / 'reports'
        self.output.mkdir(parents=True)
        self.gh = FakeGh(prs={('o/r', 5): {'state': 'OPEN', 'headRefOid': 'a' * 40}})
        self.worker = make_worker(self.root, gh=self.gh)
        self.addCleanup(self.worker.close)
        self.store = WatchStore(self.worker.state_dir / 'watched.json')
        self.meta = dict(scope='selected', output_dir=str(self.output), member='member',
                         member_home=str(self.home), last_review_comment_id=0)

    def saved(self, key='5'):
        return self.store.read()['o/r'][key]

    def feedback(self, author=7, number=101):
        self.gh.review_comments = [dict(id=number, body='Please fix this return value.',
                                       created_at='2026-11-09T00:00:00Z', html_url='https://github.com/o/r/pull/5#discussion_r101',
                                       user={'id': author, 'login': 'reviewer'})]

    def test_folderless_scope_does_not_discover_or_start_work(self):
        self.store.put('o/r', '*', {'scope': 'all'})
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.gh.rest_calls, [])
        self.assertEqual(set(self.store.read()['o/r']), {'*'})
        self.assertEqual(self.saved('*')['watch_status'], 'needs-folder')
        self.assertIn('Choose', self.saved('*')['status_reason'])

    def test_disabled_scope_does_not_discover_or_start_derived_work(self):
        self.store.put('o/r', '*', dict(self.meta, scope='all', enabled=False))
        self.store.put('o/r', '5', dict(self.meta, scope='all'))
        self.feedback()
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.gh.rest_calls, [])
        self.assertEqual(self.gh.pr_view_calls, [])
        self.assertEqual(self.saved('*')['watch_status'], 'disabled')

    def test_removed_repository_keeps_its_watch_with_a_repair_instruction(self):
        self.store.put('o/r', 5, self.meta)
        self.worker.cfg.repos = []
        self.feedback()
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.saved()['watch_status'], 'repository-removed')
        self.assertIn('Repositories', self.saved()['status_reason'])
        self.assertEqual(self.gh.pr_view_calls, [])

    def test_deleted_destination_is_explained_before_any_task_is_queued(self):
        self.store.put('o/r', 5, self.meta)
        self.output.rmdir()
        self.feedback()
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.saved()['watch_status'], 'needs-folder')
        self.assertIn('Edit this watch', self.saved()['status_reason'])
        self.assertEqual(self.worker.queue.all(), [])

    def test_ineligible_author_is_explained_without_advancing_cursor(self):
        self.store.put('o/r', 5, self.meta)
        self.feedback(author=88)
        self.assertEqual(self.worker._poll_watched(), 0)
        state = self.saved()
        self.assertEqual(state['watch_status'], 'author-not-allowed')
        self.assertIn('Settings', state['status_reason'])
        self.assertEqual(state['last_review_comment_id'], 0)
        self.assertFalse(state['latest_feedback']['eligible'])
        self.assertTrue(state['last_checked_at'])
        self.assertTrue(state['last_success_at'])

    def test_daily_cap_exposes_reset_and_preserves_feedback(self):
        self.store.put('o/r', 5, self.meta)
        self.feedback()
        for i in range(AUTO_JOBS_PER_PR_PER_DAY):
            self.worker.queue.add(Job(f'cap_{i}', JobKind.FIX_PR, 'o/r', 5, i, 7,
                                      state=JobState.DONE, dedup_key=f'auto-fix-pr:o/r:5:{i}', created_at=_now_iso()))
        self.assertEqual(self.worker._poll_watched(), 0)
        state = self.saved()
        self.assertEqual(state['watch_status'], 'daily-limit')
        self.assertTrue(state['cap_reset_at'])
        self.assertEqual(state['auto_jobs_today'], 6)
        self.assertEqual(state['last_review_comment_id'], 0)

    def test_new_eligible_feedback_uses_saved_destination_and_is_not_duplicated(self):
        self.store.put('o/r', 5, self.meta)
        self.feedback()
        self.assertEqual(self.worker._poll_watched(), 1)
        job = self.worker.queue.all()[0]
        self.assertEqual(job.notes['output_dir'], str(self.output))
        self.assertEqual(job.notes['member'], 'member')
        self.assertEqual(self.saved()['watch_status'], 'queued')
        self.assertEqual(self.saved()['last_review_comment_id'], 101)
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(len(self.worker.queue.all()), 1)

    def test_lookup_error_is_visible_on_the_scope_row_and_cursor_is_preserved(self):
        self.store.put('o/r', '*', dict(self.meta, scope='all'))
        self.store.put('o/r', 5, dict(self.meta, scope='all'))
        with patch.object(self.gh, 'rest', side_effect=RuntimeError('a private credential should never be shown')):
            self.assertEqual(self.worker._poll_watched(), 0)
        state = self.saved('*')
        self.assertEqual(state['watch_status'], 'lookup-failed')
        self.assertIn('access', state['status_reason'])
        self.assertNotIn('private credential', json.dumps(state))
        self.assertEqual(self.saved()['last_review_comment_id'], 0)

    def test_closed_pr_is_kept_as_history_without_closing_the_all_pr_scope(self):
        class ScopeGh(FakeGh):
            def rest(inner, method, path, **kwargs):
                if path == 'repos/o/r/pulls':
                    return []
                return super(ScopeGh, inner).rest(method, path, **kwargs)
        self.gh = ScopeGh(prs={('o/r', 5): {'state': 'MERGED'}})
        self.worker.gh = self.gh
        self.store.put('o/r', '*', dict(self.meta, scope='all'))
        self.store.put('o/r', 5, dict(self.meta, scope='all'))
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.saved()['watch_status'], 'closed')
        self.assertTrue(self.saved()['closed'])
        self.assertFalse(self.saved('*').get('closed', False))

    def test_removed_watch_is_not_recreated_when_github_returns(self):
        self.store.put('o/r', 5, self.meta)
        self.feedback()
        original = self.gh.pr_view
        def lookup(repo, number):
            self.store.remove(repo, number)
            return original(repo, number)
        self.gh.pr_view = lookup
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.worker.queue.all(), [])
        self.assertFalse(self.store.read().get('o/r'))

    def test_destination_edited_during_lookup_is_used_and_not_overwritten(self):
        self.store.put('o/r', 5, self.meta)
        self.feedback()
        replacement = self.home / 'new-results'
        replacement.mkdir()
        original = self.gh.pr_view
        def lookup(repo, number):
            self.store.put(repo, number, {'output_dir': str(replacement)}, only_existing=True)
            return original(repo, number)
        self.gh.pr_view = lookup
        self.assertEqual(self.worker._poll_watched(), 1)
        self.assertEqual(self.worker.queue.all()[0].notes['output_dir'], str(replacement))
        self.assertEqual(self.saved()['output_dir'], str(replacement))


class LearningRecoveryHttpTests(GuiFixture, unittest.TestCase):
    setUp = http_fixture.GuiHttpTests.setUp
    stop_server = http_fixture.GuiHttpTests.stop_server
    request = http_fixture.GuiHttpTests.request

    def start(self):
        status, _, body = self.request('POST', '/api/learn', dict(
            action='start', request_id=str(uuid.uuid4()), mode='guide', lesson='git-basics',
            account='alice', output_dir=str(self.output)))
        self.assertEqual(status, 200, body)
        return json.loads(body)['session']

    def test_saved_lessons_route_finds_the_session_without_browser_storage(self):
        session = self.start()
        status, _, body = self.request('GET', '/api/learn/sessions?account=alice')
        self.assertEqual(status, 200, body)
        rows = json.loads(body)['items']
        self.assertEqual([row['id'] for row in rows], [session['id']])
        self.assertTrue(rows[0]['readable'])

    def test_destination_route_preserves_errors_and_does_not_export_or_run(self):
        session = self.start()
        target = self.home / 'new-lesson-results'
        target.mkdir()
        payload = dict(action='destination', id=session['id'], account='alice',
                       output_dir=str(target), revision=session['revision'])
        status, _, body = self.request('POST', '/api/learn', payload)
        self.assertEqual(status, 200, body)
        changed = json.loads(body)['session']
        self.assertEqual(changed['output_dir'], str(target))
        self.assertEqual(changed['receipts'], session['receipts'])
        self.assertEqual(list(target.iterdir()), [])
        status, _, body = self.request('POST', '/api/learn', payload)
        self.assertEqual(status, 409, body)
        self.assertIn(b'Reload', body)

    def test_transcript_download_survives_a_missing_export_destination(self):
        session = self.start()
        self.output.rmdir()
        status, headers, body = self.request('GET', '/api/learn/transcript?id=' + session['id'])
        self.assertEqual(status, 200, body)
        self.assertIn('attachment', headers.get('Content-Disposition', ''))
        self.assertIn(b'FatChin learning session', body)
        self.assertIn(b'Status: ready', body)


if __name__ == '__main__':
    unittest.main()
