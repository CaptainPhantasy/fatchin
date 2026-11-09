"""Background-work promises must reflect usable watches and saved choices."""
import unittest
from unittest.mock import patch

from ghagent.web_control import ApiError
from tests.test_web_control import GuiFixture


class WatchTruthTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def meta(self, **extra):
        return {'member':'alice', 'member_home':str(self.home), 'output_dir':str(self.output),
                'enabled':True, 'scope':'selected', 'watch_status':'watching',
                'last_success_at':'2026-11-09T00:00:00Z', **extra}

    def fence_repo(self):
        return next(row for row in self.control.fence()['repos'] if row['repo'] == 'owner/repo')

    def test_folderless_saved_watch_is_not_reported_as_active(self):
        self.control.watches().put('owner/repo', 7, {'enabled':True})
        row = self.fence_repo()
        self.assertTrue(row['configured'])
        self.assertFalse(row['watches_armed'])
        self.assertEqual(row['watch_state'], 'Needs a results folder')
        self.assertIn('results folder', row['watch_next_step'])

    def test_stopped_worker_is_not_reported_as_watching(self):
        self.control.watches().put('owner/repo', 7, self.meta())
        self.runner.active = False
        row = self.fence_repo()
        self.assertFalse(row['watches_armed'])
        self.assertEqual(row['watch_state'], 'Worker stopped')
        self.assertIn('Start the worker', row['watch_next_step'])

    def test_healthy_watch_and_its_next_step_are_reported(self):
        self.control.watches().put('owner/repo', 7, self.meta())
        row = self.fence_repo()
        self.assertTrue(row['watches_armed'])
        self.assertEqual(row['watch_state'], 'Watching for new feedback')
        self.assertIn('automatically', row['watch_next_step'])

    def test_disabled_all_scope_parent_is_not_overridden_by_derived_child(self):
        self.control.watches().put('owner/repo', '*', self.meta(scope='all', enabled=False))
        self.control.watches().put('owner/repo', 7, self.meta(scope='all'))
        row = self.fence_repo()
        self.assertFalse(row['watches_armed'])
        self.assertEqual(row['watch_state'], 'Watch stopped')

    def test_paused_polling_does_not_claim_an_active_watch(self):
        self.control.watches().put('owner/repo', 7, self.meta())
        self.control.controls().change(lambda rows: rows.update(polling_paused=True))
        row = self.fence_repo()
        self.assertFalse(row['watches_armed'])
        self.assertEqual(row['watch_state'], 'Feedback checks paused')

    def test_fleet_coverage_preserves_watching_and_marks_only_saved_repos_configured(self):
        coverage = {'usage':{}, 'repositories':[
            {'repo':'owner/repo', 'status':'watching'}, {'repo':'owner/fleet-only', 'status':'watching'}]}
        with patch.object(self.control, 'fleet_overview', return_value=coverage):
            rows = {row['repo']:row for row in self.control.fence()['repos']}
        self.assertTrue(rows['owner/repo']['configured'])
        self.assertFalse(rows['owner/fleet-only']['configured'])
        for row in rows.values():
            self.assertTrue(row['watches_armed'])
            self.assertEqual(row['watch_state'], 'Fleet watching for new work')
            self.assertIn('Fleet', row['watch_next_step'])

    def test_editing_folder_keeps_a_stopped_watch_stopped_and_preserves_cursor(self):
        self.control.watches().put('owner/repo', 7, self.meta(enabled=False, last_review_comment_id=77))
        reply = self.control.edit_watch({'repo':'owner/repo', 'mode':'selected', 'number':7,
            'account':'alice', 'output_dir':str(self.output)})
        self.assertFalse(reply['watch']['enabled'])
        self.assertEqual(reply['watch']['last_review_comment_id'], 77)
        self.assertFalse(reply['watch']['watch_history'][-1]['previous']['enabled'])

    def test_editing_scope_without_enabled_keeps_stopped_choice(self):
        self.control.watches().put('owner/repo', 7, self.meta(enabled=False, last_review_comment_id=77))
        reply = self.control.edit_watch({'repo':'owner/repo', 'mode':'selected', 'number':7,
            'new_mode':'all', 'confirmed_scope_change':True, 'account':'alice', 'output_dir':str(self.output)})
        self.assertFalse(reply['watch']['enabled'])
        self.assertEqual(self.control.watches().read()['owner/repo']['7']['last_review_comment_id'], 77)

    def test_only_explicit_boolean_choice_changes_enabled(self):
        self.control.watches().put('owner/repo', 7, self.meta(enabled=False))
        data = {'repo':'owner/repo', 'mode':'selected', 'number':7,
            'account':'alice', 'output_dir':str(self.output)}
        before = self.control.watches().path.read_bytes()
        with self.assertRaises(ApiError):
            self.control.edit_watch({**data, 'enabled':'true'})
        self.assertEqual(self.control.watches().path.read_bytes(), before)
        self.assertTrue(self.control.edit_watch({**data, 'enabled':True})['watch']['enabled'])
        self.assertFalse(self.control.edit_watch({**data, 'enabled':False})['watch']['enabled'])
