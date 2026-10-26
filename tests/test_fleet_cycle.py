"""One actual-Git lifecycle across discovery, durable scheduling and task execution.

Only GitHub transport and model messages are simulated. Locks, policy selection,
Git changes, isolated checks, write intents, readiness and finalization are real.
"""
from __future__ import annotations

import copy
import shutil
import unittest

from tests import sandbox_available
from urllib.parse import unquote, urlsplit

from ghagent import fleet_runner as fr
from ghagent.fleet_discovery import FleetDiscovery
from ghagent.fleet_store import FleetStore
from ghagent.github_client import NotFound
from tests import test_fleet_runner as fixtures
from tests.test_fleet_runner import GitHub, Model, call, finish, git


class CycleGitHub(GitHub):
    """The API facade reads refs/trees from the actual temporary bare repository."""

    def __init__(self, bare, base, head):
        super().__init__(bare, base, head)
        self.prs[1].update(title='Correct the documented result', created_at='2026-10-25T00:30:00Z')
        self.reads = []

    def rest(self, method, path, body=None, paginate=False):
        path = path.lstrip('/')
        parsed = urlsplit(path)
        endpoint = parsed.path
        if method == 'GET':
            self.reads.append(path)
            if endpoint == 'user':
                return {'id': self.login, 'login': 'owner'}
            if endpoint == 'user/repos':
                assert paginate
                return [self.repo()]
            if endpoint.endswith('/collaborators/owner/permission'):
                return {'permission': 'admin'}
            if endpoint.endswith('/contents/.github/workflows') or endpoint.endswith('/contents/.gitattributes'):
                raise NotFound('HTTP 404 missing', exit_code=1)
            if endpoint.endswith('/rulesets'):
                return []
            if endpoint == 'repos/owner/repo/branches':
                assert paginate
                refs = git(self.bare, 'for-each-ref', '--format=%(refname:short)|%(objectname)', 'refs/heads').splitlines()
                return [{'name': row.split('|')[0], 'commit': {'sha': row.split('|')[1]}} for row in refs]
            if endpoint.startswith('repos/owner/repo/commits/'):
                ref = unquote(endpoint.split('/commits/', 1)[1])
                sha = git(self.bare, 'rev-parse', ref)
                return {'sha': sha, 'commit': {'tree': {'sha': git(self.bare, 'rev-parse', sha + '^{tree}')},
                                              'committer': {'date': '2026-10-25T00:00:00Z'}}}
            if endpoint.startswith('repos/owner/repo/git/trees/'):
                tree = endpoint.split('/trees/', 1)[1]
                entries = []
                for line in git(self.bare, 'ls-tree', '-r', tree).splitlines():
                    metadata, name = line.split('\t', 1)
                    _, kind, sha = metadata.split()
                    entries.append({'path': name, 'type': kind, 'sha': sha,
                                    'size': int(git(self.bare, 'cat-file', '-s', sha))})
                return {'tree': entries, 'truncated': False}
            if endpoint == 'repos/owner/repo/pulls' and 'state=open' in parsed.query:
                assert paginate
                return [self._current_pr(number) for number, pr in self.prs.items() if pr['state'] == 'open']
            if endpoint.startswith('repos/owner/repo/pulls/') and endpoint.endswith(('/comments', '/reviews')):
                assert paginate
                return []
            if endpoint.startswith('repos/owner/repo/issues/') and endpoint.endswith('/comments'):
                return copy.deepcopy(self.comments.get(endpoint, []))
            if endpoint.startswith('repos/owner/repo/pulls/') and endpoint.rsplit('/', 1)[1].isdigit():
                return self._current_pr(int(endpoint.rsplit('/', 1)[1]))
        if method == 'POST' and endpoint == 'repos/owner/repo/pulls':
            self.writes.append((endpoint, copy.deepcopy(body)))
            number = max(self.prs, default=0) + 1
            result = {**self.pr(number, body['head'], self.ref(body['head']), body['base'], self.ref(body['base'])),
                      'body': body['body'], 'user': {'id': 42}}
            self.prs[number] = copy.deepcopy(result)
        else:
            result = super().rest(method, endpoint, body=body, paginate=paginate)
        if method == 'POST' and endpoint == 'repos/owner/repo/pulls':
            result.update(title=body['title'], created_at=f'2026-10-25T01:{result["number"]:02d}:00Z')
            self.prs[result['number']] = copy.deepcopy(result)
        return result

    def _current_pr(self, number):
        pr = copy.deepcopy(self.prs[number])
        pr['head']['sha'] = self.ref(pr['head']['ref'])
        pr['base']['sha'] = self.ref(pr['base']['ref'])
        return pr

    def graphql(self, query, variables):
        if 'pullRequest(number:' in query:
            pr = self._current_pr(variables['number'])
            return {'repository': {'pullRequest': {
                'headRefOid': pr['head']['sha'], 'isDraft': False,
                'state': 'OPEN' if pr['state'] == 'open' else 'MERGED',
                'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'reviewDecision': None,
                # There are deliberately no GitHub Actions. Actual isolated local checks supply evidence.
                'commits': {'nodes': [{'commit': {'statusCheckRollup': None}}]},
                'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
        return super().graphql(query, variables)


@unittest.skipUnless(sandbox_available(), 'This integration exercises the actual isolated Linux check runner')
class FleetCycleTests(unittest.TestCase):
    checks = fixtures.FleetRunnerTests.checks
    process = fixtures.FleetRunnerTests.process

    def setUp(self):
        fixtures.FleetRunnerTests.setUp(self)
        # Reuse only its temporary repository construction and Git-transport redirection.
        # Undo every implementation patch: this test runs the production helpers.
        for fixture_patch in reversed(self.patches):
            fixture_patch.stop()
        self.gh = CycleGitHub(self.bare, self.base, self.head)
        self.store = FleetStore(self.root / 'state' / 'fleet-v1')
        self.fleet = self.store.configure({'account': 'fixture', 'member_home': str(self.root),
            'output_dir': str(self.output), 'github_id': 42, 'github_login': 'owner',
            'scope': 'all', 'repos': [], 'workers': 10})
        self.discovery = FleetDiscovery(self.gh, self.cfg, self.store)

    def execute_claim(self, slot, responses, *, expected_kind=None):
        item = self.store.claim(slot)
        self.assertIsNotNone(item)
        if expected_kind:
            self.assertEqual(item['kind'], expected_kind)
        model = Model(responses)
        result = fr.execute_item(self.cfg, {}, item, self.store, gh=self.gh,
                                 model_factory=lambda *args: model, runner=self.process)
        self.assertEqual(result['status'], 'done', result)
        self.store.finish(item['id'], item['lease'], result)
        self.assertFalse(model.responses, 'Every simulated model response should be used.')
        return item, result, model

    def advance_branch(self, branch, *, filename, content):
        git(self.source, 'fetch', str(self.bare), branch)
        git(self.source, 'checkout', '--detach', 'FETCH_HEAD')
        (self.source / filename).write_text(content)
        git(self.source, 'add', filename)
        git(self.source, 'commit', '-m', 'A new incoming change')
        sha = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'push', str(self.bare), 'HEAD:refs/heads/' + branch)
        return sha

    def test_complete_cycle_keeps_merge_readiness_fresh_and_next_claim_newest(self):
        self.discovery.scan()
        first_review, _, _ = self.execute_claim(1, [finish(findings=[{
            'title': 'Wrong result', 'body': 'Return zero for this case.', 'path': 'calc.py', 'line': 2}])],
            expected_kind='review_pr')
        self.assertEqual(len(self.store.snapshot()['comments']), 1)
        original = self.store.snapshot()['comments'][0]
        self.assertTrue(original['posted_comment_id'])
        self.assertIsNone(original.get('posted_issue_number'))
        self.assertNotIn('thread_id', original)
        fixed_item, fixed, _ = self.execute_claim(2, [finish(actionable=True, paths=['calc.py']),
            call('edit_file', path='calc.py', old_text='return 2', new_text='return 0'), finish()], expected_kind='fix_comment')
        correction = fixed['fix_pr']
        self.assertEqual(correction['target_ref'], 'feature')
        self.assertEqual(git(self.bare, 'show', correction['head_sha'] + ':calc.py'), 'def value():\n    return 0')
        self.assertEqual(git(self.bare, 'show', correction['head_sha'] + ':other.py'), 'untouched = True')
        self.assertTrue(fixed['checks'][0]['passed'])
        self.assertTrue(fixed['checks'][0]['isolated'])
        # Publication queues this next stage without another account discovery pass.
        # Another slot reviews the new head, using its own fresh model conversation and real checks.
        reviewer, reviewed, _ = self.execute_claim(3, [finish(findings=[])], expected_kind='review_pr')
        self.assertEqual(reviewer['author_slot'], fixed_item['slot'])
        self.assertNotEqual(reviewer['slot'], reviewer['author_slot'])
        self.assertEqual(reviewed['verified_head_sha'], correction['head_sha'])
        # Completed review supplies current merge evidence immediately, too.
        ready = self.store.snapshot()['ready']
        self.assertEqual([p['number'] for p in ready], [correction['number']])
        self.assertIn('Local checks', ready[0]['readiness']['checks_source'])
        self.assertEqual(len(self.store.snapshot()['comments']), 1, 'Opening/reviewing a correction does not resolve its finding.')

        # An incoming push invalidates readiness immediately on the next successful read.
        new_head = self.advance_branch(correction['branch'], filename='notes.txt', content='An additional documented detail.\n')
        self.discovery.scan()
        self.assertEqual(self.store.snapshot()['ready'], [])
        fresh_review, _, _ = self.execute_claim(4, [finish(findings=[])], expected_kind='review_pr')
        self.assertEqual(fresh_review['head_sha'], new_head)
        self.discovery.scan()
        self.assertEqual(len(self.store.snapshot()['ready']), 1)

        # Simulate the human's merge at the remote boundary by advancing the actual bare branch.
        git(self.bare, 'update-ref', 'refs/heads/feature', new_head)
        self.gh.prs[correction['number']].update(state='closed', merged=True,
            merged_at='2026-10-25T03:00:00Z', merge_commit_sha=new_head)
        self.discovery.scan()
        # A merged flag alone must not close a conversation finding: its intended target matters.
        invalid = self.store.claim(5)
        self.assertEqual(invalid['phase'], 'resolve_thread')
        self.gh.prs[correction['number']]['base']['ref'] = 'main'
        writes_before = len(self.gh.writes)
        refused_model = Model([])
        refused = fr.execute_item(self.cfg, {}, invalid, self.store, gh=self.gh,
                                  model_factory=lambda *args: refused_model, runner=self.process)
        self.assertEqual(refused['status'], 'blocked', refused)
        self.assertFalse(refused.get('resolved'))
        self.assertEqual(len(self.gh.writes), writes_before)
        self.store.finish(invalid['id'], invalid['lease'], refused)
        self.assertEqual(len(self.store.snapshot()['comments']), 1)
        # Restore the deliberately invalid API fixture and explicitly retry that rejected proof.
        self.gh.prs[correction['number']]['base']['ref'] = 'feature'
        self.store.control('retry', item_id=invalid['id'])
        final_item, finalized, final_model = self.execute_claim(6, [], expected_kind='fix_comment')
        self.assertEqual(final_item['id'], fixed_item['id'])
        self.assertEqual(final_item['phase'], 'resolve_thread')
        self.assertTrue(finalized['resolved'])
        self.assertFalse(final_model.messages, 'Confirmed merge finalization must not ask a model to invent proof.')
        self.assertEqual(self.store.snapshot()['comments'], [])
        self.assertEqual(len(self.gh.issues), 0, 'A conversation finding must not invent an issue to close.')
        self.assertEqual(self.store.snapshot()['ready'], [])

        # Two new requests arrive while old background files remain. The next free agent takes newest first.
        for number, branch, created in [(8, 'older-request', '2026-10-25T04:00:00Z'),
                                        (9, 'newest-request', '2026-10-25T05:00:00Z')]:
            git(self.bare, 'update-ref', 'refs/heads/' + branch, new_head)
            self.gh.prs[number] = {**self.gh.pr(number, branch, new_head, 'main', new_head),
                                   'title': branch, 'created_at': created}
        self.discovery.scan()
        newest = self.store.claim(7)
        self.assertEqual(newest['kind'], 'review_pr')
        self.assertEqual(newest['number'], 9)
        # No authenticated GitHub transport, live model, merge API or branch-deletion API was used.
        self.assertEqual(len(self.pushes), 1)
        self.assertTrue(any('/rulesets' in path for path in self.gh.reads))
        self.assertTrue(list((self.root / 'state' / 'fleet-v1' / 'locks').glob('*.lock')))
