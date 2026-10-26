"""Real write guards through the manual approval controller, local remotes only."""
import json
import unittest
from unittest.mock import patch
from ghagent import tasks
from ghagent.interfaces import JobKind, JobState
from ghagent.tasks import write_docs
from ghagent.web_control import ApiError
from tests.test_phase0 import inject
from tests import test_task_write_guard as guard_fixture
from tests.test_web_control import GuiFixture


class IntegratedDraftGuards(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.repo = guard_fixture.RealRepositoryTests('runTest')
        self.repo.setUp()
        self.addCleanup(self.repo.doCleanups)
        self.control.gh = self.repo.gh
        inject(write_docs.TASK)
        self.addCleanup(setattr, tasks.types, '_REGISTRY', None)
        self.repo.prepare_docs()
        self.persisted_job(kind=JobKind.WRITE_DOCS, state=JobState.WAITING_ON_YOU,
                           notes={'drafts': [{'name': 'pr', 'title': 'Setup', 'body': 'Steps'}],
                                  'worktree_path': str(self.repo.checkout),
                                  'member': 'alice', 'output_dir': str(self.output)})
        self.pr = patch.object(write_docs, '_open_pull_request', return_value={'result': 'created'})
        self.created = self.pr.start()
        self.addCleanup(self.pr.stop)

    def checks(self, code):
        text = self.config_path.read_text()
        import re
        text = re.sub(r'^checks = .*$', 'checks = ' + json.dumps([self.repo.command(code)]),
                      text, flags=re.MULTILINE)
        self.config_path.write_text(text)

    def post(self):
        return self.control.draft_action('job_test', {'name': 'pr', 'action': 'post'})

    def test_actual_failed_check_releases_claim_then_corrected_settings_allow_one_post(self):
        self.checks('raise SystemExit(7)')
        before = self.repo.branches()
        with self.assertRaisesRegex(ApiError, 'verification failed'):
            self.post()
        self.assertEqual(self.repo.branches(), before)
        stored = self.control.queue().get('job_test')
        self.assertNotIn('posting', stored.notes)
        self.assertIs(stored.state, JobState.WAITING_ON_YOU)
        self.checks('pass')
        result = self.post()
        self.assertEqual(result['job']['state'], 'done')
        self.created.assert_called_once()
        with self.assertRaises(ApiError):
            self.post()
        self.created.assert_called_once()

    def test_controller_stop_callback_reads_fresh_queue_during_checks(self):
        self.checks('pass')
        original = write_docs.verify_before_write
        def stop_and_verify(context, *args, **kwargs):
            self.assertFalse(context['stop_check']())
            self.control.queue().stop_job('job_test')
            self.assertTrue(context['stop_check']())
            return original(context, *args, **kwargs)
        before = self.repo.branches()
        with patch.object(write_docs, 'verify_before_write', stop_and_verify):
            with self.assertRaises(ApiError):
                self.post()
        self.assertEqual(self.repo.branches(), before)
        self.created.assert_not_called()
        self.assertIs(self.control.queue().get('job_test').state, JobState.STOPPED)
