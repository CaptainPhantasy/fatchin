"""Result recovery clears current warnings while keeping earlier evidence."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent.interfaces import JobState
from ghagent.web_control import ApiError
from tests.test_web_control import GuiFixture


class ResultRecoveryNextStepTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def save(self, job):
        return self.control.job_recovery(job.job_id, {
            'action':'export', 'account':'alice', 'output_dir':str(self.output)})['job']

    def test_clean_second_copy_moves_old_skip_warning_into_history(self):
        original = self.output / 'gha-worker-job_test'
        original.mkdir()
        (original / 'safe.txt').write_text('Keep this result')
        private = self.root / 'private.txt'
        private.write_text('Do not copy')
        (original / 'linked.txt').symlink_to(private)
        job = self.persisted_job(state=JobState.DONE, notes={
            'member':'alice', 'output_dir':str(self.output), 'exported_files':['safe.txt']})
        first = self.save(job)
        first_root = Path(first['notes']['artifact_dir'])
        first_receipt = (first_root / 'report.json').read_bytes()
        self.assertEqual(first['notes']['recovery_copy_skipped'], ['linked.txt'])

        second = self.save(job)
        notes = second['notes']
        self.assertNotIn('recovery_copy_skipped', notes)
        self.assertNotIn('result_export_error', notes)
        self.assertEqual(notes['export_history'][-1]['previous_recovery_copy_skipped'], ['linked.txt'])
        self.assertIn('Some saved files were skipped', notes['export_history'][-1]['previous_result_export_error'])
        second_root = Path(notes['artifact_dir'])
        self.assertEqual((second_root / 'safe.txt').read_text(), 'Keep this result')
        self.assertEqual((first_root / 'report.json').read_bytes(), first_receipt)
        self.assertTrue((original / 'linked.txt').is_symlink())
        self.assertEqual(self.gh.calls, [])

    def test_failed_new_copy_keeps_the_original_current_warning(self):
        original = self.output / 'gha-worker-job_test'
        original.mkdir()
        (original / 'safe.txt').write_text('Keep this result')
        job = self.persisted_job(state=JobState.DONE, notes={
            'member':'alice', 'output_dir':str(self.output),
            'recovery_copy_skipped':['old-link.txt'], 'result_export_error':'Previous copy skipped a link'})
        with patch('ghagent.results.write_report', side_effect=OSError('simulated report failure')):
            with self.assertRaisesRegex(ApiError, 'could not be saved'):
                self.save(job)
        saved = self.control.queue().get(job.job_id)
        self.assertEqual(saved.notes['recovery_copy_skipped'], ['old-link.txt'])
        self.assertEqual(saved.notes['result_export_error'], 'Previous copy skipped a link')
        self.assertNotIn('export_history', saved.notes)
        self.assertEqual((original / 'safe.txt').read_text(), 'Keep this result')

    def test_finished_missing_files_offers_recovery_and_it_creates_a_browsable_report(self):
        for state in (JobState.DONE, JobState.FAILED, JobState.STOPPED, JobState.WAITING_ON_YOU):
            with self.subTest(state=state):
                suffix = state.value.replace('-', '_')
                job = self.persisted_job(job_id='job_' + suffix, dedup_key=suffix, state=state)
                with self.assertRaises(ApiError) as caught:
                    self.control.job_files(job.job_id)
                self.assertEqual(caught.exception.actions, ['save-results', 'download-report'])
                self.assertNotIn('Refresh after', str(caught.exception))
                self.save(job)
                names = [row['name'] for row in self.control.job_files(job.job_id)['items']]
                self.assertIn('report.json', names)
                name, content = self.control.job_download(job.job_id, 'report.json')
                self.assertEqual(json.loads(content)['job_id'], job.job_id)

    def test_older_task_without_destination_can_follow_save_results_step(self):
        job = self.persisted_job(state=JobState.FAILED, notes={})
        with self.assertRaises(ApiError) as caught:
            self.control.job_files(job.job_id)
        self.assertIn('save-results', caught.exception.actions)
        self.assertIn('Download report', caught.exception.hint)
        self.save(job)
        self.assertIn('report.md', [row['name'] for row in self.control.job_files(job.job_id)['items']])

    def test_removed_destination_offers_the_same_working_recovery(self):
        job = self.persisted_job(state=JobState.STOPPED, notes={
            'member':'alice', 'output_dir':str(self.home / 'removed')})
        with self.assertRaises(ApiError) as caught:
            self.control.job_files(job.job_id)
        self.assertIn('save-results', caught.exception.actions)
        self.save(job)
        self.assertTrue(self.control.job_files(job.job_id)['items'])

    def test_active_task_without_destination_does_not_offer_unavailable_export(self):
        job = self.persisted_job(state=JobState.WORKING, notes={})
        with self.assertRaises(ApiError) as caught:
            self.control.job_files(job.job_id)
        self.assertEqual(caught.exception.actions, ['download-report'])
        self.assertIn('stop the task first', caught.exception.hint)
