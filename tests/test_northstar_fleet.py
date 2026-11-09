"""Recovery controls keep history and make every offered next step executable."""
import json
from pathlib import Path
import tempfile
import unittest

from ghagent.fleet_store import FleetStore
from ghagent.recovery import fleet_recovery


class FleetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FleetStore(Path(self.temp.name))
        self.fleet = self.store.configure({'account': 'alice', 'output_dir': '/home/alice/results',
            'github_id': 42, 'github_login': 'alice', 'workers': 1, 'scope': 'all'})
        self.candidate = {'kind': 'review_pr', 'repo': 'owner/repo', 'repo_id': 10,
            'number': 1, 'head_sha': 'a' * 40, 'title': 'Review this change',
            'url': 'https://github.com/owner/repo/pull/1'}
        self.store.upsert(self.fleet['id'], [self.candidate])
        self.item = self.store.claim(1)

    def finish(self, **result):
        self.store.finish(self.item['id'], self.item['lease'], result)
        return self.store.internal()['items'][self.item['id']]

    def test_archive_preserves_result_receipts_and_is_not_rediscovered_or_requeued(self):
        operation = self.store.intent(self.item['id'], self.item['lease'], 'comment', {'body': 'An already published finding.'})
        self.store.receipt(self.item['id'], self.item['lease'], operation['id'], {'id': 123, 'url': 'https://github.com/owner/repo/pull/1#issuecomment-123'})
        original = self.finish(status='blocked', error='Test command failed', checks=[{'command': 'npm test', 'exit': 1}])
        self.store.control('archive', item_id=self.item['id'], reason='I handled this on GitHub.')
        saved = self.store.internal()['items'][self.item['id']]
        self.assertEqual(saved['result'], original['result'])
        self.assertEqual(saved['status'], 'blocked')
        self.assertEqual(saved['mutation_intents'], original['mutation_intents'])
        self.assertEqual(saved['archive_reason'], 'I handled this on GitHub.')
        self.assertEqual(self.store.overview()['unresolved_count'], 0)
        self.store.upsert(self.fleet['id'], [dict(self.candidate, title='Rediscovered saved review')],
                          repository_conditions={'owner/repo': 'new-condition'})
        self.store.verification_changed('owner/repo')
        self.assertIsNone(self.store.claim(1))
        self.assertEqual(self.store.snapshot()['archived'][0]['recovery']['kind'], 'archived')
        self.assertEqual(self.store.internal()['items'][self.item['id']]['head_sha'], 'a' * 40)

    def test_restore_does_not_start_work_until_explicit_retry(self):
        self.finish(status='blocked', error='Old failure')
        self.store.control('archive', item_id=self.item['id'])
        self.store.control('restore', item_id=self.item['id'])
        self.store.verification_changed('owner/repo')
        self.store.upsert(self.fleet['id'], [dict(self.candidate, title='Rediscovered saved review')])
        self.assertIsNone(self.store.claim(1))
        self.store.control('retry', item_id=self.item['id'])
        claimed = self.store.claim(1)
        self.assertEqual(claimed['id'], self.item['id'])
        self.assertEqual(claimed['attempts'], 2)
        self.assertTrue(any(h['event'] == 'restored' for h in claimed['history']))

    def test_stopped_fleet_keeps_unresolved_work_visible_without_claiming_it_is_watched(self):
        self.finish(status='blocked', error='Old failure')
        self.store.control('stop', self.fleet['id'])
        projection = self.store.overview()
        self.assertEqual(projection['fleets'][0]['status'], 'stopped')
        self.assertEqual(projection['unresolved_count'], 1)
        self.assertEqual(projection['recent_tasks'][0]['id'], self.item['id'])
        self.assertEqual(projection['recent_tasks'][0]['fleet_status'], 'stopped')
        self.assertEqual(projection['recent_tasks'][0]['recovery']['actions'][0], 'resume')
        self.assertEqual(projection['repositories'], [])
        self.assertEqual(projection['configured_agents']['glm'], 0)

    def test_retry_under_stopped_fleet_is_rejected_without_mutating_the_item(self):
        self.finish(status='blocked', error='Check failed')
        self.store.control('stop', self.fleet['id'])
        before = self.store.internal()['items'][self.item['id']]
        with self.assertRaisesRegex(ValueError, 'Resume the fleet first'):
            self.store.control('retry', item_id=self.item['id'])
        self.assertEqual(self.store.internal()['items'][self.item['id']], before)
        self.store.control('resume', self.fleet['id'])
        self.store.control('retry', item_id=self.item['id'])
        self.assertIsNotNone(self.store.claim(1))

    def test_done_with_failed_checks_accepts_retry_and_keeps_previous_evidence(self):
        result = {'status': 'done', 'checks': [{'command': 'npm test', 'passed': False, 'exit': 1}], 'summary': 'Failed verification'}
        self.finish(**result)
        self.assertTrue(self.store.snapshot()['items'][0]['recovery']['can_retry'])
        self.store.control('retry', item_id=self.item['id'])
        saved = self.store.internal()['items'][self.item['id']]
        old = next(h for h in saved['history'] if h['event'] == 'retry-requested')
        self.assertEqual(old['previous_result'], result)
        self.assertEqual(old['previous_status'], 'done')
        self.assertEqual(saved['attempts'], 1)

    def test_exhausted_budget_requires_explicit_additional_calls_and_keeps_usage(self):
        self.finish(status='blocked', error='Check failed')
        with self.store._locked(True) as data:
            data['fleets'][self.fleet['id']].update(max_calls_total=4, calls_used=4)
        self.assertIsNone(self.store.claim(1))
        with self.assertRaisesRegex(ValueError, 'Authorize additional calls'):
            self.store.control('resume', self.fleet['id'])
        for amount in (None, 0, -1, True, 10001):
            with self.assertRaises(ValueError):
                self.store.control('authorize_budget', self.fleet['id'], additional_calls=amount)
        self.store.control('authorize_budget', self.fleet['id'], additional_calls=3)
        f = self.store.internal()['fleets'][self.fleet['id']]
        self.assertEqual((f['calls_used'], f['max_calls_total'], f['status'], f['error']), (4, 7, 'running', ''))
        self.assertEqual(f['budget_history'][0]['additional_calls'], 3)
        self.store.control('retry', item_id=self.item['id'])
        self.assertIsNotNone(self.store.claim(1))

    def test_scope_and_manual_handoffs_do_not_offer_invalid_retry(self):
        item = self.finish(status='blocked', error='This repository is outside the current writable scope.')
        recovery = fleet_recovery(item, dict(self.fleet, scope='selected', repos=[]))
        self.assertEqual(recovery['kind'], 'scope')
        self.assertFalse(recovery['can_retry'])
        self.assertNotIn('retry', recovery['actions'])
        for error, kind in [('The pull request is no longer open.', 'obsolete'),
                            ('This change needs a manual review because its complete diff cannot be inspected.', 'manual_review')]:
            recovery = fleet_recovery(dict(item, error=error), self.fleet)
            self.assertEqual(recovery['kind'], kind)
            self.assertIn('open_github', recovery['actions'])
            self.assertIn('archive', recovery['actions'])
            self.assertFalse(recovery['can_retry'])

    def test_cooldown_explains_time_and_affected_provider(self):
        item = self.finish(status='retrying', summary='Provider rate limit', retry_kind='rate_limit', retry_scope='github', retry_after=30)
        recovery = self.store.snapshot()['items'][0]['recovery']
        self.assertEqual(recovery['kind'], 'cooldown')
        self.assertTrue(recovery['retry_at'])
        self.assertIn('github', recovery['waiting_for'])
        self.assertFalse(recovery['can_retry'])

    def test_no_user_history_is_pruned_and_old_events_are_retained(self):
        self.finish(status='done', summary='Saved result')
        with self.store._locked(True) as data:
            for n in range(510):
                self.store._event(data, f'Event {n}')
            self.store._prune(data, self.fleet['id'], keep=0)
        data = self.store.internal()
        self.assertIn(self.item['id'], data['items'])
        self.assertTrue(any(e['message'] == 'Event 0' for e in data['events']))
        self.assertTrue(any(e['message'] == 'Event 509' for e in data['events']))

    def test_replacement_configuration_retains_previous_run_settings_and_usage(self):
        self.finish(status='done', summary='First run finished', model_calls=4)
        self.store.control('stop', self.fleet['id'])
        replacement = self.store.configure({'account': 'alice', 'output_dir': '/home/alice/new-results',
            'github_id': 42, 'github_login': 'alice', 'workers': 2, 'scope': 'all'})
        previous = replacement['configuration_history'][-1]['previous_configuration']
        self.assertEqual(previous['calls_used'], 4)
        self.assertEqual(previous['output_dir'], '/home/alice/results')
        self.assertEqual(previous['workers'], 1)
        self.assertEqual(replacement['id'], self.fleet['id'])
        self.assertIn(self.item['id'], self.store.internal()['items'])

    def test_events_name_the_item_repository_and_fleet_for_filtering(self):
        self.finish(status='blocked', error='Check failed')
        event = next(e for e in self.store.snapshot()['events'] if e['item_id'] == self.item['id'])
        self.assertEqual(event['repo'], 'owner/repo')
        self.assertEqual(event['fleet_id'], self.fleet['id'])
        self.store.control('pause', self.fleet['id'])
        self.assertEqual(self.store.snapshot()['events'][-1]['fleet_id'], self.fleet['id'])

    def test_publication_details_include_remote_evidence_without_credentials(self):
        operation = self.store.intent(self.item['id'], self.item['lease'], 'comment',
            {'body': 'Exact finding text', 'token': 'do-not-display', 'api_key': 'also-private', 'target_ref': 'feature'})
        self.store.receipt(self.item['id'], self.item['lease'], operation['id'],
            {'id': 123, 'url': 'https://github.com/owner/repo/pull/1#issuecomment-123', 'message': 'Bearer hidden-value'})
        self.finish(status='blocked', error='Check failed')
        public = self.store.snapshot()['items'][0]
        saved = public['operations'][0]
        self.assertEqual(saved['payload']['body'], 'Exact finding text')
        self.assertEqual(saved['payload']['target_ref'], 'feature')
        self.assertEqual(saved['result']['id'], 123)
        self.assertIn('confirmed_at', saved)
        self.assertNotIn('do-not-display', json.dumps(public))
        self.assertNotIn('also-private', json.dumps(public))
        self.assertNotIn('hidden-value', json.dumps(public))
        self.assertNotIn(self.item['lease'], json.dumps(public))
        internal = self.store.internal()['items'][self.item['id']]
        self.assertEqual(internal['mutation_intents'][0]['payload']['token'], 'do-not-display')

    def test_archiving_running_work_is_refused(self):
        before = self.store.internal()
        with self.assertRaisesRegex(ValueError, 'finish or stop'):
            self.store.control('archive', item_id=self.item['id'])
        self.assertEqual(self.store.internal(), before)

    def test_wrong_fleet_cannot_change_item(self):
        self.finish(status='blocked', error='Old failure')
        with self.assertRaisesRegex(ValueError, 'different saved fleet'):
            self.store.control('archive', 'other-fleet', self.item['id'])

    def test_finished_review_explains_unmet_merge_conditions(self):
        self.finish(status='done', readiness={'ready': False, 'reason': 'One approval is required.'})
        recovery = self.store.snapshot()['items'][0]['recovery']
        self.assertEqual(recovery['kind'], 'merge_waiting')
        self.assertIn('One approval', recovery['explanation'])
        self.assertIn('open_github', recovery['actions'])


