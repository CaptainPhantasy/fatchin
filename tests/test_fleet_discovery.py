import copy
import tempfile
import unittest
from pathlib import Path

from ghagent.fleet_discovery import FleetDiscovery
from ghagent.fleet_store import FleetStore
from ghagent.interfaces import Config, RepoConfig
from tests.test_fleet_store import fleet


class GitHub:
    def __init__(self):
        self.calls = []
        self.user = 123
        self.repo = {'id': 42, 'full_name': 'owner/repo', 'default_branch': 'main', 'permissions': {'push': True}, 'private': True}
        self.pr = {'number': 1, 'title': 'Newest feature', 'created_at': '2026-10-25T00:00:00Z',
                   'html_url': 'https://github.com/owner/repo/pull/1', 'head': {'sha': 'a'*40, 'ref': 'feature', 'repo': self.repo},
                   'base': {'sha': 'b'*40, 'ref': 'main', 'repo': self.repo}, 'merged': False}
        self.comments = []
        self.inline = []
        self.reviews = []
        self.fail = None
        self.truncate = False
        self.merge_state = 'CLEAN'
        self.review_decision = 'APPROVED'
        self.rollup = 'SUCCESS'
        self.draft = False
        self.graph_head = 'a'*40
        self.threads = []
        self.branches = [{'name': 'main', 'commit': {'sha': 'b'*40}}, {'name': 'feature', 'commit': {'sha': 'a'*40}}, {'name': 'unpublished', 'commit': {'sha': 'c'*40}}]
        self.files = [{'filename': 'src/app.py', 'changes': 4},
                      {'filename': '.env.py', 'changes': 1},
                      {'filename': 'node_modules/lib.js', 'changes': 1}]
        self.permission = 'write'
        self.open_prs = True

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append((method, path, paginate))
        if self.fail and self.fail in path:
            raise RuntimeError('Denied')
        if method != 'GET':
            raise AssertionError('Discovery must never mutate GitHub')
        if path == '/user':
            return {'id': self.user, 'login': 'alice'}
        if path.startswith('/user/repos?'):
            assert paginate
            return [self.repo, {**self.repo, 'id': 99, 'full_name': 'owner/readonly', 'permissions': {'pull': True}},
                    {**self.repo, 'id': 100, 'full_name': 'owner/archived', 'archived': True}]
        if '/collaborators/' in path:
            return {'permission': self.permission}
        if '/pulls?state=' in path:
            assert paginate
            return [self.pr] if self.open_prs else []
        if '/issues/1/comments?' in path:
            assert paginate
            return copy.deepcopy(self.comments)
        if '/pulls/1/comments?' in path:
            assert paginate
            return copy.deepcopy(self.inline)
        if '/pulls/1/reviews?' in path:
            assert paginate
            return copy.deepcopy(self.reviews)
        if '/pulls/' in path:
            return copy.deepcopy(self.pr)
        if '/branches?' in path:
            assert paginate
            return self.branches
        if '/commits/' in path:
            branch = path.rsplit('/', 1)[-1]
            sha = next(b['commit']['sha'] for b in self.branches if b['name'] == branch)
            return {'sha': sha, 'parents': [{'sha': 'f' * 40}],
                    'commit': {'tree': {'sha': 'tree-' + branch}, 'message': 'fixture change',
                               'committer': {'date': '2026-10-25T00:00:00Z'}},
                    'stats': {'total': len(self.files)},
                    'files': copy.deepcopy(self.files)}
        if '/git/trees/' in path:
            return {'truncated': self.truncate, 'tree': [
                {'path': 'src/app.py', 'sha': 'blob-app-' + ('dev' if 'unpublished' in path else 'main'), 'type': 'blob', 'size': 123},
                {'path': '.env.py', 'sha': 'secret', 'type': 'blob', 'size': 123},
                {'path': 'node_modules/lib.js', 'sha': 'vendor', 'type': 'blob', 'size': 123}]}
        raise AssertionError(path)

    def graphql(self, query, variables):
        return {'repository': {'pullRequest': {'headRefOid': self.graph_head, 'isDraft': self.draft,
                'state': 'OPEN', 'mergeable': 'MERGEABLE', 'mergeStateStatus': self.merge_state,
                'reviewDecision': self.review_decision,
                'commits': {'nodes': [{'commit': {'statusCheckRollup': {'state': self.rollup} if self.rollup else None}}]},
                'reviewThreads': {'nodes': self.threads, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}


def comment(i, body='Please fix the off-by-one error.', **fields):
    return {'id': i, 'body': body, 'user': {'login': 'alice'}, 'created_at': '2026-10-25T01:00:00Z',
            'updated_at': '2026-10-25T01:00:00Z', 'html_url': f'https://github.com/owner/repo/pull/1#issuecomment-{i}', **fields}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FleetStore(Path(self.temp.name) / 'fleet')
        self.fleet = fleet(self.store)
        self.gh = GitHub()
        self.cfg = Config(state_dir=self.temp.name, worktree_root=self.temp.name,
                          repos=[RepoConfig(owner='owner', name='repo', checks=['python -m unittest'])])
        self.discovery = FleetDiscovery(self.gh, self.cfg, self.store)

    def test_all_writable_repos_paginate_all_comment_types_and_nonpr_branch_pushes(self):
        self.gh.comments = [comment(1)]
        self.gh.inline = [comment(2, path='src/app.py', line=8)]
        self.gh.reviews = [comment(3, body='This crashes with an empty list.')]
        self.discovery.scan()
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot['comments']), 3)
        self.assertEqual({i['source_ref'] for i in snapshot['items'] if i['kind'] == 'review_code'}, {'main'})
        self.assertEqual({len(i.get('paths') or []) for i in snapshot['items'] if i['kind'] == 'review_code'}, {1})
        # First sighting of a branch is only the baseline; a later push promotes.
        self.assertEqual({i['source_ref'] for i in snapshot['items'] if i['kind'] == 'open_pr'}, set())
        self.gh.branches[-1]['commit']['sha'] = 'd' * 40
        self.discovery.scan()
        promoted = self.store.snapshot()['items']
        self.assertEqual({i['source_ref'] for i in promoted if i['kind'] == 'open_pr'}, {'unpublished'})
        self.assertEqual({i['repo'] for i in snapshot['items']}, {'owner/repo'})
        self.assertTrue(all('.env' not in i.get('path', '') and 'node_modules' not in i.get('path', '') for i in snapshot['items']))
        self.assertTrue(all(c[0] == 'GET' for c in self.gh.calls))

    def test_ready_requires_completed_review_current_checks_approvals_and_no_comments(self):
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['ready'], [])
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'done'})
        self.discovery.scan()
        self.assertEqual(len(self.store.snapshot()['ready']), 1)
        for field, value in [('draft', True), ('rollup', 'PENDING'), ('review_decision', 'REVIEW_REQUIRED'), ('merge_state', 'UNKNOWN'), ('graph_head', 'z'*40)]:
            original = getattr(self.gh, field)
            setattr(self.gh, field, value)
            self.discovery.scan()
            self.assertEqual(self.store.snapshot()['ready'], [], field)
            setattr(self.gh, field, original)
        self.gh.comments = [comment(1)]
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['ready'], [])

    def test_failed_pagination_invalidates_ready_does_not_resolve_or_erase_records(self):
        self.discovery.scan()
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'done'})
        self.discovery.scan()
        self.assertEqual(len(self.store.snapshot()['ready']), 1)
        self.gh.fail = '/comments?'
        self.discovery.scan()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['ready'], [])
        self.assertTrue(snapshot['supervisor']['discovery_error'])
        self.assertEqual(self.store.internal()['items'][item['id']]['status'], 'done')

    def test_changed_identity_stops_claims_and_discovery_recovers_automatically(self):
        self.discovery.scan()
        self.gh.user = 999
        self.discovery.scan()
        self.assertIsNone(self.store.claim(1))
        self.assertIn('identity changed', self.store.snapshot()['fleets'][0]['error'])
        self.gh.user = 123
        self.discovery.scan()
        self.assertIsNotNone(self.store.claim(1))

    def test_untrusted_comments_resolved_threads_and_fleet_narration_not_queued(self):
        self.gh.comments = [comment(1, body='<!-- fatchin:fleet:v1 --> Review complete.')]
        self.gh.inline = [comment(2, path='x.py')]
        self.gh.threads = [{'isResolved': True, 'isOutdated': False,
                            'comments': {'nodes': [{'databaseId': 2}], 'pageInfo': {'hasNextPage': False}}}]
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['comments'], [])
        self.gh.comments = [comment(3)]
        self.gh.permission = 'read'
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['comments'], [])

    def test_first_sighting_is_baseline_and_a_new_push_promotes(self):
        self.discovery.scan()
        self.assertFalse([i for i in self.store.snapshot()['items'] if i['kind'] == 'open_pr'])
        count = len(self.store.snapshot()['items'])
        self.discovery.scan()
        self.assertEqual(len(self.store.snapshot()['items']), count)
        self.gh.branches[-1]['commit']['sha'] = 'd' * 40
        self.discovery.scan()
        fresh = [i for i in self.store.snapshot()['items']
                 if i['kind'] == 'open_pr' and i['source_ref'] == 'unpublished']
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]['head_sha'], 'd' * 40)

    def test_unreadable_commit_never_claims_complete_discovery(self):
        self.gh.fail = '/commits/'
        self.discovery.scan()
        self.assertIn('owner/repo', self.store.snapshot()['supervisor']['discovery_failed_repos'])

    def test_policy_uses_the_github_login_for_personal_repositories(self):
        self.cfg.fleet_policy = {'personal_default': 'include', 'org_default': 'exclude'}
        self.fleet = fleet(self.store, account='floyd', github_login='owner')
        self.discovery = FleetDiscovery(self.gh, self.cfg, self.store)
        self.discovery.scan()
        snapshot = self.store.snapshot()
        current = next(f for f in snapshot['fleets'] if f['account'] == 'floyd')
        self.assertEqual(current['policy_skipped'], [])
        self.assertTrue(any(i['repo'] == 'owner/repo' for i in snapshot['items']))

    def test_commit_review_is_batched_by_changed_files(self):
        self.gh.branches = [{'name': 'main', 'commit': {'sha': 'b' * 40}}]
        self.gh.files = [{'filename': f'src/module_{i}.py', 'changes': 2} for i in range(60)]
        self.discovery.scan()
        items = [i for i in self.store.snapshot()['items'] if i['kind'] == 'review_code']
        self.assertEqual(len(items), 3)
        self.assertTrue(all(len(i['paths']) <= 25 for i in items))
        self.assertEqual(len({p for i in items for p in i['paths']}), 60)
        self.assertTrue(all(i['base_sha'] == 'f' * 40 and i['head_sha'] == 'b' * 40 for i in items))
        self.assertEqual(self.store.snapshot()['ready'], [])

    def test_confirmed_merge_still_requires_finalization_of_conversation_findings(self):
        self.discovery.scan()
        review = self.store.claim(1)
        self.store.finish(review['id'], review['lease'], {'status': 'done', 'findings': [{'body': 'Bad bounds', 'path': 'src/app.py'}]})
        fix = self.store.claim(2)
        self.store.finish(fix['id'], fix['lease'], {'status': 'done', 'fix_pr': {'repo': 'owner/repo', 'number': 1, 'author_slot': 2}})
        self.discovery.scan()
        self.assertEqual(len(self.store.snapshot()['comments']), 1)
        self.gh.pr.update(merged=True, merged_at='2026-10-25T02:00:00Z')
        self.discovery.scan()
        self.assertEqual(len(self.store.snapshot()['comments']), 1)
        finalization = self.store.claim(3)
        self.assertEqual(finalization['id'], fix['id'])
        self.assertEqual(finalization['phase'], 'resolve_thread')
        self.store.finish(finalization['id'], finalization['lease'], {'status': 'done', 'resolved': True})
        self.assertEqual(self.store.snapshot()['comments'], [])

    def test_local_checks_allow_no_actions_readiness_but_never_override_remote_failure(self):
        self.gh.rollup = None
        self.discovery.scan()
        item = self.store.claim(1)
        self.store.finish(item['id'], item['lease'], {'status': 'done', 'verified_head_sha': 'a' * 40,
                                                     'checks': [{'command': 'python -m unittest', 'exit': 0, 'passed': True}]})
        self.discovery.scan()
        ready = self.store.snapshot()['ready']
        self.assertEqual(len(ready), 1)
        self.assertIn('Local checks', ready[0]['readiness']['checks_source'])
        self.gh.rollup = 'FAILURE'
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['ready'], [])

    def test_outdated_open_thread_stays_actionable_and_merge_queues_real_resolution(self):
        self.gh.inline = [comment(2, path='src/app.py', line=1)]
        self.gh.threads = [{'id': 'thread-2', 'isResolved': False, 'isOutdated': True,
                            'comments': {'nodes': [{'databaseId': 2}], 'pageInfo': {'hasNextPage': False}}}]
        self.discovery.scan()
        fix = self.store.claim(1)
        self.assertEqual(fix['kind'], 'fix_comment')
        self.assertEqual(fix['thread_id'], 'thread-2')
        self.store.finish(fix['id'], fix['lease'], {'status': 'done', 'fix_pr': {'repo': 'owner/repo', 'number': 2}})
        self.gh.pr.update(merged=True, merged_at='2026-10-25T02:00:00Z', merge_commit_sha='e'*40)
        self.discovery.scan()
        saved = self.store.internal()['items'][fix['id']]
        self.assertEqual(saved['phase'], 'resolve_thread')
        self.assertEqual(saved['status'], 'queued')
        self.assertNotIn('resolved_at', saved)
        finalize = self.store.claim(2)
        self.assertEqual(finalize['id'], fix['id'])
        self.store.finish(finalize['id'], finalize['lease'], {'status': 'done', 'resolved': True})
        self.assertEqual(self.store.snapshot()['comments'], [])

    def test_external_resolution_closes_saved_comment_and_latest_repair_owns_review(self):
        self.gh.inline = [comment(2, path='src/app.py', line=1)]
        self.gh.threads = [{'id': 'thread-2', 'isResolved': False, 'isOutdated': False,
                            'comments': {'nodes': [{'databaseId': 2}], 'pageInfo': {'hasNextPage': False}}}]
        self.discovery.scan()
        fix = self.store.claim(1)
        self.store.finish(fix['id'], fix['lease'], {'status': 'done', 'fix_pr': {'repo': 'owner/repo', 'number': 1, 'repair_round': 0, 'author_slot': 1}})
        self.gh.threads[0]['isResolved'] = True
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['comments'], [])
        with self.store._locked(True) as state:
            state['items']['later-repair'] = {**state['items'][fix['id']], 'id': 'later-repair',
                'fix_pr': {'repo': 'owner/repo', 'number': 1, 'repair_round': 2, 'author_slot': 3,
                           'head_sha': 'a' * 40, 'root_finding_id': fix['id']}}
        self.discovery.scan()
        review = next(i for i in self.store.snapshot()['items'] if i['kind'] == 'review_pr')
        self.assertEqual(review['author_slot'], 3)
        self.assertEqual(review['correction_pr']['repair_round'], 2)
        claim = self.store.claim(3)
        self.assertTrue(claim is None or claim['kind'] != 'review_pr')
        reviewer = self.store.claim(4)
        self.assertEqual(reviewer['kind'], 'review_pr')

    def test_completed_pr_is_published_before_background_enumeration(self):
        original = self.gh.rest
        seen = []
        def probe(method, path, **kwargs):
            if '/branches?' in path:
                seen.extend(i for i in self.store.snapshot()['items'] if i['kind'] == 'review_pr')
            return original(method, path, **kwargs)
        self.gh.rest = probe
        self.discovery.scan()
        self.assertEqual(len(seen), 1)

    def test_unchanged_branch_tree_is_not_refetched_on_every_poll(self):
        self.discovery.scan()
        first = sum('/git/trees/' in c[1] for c in self.gh.calls)
        self.discovery.scan()
        self.assertEqual(sum('/git/trees/' in c[1] for c in self.gh.calls), first)

    def test_prepared_push_provenance_blocks_self_review_before_publisher_finish(self):
        self.discovery.scan()
        review = self.store.claim(1)
        self.store.finish(review['id'], review['lease'], {'status': 'done', 'findings': [{'body': 'Off by one', 'path': 'src/app.py'}]})
        fix = self.store.claim(2)
        self.gh.pr['body'] = 'Exact recorded correction body'
        self.store.intent(fix['id'], fix['lease'], 'push', {
            'repo': 'owner/repo', 'repository_id': 42, 'prepared_commit_sha': 'a' * 40,
            'branch': 'feature', 'target_ref': 'main', 'pr_body': self.gh.pr['body'],
            'root_finding_id': fix['id']})
        self.discovery.scan()
        item = next(i for i in self.store.snapshot()['items'] if i['kind'] == 'review_pr')
        self.assertEqual(item['author_slot'], 2)
        self.assertTrue(item['correction_pr']['publication_pending'])
        # A spoofed remote marker/body alone cannot invent local provenance.
        self.gh.pr['body'] = 'Unrelated text'
        self.assertIsNone(FleetDiscovery._correction(self.store.internal(), 'owner/repo', 1, self.gh.pr))

    def test_archive_and_write_access_removal_block_stale_work_before_repository_reads(self):
        self.discovery.scan()
        self.gh.repo['archived'] = True
        self.discovery.scan()
        self.assertIsNone(self.store.claim(1))
        self.assertTrue(all(i['scope_excluded'] for i in self.store.internal()['items'].values()))
        self.gh.repo['archived'] = False
        self.gh.repo['permissions'] = {'pull': True}
        self.discovery.scan()
        self.assertIsNone(self.store.claim(1))
        self.gh.repo['permissions'] = {'push': True}
        self.discovery.scan()
        self.assertIsNotNone(self.store.claim(1))
