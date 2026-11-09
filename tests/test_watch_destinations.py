"""Real saved folders are suggestions, never silent output/authority choices."""
import json
import unittest
from unittest.mock import patch

from ghagent.web_control import ApiError
from tests.test_web_control import GuiFixture


class WatchDestinationTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def meta(self, **extra):
        return {'member':'alice','member_home':str(self.home),'output_dir':str(self.output),**extra}

    def choices(self):
        return self.control.watch_destinations('owner/repo')['items']

    def test_saved_watch_folder_is_validated_offered_and_not_applied(self):
        self.control.watches().put('owner/repo', '*', self.meta(enabled=False))
        before=self.control.watches().path.read_bytes()
        self.assertEqual(self.choices(),[{'account':'alice','output_dir':str(self.output),'source':'Saved for this project’s watch'}])
        self.assertEqual(self.control.watches().path.read_bytes(),before)

    def test_removed_destination_offers_still_valid_earlier_setup(self):
        self.control.watches().put('owner/repo','*',self.meta(output_dir=str(self.home/'removed'),watch_history=[{'previous':self.meta()}]))
        self.assertEqual(self.choices()[0]['output_dir'],str(self.output))
        self.assertIn('Earlier',self.choices()[0]['source'])

    def test_prior_task_destination_is_offered_for_folderless_watch(self):
        self.persisted_job(notes=self.meta())
        self.control.watches().put('owner/repo','*',{'scope':'all'})
        self.assertIn('earlier task',self.choices()[0]['source'])
        self.assertNotIn('output_dir',self.control.watches().read()['owner/repo']['*'])

    def test_fleet_destination_and_other_watch_are_explicit_distinct_offers(self):
        other=self.home/'other-results';other.mkdir()
        self.control.watches().put('owner/other','*',self.meta(output_dir=str(other)))
        fleet={'fleets':[{'account':'alice','output_dir':str(self.output),'scope':'selected','repos':['owner/repo']}]}
        with patch.object(self.control.fleet_store().__class__, 'snapshot',return_value=fleet):
            self.assertEqual([c['output_dir'] for c in self.choices()],[str(self.output),str(other)])

    def test_invalid_missing_outside_home_and_whole_home_folders_are_not_offered(self):
        for number,meta in enumerate([self.meta(output_dir='/tmp'),self.meta(output_dir=str(self.home)),self.meta(output_dir=str(self.home/'missing')),self.meta(member='unknown')],1):
            self.control.watches().put('owner/repo',number,meta)
        self.assertEqual(self.choices(),[])

    def test_candidates_are_deduplicated_and_filtered_by_account(self):
        self.control.watches().put('owner/repo',7,self.meta())
        self.control.watches().put('owner/other','*',self.meta())
        self.assertEqual(len(self.choices()),1)
        self.assertEqual(len(self.control.watch_destinations('owner/repo','alice')['items']),1)
        with self.assertRaises(ApiError):self.control.watch_destinations('owner/repo','unknown')

    def test_unreadable_saved_state_is_an_error_not_an_empty_offer(self):
        self.control.watches().path.write_text('{bad-json')
        with self.assertRaises(Exception):self.choices()

    def save_new_repo(self):
        return self.control.save_repo({'owner':'owner','name':'new-project','base_branch':'main','checks':['python3 -m unittest'],'revision':self.control.revision()})

    def test_new_repository_exposes_inactive_watch_setup_and_never_picks_folder(self):
        self.control.watches().put('owner/other','*',self.meta())
        reply=self.save_new_repo();watch=self.control.watches().read()['owner/new-project']['*']
        self.assertTrue(reply['watch_setup_required']);self.assertFalse(watch['enabled']);self.assertNotIn('output_dir',watch)
        view=next(w for w in self.control.state()['watches'] if w['repo']=='owner/new-project')
        self.assertEqual(view['health'],'needs-folder');self.assertIn('Automatic follow-up is off',view['status_reason'])
        self.assertEqual(self.control.queue().all(),[])

    def test_finishing_new_watch_setup_keeps_old_history_and_waits_for_normal_poll(self):
        self.save_new_repo();before=self.control.watches().read()['owner/new-project']['*']
        self.control.add_watch({'repo':'owner/new-project','mode':'all','account':'alice','output_dir':str(self.output)})
        saved=self.control.watches().read()['owner/new-project']['*']
        self.assertTrue(saved['enabled']);self.assertNotIn('setup_required',saved)
        self.assertEqual(saved['armed_at'],before['armed_at']);self.assertEqual(saved['watch_history'][-1]['previous'],before)
        self.assertEqual(saved['watch_status'],'not-checked');self.assertEqual(self.control.queue().all(),[])

    def test_existing_watch_creation_does_not_reset_feedback_or_history(self):
        self.control.watches().put('owner/repo',7,self.meta(last_review_comment_id=91,watch_history=[{'earlier':'kept'}]))
        before=self.control.watches().path.read_bytes()
        with self.assertRaises(ApiError) as error:self.control.add_watch(self.task_data(mode='selected'))
        self.assertEqual(error.exception.status,409);self.assertEqual(self.control.watches().path.read_bytes(),before)

    def test_declining_pending_watch_setup_shows_stopped_and_keeps_record(self):
        self.save_new_repo()
        self.control.stop_watch({'repo':'owner/new-project','mode':'all'})
        saved=self.control.watches().read()['owner/new-project']['*']
        self.assertFalse(saved['enabled']);self.assertNotIn('setup_required',saved)
        view=next(w for w in self.control.state()['watches'] if w['repo']=='owner/new-project')
        self.assertEqual(view['health'],'disabled')

    def test_saving_a_removed_suggested_folder_fails_without_changing_watch(self):
        self.control.watches().put('owner/repo','*',{'scope':'all','last_checked_pr':7})
        self.persisted_job(notes=self.meta());self.assertTrue(self.choices());before=self.control.watches().path.read_bytes()
        self.output.rmdir()
        with self.assertRaises(ApiError):self.control.edit_watch({'repo':'owner/repo','mode':'all','account':'alice','output_dir':str(self.output)})
        self.assertEqual(self.control.watches().path.read_bytes(),before)

    def test_new_watch_baselines_existing_feedback_and_queues_only_new_feedback_in_chosen_folder(self):
        from ghagent.worker import Worker
        self.control.add_watch(self.task_data(mode='all'))
        class NoExecution:
            def execute(self, job):
                raise AssertionError('A polling check must not execute a model task')
        with patch('ghagent.model_client.ModelClient') as model:
            worker=Worker(self.gh,self.control.queue(),NoExecution(),self.control.config(),env={})
            self.assertEqual(worker._poll_watched(),0)
            self.assertEqual(self.control.watches().read()['owner/repo']['7']['last_review_comment_id'],30)
            self.gh.comments.append({'id':31,'body':'Please fix this new review comment.','user':{'id':42,'login':'member'}})
            self.assertEqual(worker._poll_watched(),1)
            model.assert_not_called()
        queued=self.control.queue().all()
        self.assertEqual(len(queued),1);self.assertEqual(queued[0].command_comment_id,31)
        self.assertEqual(queued[0].notes['member'],'alice')
        self.assertEqual(queued[0].notes['output_dir'],str(self.output))
        self.assertEqual(self.control.watches().read()['owner/repo']['7']['last_review_comment_id'],31)