from tests.test_web_control import GuiFixture
from ghagent.web_control import ApiError


class FleetRecoveryAPIContractTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.store = self.control.fleet_store()
        self.fleet = self.store.configure({'account': 'alice', 'output_dir': str(self.output),
            'github_id': 42, 'github_login': 'test-member', 'workers': 1, 'scope': 'all'})
        self.store.upsert(self.fleet['id'], [{'kind': 'review_pr', 'repo': 'owner/repo',
            'repo_id': 42, 'number': 7, 'head_sha': 'a' * 40, 'title': 'Review this change'}])
        self.item = self.store.claim(1)
        self.store.finish(self.item['id'], self.item['lease'], {'status': 'blocked', 'error': 'The check failed.'})

    def test_archive_and_restore_are_wired_to_saved_item_and_public_next_step(self):
        reply = self.control.fleet_action({'action': 'archive', 'item_id': self.item['id'],
            'reason': 'I have handled this.'})
        archived = next(row for row in reply['items'] if row['id'] == self.item['id'])
        self.assertEqual(archived['recovery']['kind'], 'archived')
        self.assertEqual(reply['unresolved_count'], 0)
        restored = self.control.fleet_action({'action': 'restore', 'item_id': self.item['id']})
        item = next(row for row in restored['items'] if row['id'] == self.item['id'])
        self.assertNotIn('archived_at', item)
        self.assertTrue(item['restore_hold'])
        self.assertEqual(self.store.internal()['items'][self.item['id']]['status'], 'blocked')

    def test_public_api_explains_stopped_retry_without_queuing_it(self):
        self.store.control('stop', self.fleet['id'])
        with self.assertRaisesRegex(ApiError, 'Resume the fleet first'):
            self.control.fleet_action({'action': 'retry', 'item_id': self.item['id']})
        self.assertEqual(self.store.internal()['items'][self.item['id']]['status'], 'blocked')
        summary = self.control.state()['fleet_summary']
        self.assertEqual(summary['unresolved_count'], 1)
        self.assertEqual(summary['recent_tasks'][0]['id'], self.item['id'])

    def test_public_api_budget_authorization_binds_identity_and_keeps_usage(self):
        with self.store._locked(True) as data:
            data['fleets'][self.fleet['id']].update(max_calls_total=4, calls_used=4, status='paused')
        with self.assertRaisesRegex(ApiError, 'Authorize additional calls'):
            self.control.fleet_action({'action': 'resume', 'fleet_id': self.fleet['id']})
        response = self.control.fleet_action({'action': 'authorize_budget', 'fleet_id': self.fleet['id'], 'additional_calls': 3})
        fleet = next(row for row in response['fleets'] if row['id'] == self.fleet['id'])
        self.assertEqual(fleet['calls_used'], 4)
        self.assertEqual(fleet['max_calls_total'], 7)
        self.assertEqual(fleet['status'], 'running')


if __name__ == '__main__':
    unittest.main()
