import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from ghagent.fleet_store import FleetStore, FleetStateError


def fleet(store, account='alice', workers=10, deepseek_workers=0, github_login='alice'):
    return store.configure({'account': account, 'output_dir': '/home/' + account + '/reports',
                            'github_id': 123, 'github_login': github_login, 'workers': workers,
                            'deepseek_workers': deepseek_workers, 'enabled_providers': ['glm', 'deepseek']})


def candidate(n, kind='review_pr', **fields):
    item = {'kind': kind, 'repo': 'owner/repo', 'repo_id': 42, 'number': n,
            'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'created_at': f'2026-10-{n:02d}T00:00:00+00:00',
            'title': f'Change {n}', 'url': f'https://github.com/owner/repo/pull/{n}', **fields}
    if kind == 'review_code':
        item.setdefault('paths', [item.get('path') or f'file-{n}.py'])
        item.setdefault('batch', n)
    return item


class FleetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FleetStore(Path(self.temp.name) / 'fleet-v1')
        self.fleet = fleet(self.store)

    def test_full_fleet_spans_at_least_five_repositories_with_four_per_project(self):
        from collections import Counter
        self.store.control('stop', self.fleet['id'])
        self.fleet = fleet(self.store, workers=10, deepseek_workers=10)
        work = [candidate(n + 1, 'review_code', repo=f'owner/project-{project}',
                          repo_id=100 + project, path=f'file-{n}.py',
                          created_at=f'2026-10-{25-project:02d}T00:00:00+00:00')
                for project in range(5) for n in range(12)]
        self.store.upsert(self.fleet['id'], work)
        claims = [self.store.claim(slot) for slot in range(1, 21)]
        self.assertTrue(all(claims))
        self.assertEqual(Counter(i['repo'] for i in claims), {f'owner/project-{n}': 4 for n in range(5)})
        first = claims[0]
        self.store.finish(first['id'], first['lease'], {'status': 'done'})
        self.store.upsert(self.fleet['id'], [candidate(26, 'fix_comment', repo='owner/urgent',
            repo_id=999, comment_id='inline:22', body='Fix this exact failure')])
        self.assertEqual(self.store.claim(first['slot'])['repo'], 'owner/urgent')

    def test_source_fork_cap_applies_across_pools_and_parent_projects(self):
        self.store.upsert(self.fleet['id'], [candidate(n, repo=f'owner/parent-{n}', repo_id=n,
            source_repo='contributor/shared-fork', source_repo_id=500) for n in range(1, 8)])
        self.assertTrue(all(self.store.claim(slot) for slot in range(1, 5)))
        self.assertIsNone(self.store.claim(5))

    def test_fix_publication_queues_independent_review_without_waiting_for_account_scan(self):
        self.store.upsert(self.fleet['id'], [candidate(1, 'fix_comment', comment_id='1', body='Fix the return')])
        item = self.store.claim(1)
        correction = {'repo': 'owner/repo', 'repo_id': 42, 'number': 10,
            'head_sha': 'c' * 40, 'branch': 'fatchin/fix-example', 'target_ref': 'feature',
            'url': 'https://github.com/owner/repo/pull/10'}
        self.store.finish(item['id'], item['lease'], {'status': 'done', 'fix_pr': correction})
        self.assertIsNone(self.store.claim(1), 'The author slot must not take its own correction review.')
        review = self.store.claim(2)
        self.assertEqual((review['kind'], review['number'], review['head_sha']), ('review_pr', 10, 'c' * 40))
        self.assertEqual(review['author_slot'], 1)
        self.store.finish(review['id'], review['lease'], {'status': 'done', 'readiness': {
            'ready': True, 'head_sha': 'c' * 40, 'checks_source': 'GitHub'}})
        self.assertEqual(self.store.snapshot()['ready'][0]['id'], review['id'])

    def test_comment_author_hands_fix_to_another_fresh_slot(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        review = self.store.claim(1)
        self.store.finish(review['id'], review['lease'], {'status': 'done', 'findings': [
            {'body': 'Fix the return value', 'path': 'calc.py', 'line': 2}]})
        self.assertIsNone(self.store.claim(1))
        fix = self.store.claim(2)
        self.assertEqual((fix['kind'], fix['author_slot']), ('fix_comment', 1))

    def test_claim_cross_process_store_instances_never_exceeds_pool(self):
        self.store.upsert(self.fleet['id'], [candidate(i, repo=f'owner/repo-{i % 5}', repo_id=42 + i % 5) for i in range(1, 31)])
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            claims = list(pool.map(lambda slot: FleetStore(self.store.root).claim(slot), list(range(1, 21)) * 2))
        actual = [c for c in claims if c]
        self.assertEqual(len(actual), 10)
        self.assertEqual(len({c['id'] for c in actual}), 10)
        self.assertTrue(all(c['provider'] == 'glm' for c in actual))

    def test_twenty_global_workers_two_provider_pools_across_fleets(self):
        self.store.control('stop', self.fleet['id'])
        first = fleet(self.store, workers=10, deepseek_workers=10)
        second = fleet(self.store, account='bob', workers=10, deepseek_workers=10)
        self.store.upsert(first['id'], [candidate(i, repo=f'owner/repo-{i % 5}', repo_id=42 + i % 5) for i in range(1, 16)])
        self.store.upsert(second['id'], [candidate(i, repo=f'owner/repo-{i % 5}', repo_id=42 + i % 5) for i in range(16, 31)])
        claims = [self.store.claim(i) for i in range(1, 21)]
        self.assertTrue(all(claims))
        self.assertEqual(sum(c['provider'] == 'glm' for c in claims), 10)
        self.assertEqual(sum(c['provider'] == 'deepseek' for c in claims), 10)
        self.assertIsNone(self.store.claim(1))

    def test_comments_then_newest_created_and_new_arrival_after_completion(self):
        self.store.upsert(self.fleet['id'], [candidate(2), candidate(3), candidate(1, 'fix_comment', body='Fix crash', comment_id='1')])
        first = self.store.claim(1)
        self.assertEqual(first['kind'], 'fix_comment')
        self.store.finish(first['id'], first['lease'], {'status': 'done'})
        self.store.upsert(self.fleet['id'], [candidate(9)])
        self.assertEqual(self.store.claim(2)['number'], 9)

    def test_independent_slot_with_one_concurrent_worker(self):
        self.store.control('stop', self.fleet['id'])
        f = fleet(self.store, workers=1)
        self.store.upsert(f['id'], [candidate(1, author_slot=1)])
        self.assertIsNone(self.store.claim(1))
        claim = self.store.claim(2)
        self.assertEqual(claim['slot'], 2)
        self.store.upsert(f['id'], [candidate(2)])
        self.assertIsNone(self.store.claim(3))

    def test_target_exclusion_fixes_but_parallel_distinct_readonly_files(self):
        self.store.upsert(self.fleet['id'], [candidate(i, 'fix_comment', comment_id=str(i), body='Fix', target_ref='main') for i in (1, 2)])
        self.assertIsNotNone(self.store.claim(1))
        self.assertIsNone(self.store.claim(2))
        self.store.upsert(self.fleet['id'], [candidate(i, 'review_code', path=f'f{i}.py', source_ref='main') for i in (3, 4)])
        self.assertIsNotNone(self.store.claim(3))
        self.assertIsNotNone(self.store.claim(4))

    def test_stop_fences_new_writes_preserves_admitted_receipts_and_no_replay(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        item = self.store.claim(1)
        op = self.store.intent(item['id'], item['lease'], 'comment', {'body_digest': 'd', 'target': 'issue/1'})
        self.store.control('stop', self.fleet['id'])
        self.assertFalse(self.store.alive(item['id'], item['lease']))
        with self.assertRaises(FleetStateError):
            self.store.intent(item['id'], item['lease'], 'comment', {'body_digest': 'other'})
        self.store.receipt(item['id'], item['lease'], op['id'], {'id': 17})
        self.store.recover(item['id'], item['lease'], 'Stopped process confirmed gone.')
        state = self.store.internal()['items'][item['id']]
        self.assertEqual(state['mutation_intents'][0]['result'], {'id': 17})
        self.assertEqual(state['status'], 'blocked')
        self.assertNotIn('lease', self.store.snapshot()['items'][0])

    def test_pause_drains_and_corruption_is_preserved(self):
        self.store.upsert(self.fleet['id'], [candidate(1), candidate(2)])
        item = self.store.claim(1)
        self.store.control('pause', self.fleet['id'])
        self.assertTrue(self.store.alive(item['id'], item['lease']))
        self.assertIsNone(self.store.claim(2))
        self.store.path.write_text('{broken')
        with self.assertRaises(FleetStateError):
            self.store.snapshot()
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_bounded_retry_keeps_intent_history_and_rejects_old_lease(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        first = None
        for attempt in range(3):
            with self.store._locked(True) as data:
                for entry in data['items'].values():
                    entry['eligible_at'] = ''
            item = self.store.claim(attempt + 1)
            if first is None:
                first = item
                op = self.store.intent(item['id'], item['lease'], 'push', {'prepared_commit_sha': 'c' * 40})
            self.store.recover(item['id'], item['lease'], 'Child crashed')
        saved = self.store.internal()['items'][first['id']]
        self.assertEqual(saved['status'], 'blocked')
        self.assertEqual(saved['attempts'], 3)
        self.assertEqual(len(saved['mutation_intents']), 1)
        self.assertFalse(self.store.heartbeat(first['id'], first['lease'], 'late'))
        self.store.control('retry', item_id=first['id'])
        retry = self.store.claim(4)
        self.assertEqual(retry['attempts'], 4)
        self.assertEqual(retry['mutation_intents'][0]['id'], op['id'])

    def test_findings_cross_fleet_dedup_correction_provenance_and_ready_fence(self):
        other = fleet(self.store, account='bob')
        self.store.upsert(self.fleet['id'], [candidate(1)])
        self.store.upsert(other['id'], [candidate(1)])
        self.assertEqual(len(self.store.internal()['items']), 1)
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'done', 'findings': [{'body': 'Missing bounds check', 'path': 'x.py'}]})
        ready = {'owner/repo#1': {'ready': True, 'head_sha': 'a' * 40}}
        self.store.upsert(self.fleet['id'], [], readiness=ready)
        self.assertEqual(self.store.snapshot()['ready'], [])
        finding = self.store.claim(2)
        self.store.finish(finding['id'], finding['lease'], {'status': 'done', 'fix_pr': {'repo': 'owner/repo', 'number': 2}})
        self.store.upsert(self.fleet['id'], [candidate(2, correction_pr={'repo': 'owner/repo', 'number': 2}, author_slot=2)])
        self.assertIsNone(self.store.claim(2))
        self.assertIsNotNone(self.store.claim(3))
        self.assertEqual(len(self.store.snapshot()['comments']), 1)  # Open correction is not resolution.
        self.store.upsert(self.fleet['id'], [], resolved_items=[finding['id']], readiness=ready)
        self.assertEqual(len(self.store.snapshot()['ready']), 1)
        self.store.upsert(self.fleet['id'], [], error='Read failed')
        self.assertEqual(self.store.snapshot()['ready'], [])

    def test_new_branch_push_supersedes_queued_versions_and_changed_policy_retries(self):
        self.store.upsert(self.fleet['id'], [candidate(1, 'review_code', path='x.py', source_repo='owner/repo', source_ref='dev')],
                          repository_conditions={'owner/repo': 'old'})
        self.store.upsert(self.fleet['id'], [], branch_heads={'owner/repo:dev': 'c' * 40})
        self.assertEqual(self.store.snapshot()['items'][0]['status'], 'dismissed')
        self.store.upsert(self.fleet['id'], [candidate(2)])
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'blocked', 'summary': 'Configure checks'})
        self.store.upsert(self.fleet['id'], [], repository_conditions={'owner/repo': 'old'})
        self.store.upsert(self.fleet['id'], [], repository_conditions={'owner/repo': 'new'})
        self.assertEqual(self.store.internal()['items'][item['id']]['status'], 'queued')

    def test_new_head_refreshes_unstarted_comment_but_never_rewrites_prepared_write(self):
        original = candidate(1, 'fix_comment', comment_id='inline:7', comment_revision='same', body='Fix this crash', source_ref='feature')
        self.store.upsert(self.fleet['id'], [original])
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'blocked', 'error': 'Head moved'})
        self.store.upsert(self.fleet['id'], [{**original, 'head_sha': 'c' * 40}])
        refreshed = self.store.internal()['items'][item['id']]
        self.assertEqual(refreshed['status'], 'queued')
        self.assertEqual(refreshed['head_sha'], 'c' * 40)
        second = self.store.claim(2)
        self.store.intent(second['id'], second['lease'], 'push', {'prepared_commit_sha': 'd' * 40})
        self.store.finish(second['id'], second['lease'], {'status': 'blocked', 'error': 'Uncertain push'})
        self.store.upsert(self.fleet['id'], [{**original, 'head_sha': 'e' * 40}])
        preserved = self.store.internal()['items'][item['id']]
        self.assertEqual(preserved['head_sha'], 'c' * 40)
        self.assertEqual(preserved['status'], 'blocked')

    def test_retry_stays_on_its_original_provider_without_substitution(self):
        self.store.control('stop', self.fleet['id'])
        f = fleet(self.store, workers=10, deepseek_workers=10)
        self.store.upsert(f['id'], [candidate(1)])
        item = self.store.claim(11)
        self.store.recover(item['id'], item['lease'], 'Provider temporarily unavailable')
        with self.store._locked(True) as state:
            state['items'][item['id']]['eligible_at'] = ''
        self.assertIsNone(self.store.claim(1))
        self.assertEqual(self.store.claim(12)['provider'], 'deepseek')

    def test_rate_limit_waits_automatically_beyond_three_occurrences_without_fault_budget(self):
        self.store.upsert(self.fleet['id'], [candidate(1), candidate(2)])
        for _ in range(4):
            item = self.store.claim(1)
            self.store.finish(item['id'], item['lease'], {'status': 'retrying', 'retry_kind': 'rate_limit',
                'retry_after': 1200, 'summary': 'Provider rate limit'})
            self.assertIsNone(self.store.claim(2))
            saved = self.store.internal()['items'][item['id']]
            self.assertEqual(saved['status'], 'retrying')
            self.assertEqual(saved['fault_attempts'], 0)
            with self.store._locked(True) as state:
                state['rate_limits']['glm']['until'] = ''
                state['items'][item['id']]['eligible_at'] = ''
        self.assertIsNotNone(self.store.claim(2))

    def test_repeated_service_outage_enters_automatic_cooldown_instead_of_reset_chore(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        for attempt in range(3):
            with self.store._locked(True) as state:
                for value in state['items'].values():
                    value['eligible_at'] = ''
            item = self.store.claim(attempt + 1)
            self.store.finish(item['id'], item['lease'], {'status': 'retrying', 'retry_kind': 'service_unavailable',
                                                        'summary': 'Provider unavailable'})
        saved = self.store.internal()['items'][item['id']]
        self.assertEqual(saved['status'], 'retrying')
        self.assertEqual(saved['fault_attempts'], 3)
        self.assertEqual(saved['fault_allowance'], 6)
        self.assertIsNone(self.store.claim(4))

    def test_restart_selected_scope_blocks_old_queue_and_retains_uncertain_write(self):
        excluded = candidate(3, 'fix_comment', repo='owner/excluded', repo_id=99, comment_id='9', body='Fix crash')
        self.store.upsert(self.fleet['id'], [candidate(1), excluded],
                          repositories=[{'repo': 'owner/repo', 'id': 42}, {'repo': 'owner/excluded', 'id': 99}])
        uncertain = self.store.claim(1)
        operation = self.store.intent(uncertain['id'], uncertain['lease'], 'push', {'prepared_commit_sha': 'c' * 40})
        self.store.control('stop', self.fleet['id'])
        self.store.recover(uncertain['id'], uncertain['lease'], 'Child exited')
        selected = self.store.configure({'account': 'alice', 'output_dir': '/home/alice/reports', 'github_id': 123,
                                        'github_login': 'alice', 'workers': 10, 'scope': 'selected', 'repos': ['owner/repo']})
        self.assertEqual(selected['id'], self.fleet['id'])
        saved = self.store.internal()['items'][uncertain['id']]
        self.assertEqual(saved['status'], 'blocked')
        self.assertTrue(saved['scope_excluded'])
        self.assertEqual(saved['mutation_intents'][0]['id'], operation['id'])
        with self.assertRaisesRegex(ValueError, 'outside the current writable scope'):
            self.store.control('retry', item_id=uncertain['id'])
        self.assertEqual(self.store.claim(2)['repo'], 'owner/repo')
        self.assertIsNone(self.store.claim(3))

    def test_access_removal_fences_running_and_blocks_queue_then_restoration_reconciles(self):
        self.store.upsert(self.fleet['id'], [candidate(1), candidate(2)], repositories=[{'repo': 'owner/repo', 'id': 42}])
        running = self.store.claim(1)
        operation = self.store.intent(running['id'], running['lease'], 'comment', {'body_digest': 'recorded-comment'})
        self.store.upsert(self.fleet['id'], [], repositories=[])
        self.assertFalse(self.store.alive(running['id'], running['lease']))
        self.assertIsNone(self.store.claim(2))
        with self.assertRaises(FleetStateError):
            self.store.intent(running['id'], running['lease'], 'comment', {'body_digest': 'another-comment'})
        self.store.receipt(running['id'], running['lease'], operation['id'], {'id': 99})
        self.store.recover(running['id'], running['lease'], 'Access removed; child exited')
        self.store.upsert(self.fleet['id'], [], repositories=[])
        self.assertTrue(all(i['status'] == 'blocked' for i in self.store.internal()['items'].values()))
        self.store.upsert(self.fleet['id'], [], repositories=[{'repo': 'owner/repo', 'id': 42}])
        self.assertTrue(all(i['status'] == 'queued' for i in self.store.internal()['items'].values()))
        saved = self.store.internal()['items'][running['id']]
        self.assertEqual(saved['mutation_intents'][0]['result'], {'id': 99})

    def test_restart_cannot_forget_previous_removed_access_catalog(self):
        self.store.upsert(self.fleet['id'], [candidate(1)], repositories=[])
        self.store.control('stop', self.fleet['id'])
        fleet(self.store)
        self.assertIsNone(self.store.claim(1))
        self.assertEqual(self.store.snapshot()['items'][0]['status'], 'blocked')

    def test_explicit_provider_pool_change_rebinds_retry_with_saved_writes_and_usage_history(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        item = self.store.claim(1)
        operation = self.store.intent(item['id'], item['lease'], 'comment', {'body_digest': 'prepared-before-pool-change'})
        self.store.heartbeat(item['id'], item['lease'], 'Explaining', model_calls=7)
        self.store.control('stop', self.fleet['id'])
        self.store.recover(item['id'], item['lease'], 'Stopped process exited')
        replacement = fleet(self.store, workers=0, deepseek_workers=1)
        self.assertEqual(replacement['id'], self.fleet['id'])
        self.store.control('retry', item_id=item['id'])
        retry = self.store.claim(11)
        self.assertIsNotNone(retry)
        self.assertEqual(retry['provider'], 'deepseek')
        self.assertEqual(retry['attempts'], 2)
        self.assertEqual(retry['model_calls'], 7)
        self.assertEqual(retry['mutation_intents'][0]['id'], operation['id'])
        self.assertEqual(retry['provider_history'][0]['from'], 'glm')
        self.assertEqual(retry['provider_history'][0]['to'], 'deepseek')
        self.assertTrue(any(entry['event'] == 'recovered' for entry in retry['history']))
        self.assertNotEqual(retry['lease'], item['lease'])

    def test_explicit_pool_change_can_leave_old_provider_cooldown_without_automatic_fallback(self):
        self.store.control('stop', self.fleet['id'])
        active = fleet(self.store, workers=1, deepseek_workers=1)
        self.store.upsert(active['id'], [candidate(1)])
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'retrying', 'retry_kind': 'rate_limit',
                                                     'retry_after': 3600, 'retry_scope': 'provider'})
        self.assertIsNone(self.store.claim(11), 'A 429 alone must not switch providers.')
        self.store.control('stop', active['id'])
        fleet(self.store, workers=0, deepseek_workers=1)
        changed = self.store.claim(11)
        self.assertIsNotNone(changed)
        self.assertEqual(changed['provider'], 'deepseek')
        self.assertTrue(self.store.internal()['rate_limits']['glm']['until'])

    def test_saved_verification_requeues_source_fork_without_discovery_condition_metadata(self):
        self.store.upsert(self.fleet['id'], [candidate(1, 'fix_comment', comment_id='inline:8',
                                                      source_repo='contributor/Fork', body='Fix the failure')])
        item = self.store.claim(1)
        operation = self.store.intent(item['id'], item['lease'], 'push', {'prepared_commit_sha': 'd' * 40})
        self.store.finish(item['id'], item['lease'], {'status': 'blocked', 'error': 'No verification command'})
        before = self.store.internal()['items'][item['id']]
        self.assertNotIn('condition_key', before)
        self.assertEqual(self.store.verification_changed('owner/repo')['count'], 0)
        changed = self.store.verification_changed('CONTRIBUTOR/fork')
        self.assertEqual(changed['requeued'], [item['id']])
        saved = self.store.internal()['items'][item['id']]
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['attempts'], before['attempts'])
        self.assertEqual(saved['mutation_intents'][0]['id'], operation['id'])
        self.assertEqual(saved['result'], before['result'])
        self.assertEqual(saved['fault_allowance'], saved['fault_attempts'] + 3)
        self.assertTrue(self.store.internal()['supervisor']['refresh_requested'])
        self.assertTrue(any(entry['event'] == 'verification-changed' for entry in saved['history']))

    def test_verification_change_preserves_hard_limits_and_excluded_work(self):
        self.store.upsert(self.fleet['id'], [candidate(1), candidate(2),
            candidate(3, 'fix_comment', comment_id='9', body='Repair', correction_pr={'repair_round': 3})])
        with self.store._locked(True) as state:
            for item in state['items'].values():
                item['status'] = 'blocked'
                if item['number'] == 1:
                    item['auto_recheck'] = False
                if item['number'] == 2:
                    item['repo'] = 'owner/excluded'
                    item['source_repo'] = 'owner/repo'
            state['fleets'][self.fleet['id']]['scope'] = 'selected'
            state['fleets'][self.fleet['id']]['repos'] = ['owner/repo']
        self.assertEqual(self.store.verification_changed('owner/repo')['count'], 0)
        self.assertTrue(all(i['status'] == 'blocked' for i in self.store.internal()['items'].values()))
