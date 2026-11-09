"""Real control-plane recovery contracts, isolated from paid and live systems."""
import json
import unittest
from unittest.mock import patch
from ghagent.interfaces import JobKind, JobState
from ghagent.web_control import ApiError
from tests.test_web_control import GuiFixture


class RecoveryIntegrationTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_saved_draft_survives_worker_restart_without_publishing(self):
        job = self.persisted_job(kind=JobKind.TRIAGE_ISSUES, number=0, state=JobState.WAITING_ON_YOU,
            notes={'member':'alice','output_dir':str(self.output),'drafts':[{'name':'proposal','body':'Keep this draft.'}]})
        before = self.control.queue().get(job.job_id).to_json()
        self.control.service_action({'action':'restart'})
        self.assertEqual(self.control.queue().get(job.job_id).to_json(), before)
        self.assertFalse(any(call[0] != 'GET' for call in self.gh.calls))

    def test_uncertain_publication_cannot_be_retried_as_an_ordinary_task(self):
        for marker in ('posting', 'publish_outcome'):
            with self.subTest(marker=marker):
                self.control.queue().jobs_path.write_text('{"jobs":{}}')
                job = self.persisted_job(state=JobState.STOPPED, notes={marker:'uncertain','member':'alice','output_dir':str(self.output)})
                with self.assertRaisesRegex(ApiError,'publication is not confirmed'):
                    self.control.job_action(job.job_id,'retry')
                self.assertEqual(len(self.control.queue().all()),1)
                self.assertEqual(self.control.queue().get(job.job_id).notes[marker],'uncertain')

    def test_retry_restores_required_fields_from_the_saved_request(self):
        original={'repo':'owner/repo','kind':'triage-issues','account':'alice','output_dir':str(self.output),'values':{'words':'Organize the first two tasks.'}}
        job=self.persisted_job(kind=JobKind.TRIAGE_ISSUES,number=0,state=JobState.FAILED,notes={'request':original})
        reply=self.control.job_action(job.job_id,'retry')
        self.assertEqual(reply['job']['notes']['values']['words'],original['values']['words'])
        self.assertEqual(len(self.control.queue().all()),2)
        self.assertEqual(self.control.queue().get(job.job_id).state,JobState.FAILED)

    def test_stopping_watch_keeps_all_feedback_positions(self):
        store=self.control.watches()
        store.put('owner/repo','*',{'scope':'all','last_review_comment_id':90})
        store.put('owner/repo',7,{'scope':'all','last_review_comment_id':88})
        self.control.stop_watch({'repo':'owner/repo','mode':'all'})
        rows=store.read()['owner/repo']
        self.assertEqual(set(rows),{'*','7'})
        self.assertEqual(rows['7']['last_review_comment_id'],88)
        self.assertFalse(rows['*']['enabled'])
        self.assertFalse(rows['7']['enabled'])

    def test_deepseek_credential_is_named_and_described_as_deepseek(self):
        reply=self.control.credential({'kind':'deepseek','value':'test-dedicated-deepseek','supplied_by':'Test member'})
        from ghagent.config_loader import load_env_file
        self.assertEqual(load_env_file(self.env_path)['DEEPSEEK_API_KEY'],'test-dedicated-deepseek')
        self.assertIn('DeepSeek',reply['record']['what'])
        self.assertNotIn('test-dedicated-deepseek',json.dumps(reply))

    def test_pool_connection_test_is_one_attempt_and_counts_the_selected_pool(self):
        with self.env_path.open('a') as out:out.write('DEEPSEEK_API_KEY=test-deepseek\n')
        with patch('ghagent.model_client.ModelClient') as Client:
            Client.return_value.chat.return_value={'content':'OK'}
            result=self.control.provider_check({'provider':'deepseek','confirmed':True})
            self.assertTrue(result['ok'])
            self.assertEqual(Client.call_args.kwargs['max_attempts'],1)
            self.assertIn('deepseek-model-call-counts.json',Client.call_args.kwargs['day_counter_path'])
            Client.return_value.chat.assert_called_once()
            self.assertEqual(Client.call_args.args[0].max_calls_per_job,1)
        self.assertFalse(any(call[0] != 'GET' for call in self.gh.calls))

    def test_connection_test_does_nothing_without_the_persons_decision(self):
        with patch('ghagent.model_client.ModelClient') as Client:
            with self.assertRaisesRegex(ApiError,'Send one test request'):
                self.control.provider_check({'provider':'deepseek'})
            Client.assert_not_called()

    def test_actual_project_access_check_explains_scope_is_a_separate_decision(self):
        with patch.object(self.gh,'rest',return_value={'permissions':{'push':True}}) as read:
            result=self.control.repository_access('owner/repo')
            read.assert_called_once_with('GET','repos/owner/repo')
        self.assertTrue(result['can_write'])
        self.assertIn('does not add',result['next_step'])
        self.assertEqual(self.control.fleet_store().snapshot()['fleets'],[])

    def test_report_never_invents_an_automatic_watch(self):
        from ghagent.results import _report_markdown
        job=self.persisted_job(kind=JobKind.EXPLAIN_REPO,number=0,state=JobState.DONE,notes={'agent_summary':'Read-only result'})
        report=_report_markdown(job)
        self.assertIn('No automatic follow-up was recorded',report)
        self.assertNotIn('review watch stays armed',report)
