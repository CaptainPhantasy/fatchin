"""Repository policy, publication caps, run budget and drain boundaries."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghagent.fleet_discovery import _apply_policy, _policy_allows
from ghagent.fleet_store import FleetStore
from tests.test_fleet_store import candidate


class PolicyTests(unittest.TestCase):
    def test_personal_default_on_and_org_default_off(self):
        policy = {'personal_default': 'include', 'org_default': 'exclude',
                  'include': ['acme/important'], 'exclude': ['alice/junk']}
        self.assertTrue(_policy_allows('alice/thing', policy, 'alice'))
        self.assertFalse(_policy_allows('acme/thing', policy, 'alice'))
        self.assertTrue(_policy_allows('acme/important', policy, 'alice'))
        self.assertFalse(_policy_allows('alice/junk', policy, 'alice'))

    def test_unset_policy_keeps_previous_scope(self):
        self.assertTrue(_policy_allows('acme/thing', {}, 'alice'))
        kept, skipped = _apply_policy(
            [{'full_name': 'alice/a'}, {'full_name': 'acme/b'}],
            {'org_default': 'exclude'}, 'alice')
        self.assertEqual([r['full_name'] for r in kept], ['alice/a'])
        self.assertEqual(skipped, ['acme/b'])


class StorePipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FleetStore(Path(temporary.name) / 'fleet-v1')
        self.fleet = self.store.configure({
            'account': 'alice', 'output_dir': '/home/alice/reports',
            'github_id': 123, 'github_login': 'alice', 'workers': 10,
            'deepseek_workers': 0, 'enabled_providers': ['glm'],
            'max_calls_total': 2})

    def test_publication_cap_is_shared_across_slots(self):
        fleet_id = self.fleet['id']
        self.assertTrue(self.store.reserve_publication(fleet_id, 'owner/repo', 2))
        self.assertTrue(self.store.reserve_publication(fleet_id, 'owner/repo', 2))
        self.assertFalse(self.store.reserve_publication(fleet_id, 'owner/repo', 2))
        self.assertTrue(self.store.reserve_publication(fleet_id, 'owner/other', 2))

    def test_run_budget_pauses_the_fleet(self):
        self.store.upsert(self.fleet['id'], [candidate(1, 'review_pr')])
        item = self.store.claim(1)
        self.assertIsNotNone(item)
        self.store.finish(item['id'], item['lease'],
                          {'status': 'done', 'model_calls': 2})
        self.assertIsNone(self.store.claim(1))
        fleet = self.store.snapshot()['fleets'][0]
        self.assertEqual(fleet['status'], 'paused')
        self.assertIn('budget', fleet['error'].lower())

    def test_drain_is_recorded_after_two_quiet_minutes(self):
        clock = {'value': '2026-10-25T00:00:00+00:00'}
        with mock.patch('ghagent.fleet_store.now', lambda: clock['value']):
            self.assertIsNone(self.store.evaluate_drain(self.fleet['id']))
            clock['value'] = '2026-10-25T00:02:01+00:00'
            self.assertIsNotNone(self.store.evaluate_drain(self.fleet['id']))
            self.assertEqual(self.store.evaluate_drain(self.fleet['id']),
                             '2026-10-25T00:02:01+00:00')

    def test_new_work_clears_the_drained_marker(self):
        clock = {'value': '2026-10-25T00:00:00+00:00'}
        with mock.patch('ghagent.fleet_store.now', lambda: clock['value']):
            self.store.evaluate_drain(self.fleet['id'])
            clock['value'] = '2026-10-25T00:02:01+00:00'
            self.assertIsNotNone(self.store.evaluate_drain(self.fleet['id']))
        self.store.upsert(self.fleet['id'], [candidate(1, 'review_pr')])
        self.assertIsNone(self.store.evaluate_drain(self.fleet['id']))
        self.assertFalse(self.store.snapshot()['fleets'][0].get('drained_at'))

    def test_legacy_per_file_review_is_retired_on_refresh(self):
        legacy = candidate(1, 'review_code', path='a.py', blob_sha='b' * 40, source_ref='main')
        legacy.pop('paths')
        legacy.pop('batch')
        self.store.upsert(self.fleet['id'], [legacy])
        self.store.upsert(self.fleet['id'], [], scanned_repos=[])
        item = next(i for i in self.store.snapshot()['items'] if i['kind'] == 'review_code')
        self.assertEqual(item['status'], 'dismissed')
        self.assertIn('commit-level', item['error'])

    def test_calls_are_counted_while_an_item_runs(self):
        self.store.upsert(self.fleet['id'], [candidate(1, 'review_pr')])
        item = self.store.claim(1)
        self.store.note_calls(item['id'], item['lease'], 2)
        self.assertEqual(self.store.snapshot()['fleets'][0]['calls_used'], 2)
        self.store.note_calls(item['id'], item['lease'], 2)
        self.assertEqual(self.store.snapshot()['fleets'][0]['calls_used'], 2)
        self.store.finish(item['id'], item['lease'],
                          {'status': 'done', 'model_calls': 2})
        self.assertEqual(self.store.snapshot()['fleets'][0]['calls_used'], 2)
        self.assertIsNone(self.store.claim(1))

    def test_prune_keeps_a_bounded_number_of_terminal_items(self):
        data = {'items': {}}
        for index in range(5):
            data['items'][f'i{index}'] = {
                'id': f'i{index}', 'fleet_id': self.fleet['id'], 'status': 'done',
                'updated_at': f'2026-10-2{index}T00:00:00+00:00'}
        data['items']['live'] = {'id': 'live', 'fleet_id': self.fleet['id'], 'status': 'queued'}
        FleetStore._prune(data, self.fleet['id'], keep=2)
        self.assertEqual(sorted(data['items']), ['i3', 'i4', 'live'])


if __name__ == '__main__':
    unittest.main()
