"""Real isolated Git, simulated GitHub/model: fleet publication and recovery."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import subprocess
import shutil
import tempfile
import unittest

from tests import sandbox_available
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ghagent import fleet_runner as fr
from ghagent.github_client import NotFound
from ghagent.interfaces import Config, ModelConfig, RepoConfig
from ghagent.triggers import TriggerReport


def git(root, *args):
    proc = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def call(name, **args):
    return {'content': None, 'tool_calls': [{'id': 'tool1', 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(args)}}]}


def finish(**args):
    return call('finish', summary='The zero result is returned correctly.', complete=True, **args)


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def chat(self, messages, tools=None):
        self.messages.append(copy.deepcopy(messages))
        if not self.responses:
            raise AssertionError('Unexpected model call')
        return self.responses.pop(0)


class Store:
    def __init__(self, item):
        self.item = item
        self.item.setdefault('mutation_intents', [])
        self.live = True
        self.crash_receipt = None
        self.stop_on_intent = False

    def alive(self, ident, lease):
        return self.live and self.item['lease'] == lease

    def heartbeat(self, ident, lease, stage, **fields):
        return self.alive(ident, lease)

    def intent(self, ident, lease, kind, payload):
        if self.stop_on_intent:
            self.live = False
        if not self.alive(ident, lease):
            raise fr.FleetStopped('Stopped before write admission.')
        key = hashlib.sha256((kind + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        for op in self.item['mutation_intents']:
            if op['id'] == key:
                return copy.deepcopy(op)
        op = {'id': key, 'kind': kind, 'payload': copy.deepcopy(payload), 'state': 'prepared', 'result': None}
        self.item['mutation_intents'].append(op)
        return copy.deepcopy(op)

    def receipt(self, ident, lease, operation, result):
        for op in self.item['mutation_intents']:
            if op['id'] == operation:
                if self.crash_receipt == op['kind']:
                    self.crash_receipt = None
                    raise RuntimeError('Simulated crash after external write')
                op.update(state='confirmed', result=copy.deepcopy(result))
                return
        raise AssertionError('Missing intent')

    def internal(self):
        return {'items': {self.item['id']: copy.deepcopy(self.item)}}

    def reserve_publication(self, fleet_id, repo, cap):
        return True


class GitHub:
    def __init__(self, bare, base, head):
        self.bare, self.base = bare, base
        self.login = 42
        self.repo_permissions = True
        self.prs = {1: self.pr(1, 'feature', head, 'main', base)}
        self.comments = {}
        self.issues = []
        self.writes = []
        self.raise_after_post = False
        self.repo_permissions = True
        self.thread = {'id': 'PRT_fixture', 'isResolved': False, 'viewerCanResolve': True,
                       'pullRequest': {'number': 1, 'repository': {'databaseId': 51}},
                       'comments': {'nodes': [{'databaseId': 9}]}}

    def current_user_id(self):
        return self.login

    def repo(self, name='owner/repo'):
        return {'id': 51, 'full_name': name, 'default_branch': 'main', 'private': True,
                'permissions': {'push': self.repo_permissions}, 'archived': False, 'disabled': False}

    def pr(self, number, branch, head, target, base):
        return {'id': number + 100, 'number': number, 'state': 'open', 'draft': False,
                'head': {'sha': head, 'ref': branch, 'repo': self.repo()},
                'base': {'sha': base, 'ref': target, 'repo': self.repo()},
                'html_url': f'https://github.com/owner/repo/pull/{number}'}

    def ref(self, branch):
        p = subprocess.run(['git', '--git-dir', str(self.bare), 'rev-parse', '--verify', 'refs/heads/' + branch], capture_output=True, text=True)
        if p.returncode:
            raise NotFound('not found', exit_code=1)
        return p.stdout.strip()

    def rest(self, method, path, body=None, paginate=False):
        from urllib.parse import unquote
        if method == 'GET':
            if path == 'repos/owner/repo':
                return self.repo()
            if path.startswith('repos/owner/repo/compare/'):
                base, head = path.rsplit('/', 1)[1].split('...')
                ancestor = git(self.bare, 'merge-base', base, head)
                return {'status': 'identical' if base == head else 'ahead' if ancestor == base else 'diverged', 'merge_base_commit': {'sha': ancestor}}
            if path.startswith('repos/owner/repo/git/ref/heads/'):
                return {'object': {'sha': self.ref(unquote(path.split('/heads/', 1)[1]))}}
            if path == 'repos/owner/repo/pulls':
                expected = (body or {}).get('head', '').split(':', 1)[-1]
                return [copy.deepcopy(pr) for pr in self.prs.values() if not expected or pr['head']['ref'] == expected]
            if path.startswith('repos/owner/repo/pulls/'):
                number = int(path.rsplit('/', 1)[1])
                pr = copy.deepcopy(self.prs[number])
                pr['head']['sha'] = self.ref(pr['head']['ref'])
                pr['base']['sha'] = self.ref(pr['base']['ref'])
                return pr
            if path.endswith('/comments'):
                return copy.deepcopy(self.comments.get(path, []))
            if path == 'repos/owner/repo/issues':
                return copy.deepcopy(self.issues)
            if path.startswith('repos/owner/repo/issues/'):
                return copy.deepcopy(next(x for x in self.issues if x['number'] == int(path.rsplit('/', 1)[1])))
        elif method == 'PATCH':
            issue = next(x for x in self.issues if x['number'] == int(path.rsplit('/', 1)[1]))
            issue.update(body)
            self.writes.append((path, body))
            return copy.deepcopy(issue)
        elif method == 'POST':
            self.writes.append((path, copy.deepcopy(body)))
            result = {'id': 300 + len(self.writes), 'body': body['body'], 'user': {'id': 42},
                      'html_url': 'https://github.com/owner/repo/issues/1#issuecomment-' + str(300 + len(self.writes))}
            if path == 'repos/owner/repo/pulls':
                number = max(self.prs) + 1
                result.update(self.pr(number, body['head'], self.ref(body['head']), body['base'], self.ref(body['base'])))
                self.prs[number] = copy.deepcopy(result)
            elif path == 'repos/owner/repo/issues':
                result['number'] = 50 + len(self.issues)
                self.issues.append(copy.deepcopy(result))
            else:
                self.comments.setdefault(path, []).append(copy.deepcopy(result))
            if self.raise_after_post:
                self.raise_after_post = False
                raise TimeoutError('Simulated response loss')
            return result
        raise AssertionError((method, path, body))

    def graphql(self, query, variables):
        if query.startswith('mutation'):
            self.thread['isResolved'] = True
            self.writes.append(('graphql:resolve', variables))
            return {'resolveReviewThread': {'thread': {'id': self.thread['id'], 'isResolved': True}}}
        return {'node': copy.deepcopy(self.thread)}


class FleetRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init', '-b', 'main')
        git(self.source, 'config', 'user.name', 'Fixture')
        git(self.source, 'config', 'user.email', 'fixture@example.invalid')
        (self.source / 'calc.py').write_text('def value():\n    return 1\n')
        (self.source / 'other.py').write_text('untouched = True\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'baseline')
        self.base = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'switch', '-c', 'feature')
        (self.source / 'calc.py').write_text('def value():\n    return 2\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'feature')
        self.head = git(self.source, 'rev-parse', 'HEAD')
        self.bare = self.root / 'remote.git'
        git(self.source, 'clone', '--bare', str(self.source), str(self.bare))
        self.output = self.root / 'output'
        self.output.mkdir()
        self.cfg = Config(state_dir=str(self.root / 'state'), worktree_root=str(self.root / 'scratch'),
                          repos=[RepoConfig('owner', 'repo', checks=['python3 -m py_compile calc.py'])],
                          model=ModelConfig(), max_tool_iterations=10)
        # py_compile creates artifacts, so use a command checking syntax without
        # changing the candidate; no network or provider is used.
        self.cfg.repos[0].checks = ['python3 -c "import ast,pathlib; ast.parse(pathlib.Path(\'calc.py\').read_text())"']
        self.gh = GitHub(self.bare, self.base, self.head)
        self.counter = 0
        self.pushes = []
        self.patches = [patch.object(fr, '_locks', lambda *a: contextlib.nullcontext()),
                        patch.object(fr, '_finding_lock', lambda *a: contextlib.nullcontext()),
                        patch.object(fr.triggers, 'preflight', return_value=TriggerReport()),
                        patch.object(fr, '_repository_policy', lambda cfg, *a: cfg),
                        patch.object(fr, '_run_repository_checks', self.checks)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.temp.cleanup)

    def checks(self, cfg, root, commands, should_stop):
        from ghagent.coding_agent import CodingAgent
        agent = CodingAgent(None, cfg)
        agent._active_worktree = root
        return agent._run_checks(commands, should_stop)

    def item(self, kind='review_pr', **updates):
        self.counter += 1
        return {'id': 'item-' + str(self.counter), 'lease': 'lease-' + str(self.counter),
                'repo': 'owner/repo', 'repo_id': 51, 'number': 1,
                'head_sha': self.head, 'base_sha': self.base, 'kind': kind,
                'slot': 1, 'provider': 'glm', 'title': 'Fix incorrect return value',
                'body': 'The return must be zero, not two.', 'path': 'calc.py', 'line': 2,
                'url': 'https://github.com/owner/repo/pull/1#discussion_r9',
                'fleet': {'github_id': 42, 'github_login': 'owner', 'output_dir': str(self.output),
                          'member_home': str(self.root), 'account': 'fixture'}, **updates}

    def process(self, argv, **kwargs):
        argv = [str(self.bare) if str(x).startswith('https://github.com/owner/repo.git') else x for x in argv]
        if 'push' in argv:
            self.pushes.append(list(argv))
        return subprocess.run(argv, **kwargs)

    def execute(self, item, responses, store=None):
        model = Model(responses)
        store = store or Store(item)
        result = fr.execute_item(self.cfg, {}, copy.deepcopy(item), store, gh=self.gh,
                                 model_factory=lambda *a: model, runner=self.process)
        return result, model, store

    def fixing(self):
        return [finish(actionable=True, paths=['calc.py']),
                call('edit_file', path='calc.py', old_text='return 2', new_text='return 0'), finish()]

    def test_real_review_fix_independent_review_cycle(self):
        review = self.item()
        result, _, _ = self.execute(review, [finish(findings=[{'title': 'Wrong result', 'body': 'Return zero for the documented case.', 'path': 'calc.py', 'line': 2}])])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['findings']), 1)
        finding = result['findings'][0]
        fix = self.item('fix_comment', slot=2, **{k: v for k, v in finding.items() if k != 'id'})
        fixed, _, _ = self.execute(fix, self.fixing())
        self.assertEqual(fixed['status'], 'done', fixed)
        pr = fixed['fix_pr']
        self.assertEqual(pr['target_ref'], 'feature')
        self.assertEqual(git(self.bare, 'show', pr['head_sha'] + ':calc.py'), 'def value():\n    return 0')
        self.assertEqual(git(self.bare, 'show', pr['head_sha'] + ':other.py'), 'untouched = True')
        self.assertEqual(len(self.pushes), 1)
        second = self.item(number=pr['number'], head_sha=pr['head_sha'], slot=3, author_slot=2, correction_pr=pr)
        reviewed, _, _ = self.execute(second, [finish(findings=[])])
        self.assertEqual(reviewed['status'], 'done', reviewed)
        self.assertEqual(reviewed['reviewer_slot'], 3)
        self.assertFalse(reviewed['ready'])
        self.assertTrue(Path(reviewed['report_path']).is_file())

    def test_same_author_slot_cannot_review(self):
        result, model, _ = self.execute(self.item(author_slot=1), [])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(model.messages, [])
        self.assertEqual(self.gh.writes, [])

    def test_identity_change_blocks_before_model(self):
        self.gh.login = 99
        result, model, _ = self.execute(self.item(), [])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(model.messages)

    def test_changed_head_blocks_before_model(self):
        result, model, _ = self.execute(self.item(head_sha=self.base), [])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(model.messages)

    def test_model_cannot_execute_shell_or_edit_unrelated_file(self):
        model = Model([call('run_command', command='gh issue create'),
                       call('edit_file', path='other.py', old_text='True', new_text='False'), finish()])
        agent = fr.RestrictedAgent(model, self.source, lambda *a: None)
        agent.run('fix', {}, ['calc.py'])
        self.assertEqual((self.source / 'other.py').read_text(), 'untouched = True\n')
        outputs = [m['content'] for msgs in model.messages for m in msgs if m['role'] == 'tool']
        self.assertTrue(any('cannot run commands' in x for x in outputs))
        self.assertTrue(any('outside this finding' in x for x in outputs))

    def test_secret_and_symlink_paths_excluded(self):
        (self.source / '.env').write_text('EXAMPLE_TOKEN=synthetic-secret-example\n')
        (self.source / 'outside').symlink_to(self.root)
        agent = fr.RestrictedAgent(Model([]), self.source, lambda *a: None)
        for path in ['.env', '.git/config', '../output', 'outside/file']:
            with self.subTest(path=path), self.assertRaises(fr.FleetBlocked):
                agent.dispatch('read_file', {'path': path}, 'review', set())

    def test_no_checks_blocks_without_model_call(self):
        self.cfg.repos.clear()
        result, model, _ = self.execute(self.item('fix_comment'), [])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('verification commands', result['summary'])
        self.assertFalse(model.messages)
        self.assertFalse(self.pushes)

    def test_non_actionable_comment_does_not_edit_or_post(self):
        result, _, _ = self.execute(self.item('fix_comment'), [finish(actionable=False)])
        self.assertEqual(result['status'], 'dismissed', result)
        self.assertFalse(self.pushes)
        self.assertFalse(self.gh.writes)

    def test_scope_expansion_rejected_before_edit(self):
        result, _, _ = self.execute(self.item('fix_comment'), [finish(actionable=True, paths=['calc.py', 'other.py'])])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('expanded', result['summary'])
        self.assertFalse(self.pushes)

    def test_failed_checks_hold_push(self):
        self.cfg.repos[0].checks = ['python3 -c "raise SystemExit(1)"']
        result, _, _ = self.execute(self.item('fix_comment'), self.fixing())
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('verification failed', result['summary'])
        self.assertEqual(result['checks'][0]['exit'], 1)
        self.assertFalse(self.pushes)
        self.assertFalse(self.gh.writes)

    def test_automation_preflight_holds_comments_and_push(self):
        with patch.object(fr.triggers, 'preflight', return_value=TriggerReport('NEEDS_REVIEW', ['comment workflow could run'])):
            result, _, _ = self.execute(self.item(), [finish(findings=[])])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(self.gh.writes)

    def test_stop_at_atomic_intent_admission_prevents_comment(self):
        item = self.item()
        store = Store(item)
        store.stop_on_intent = True
        result, _, _ = self.execute(item, [finish(findings=[])], store)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(self.gh.writes)

    def test_lost_comment_response_reconciles_without_duplicate_or_model(self):
        item = self.item()
        self.gh.raise_after_post = True
        result, _, store = self.execute(item, [finish(findings=[])])
        self.assertEqual(result['status'], 'retrying')
        self.assertEqual(len(self.gh.writes), 1)
        result, model, _ = self.execute(store.item, [], store)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(self.gh.writes), 1)
        self.assertFalse(model.messages)

    def test_push_crash_reuses_exact_commit_without_model_or_second_push(self):
        item = self.item('fix_comment')
        store = Store(item)
        store.crash_receipt = 'push'
        first, _, store = self.execute(item, self.fixing(), store)
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(len(self.pushes), 1)
        prepared = next(op['payload']['prepared_commit_sha'] for op in store.item['mutation_intents'] if op['kind'] == 'push')
        second, model, _ = self.execute(store.item, [], store)
        self.assertEqual(second['status'], 'done', second)
        self.assertEqual(second['fix_pr']['head_sha'], prepared)
        self.assertFalse(model.messages)
        self.assertEqual(len(self.pushes), 1)

    def test_fix_on_correction_updates_same_branch_without_nested_pr(self):
        first, _, _ = self.execute(self.item('fix_comment'), self.fixing())
        self.assertEqual(first['status'], 'done', first)
        pr = first['fix_pr']
        comments_before = len([w for w in self.gh.writes if w[0].endswith('/pulls')])
        item = self.item('fix_comment', number=pr['number'], head_sha=pr['head_sha'], correction_pr=pr, slot=2)
        result, _, _ = self.execute(item, [finish(actionable=True, paths=['calc.py']),
            call('edit_file', path='calc.py', old_text='return 0', new_text='return -1'), finish()])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['fix_pr']['number'], pr['number'])
        self.assertEqual(result['fix_pr']['repair_round'], 1)
        self.assertEqual(len([w for w in self.gh.writes if w[0].endswith('/pulls')]), comments_before)

    def test_three_correction_rounds_require_review(self):
        correction = {'repo': 'owner/repo', 'branch': 'feature', 'number': 1, 'repair_round': 3}
        result, model, _ = self.execute(self.item('fix_comment', correction_pr=correction), [])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('three correction rounds', result['summary'])
        self.assertFalse(model.messages)

    def test_deepseek_reasoning_content_preserved_in_tool_history(self):
        response = call('read_file', path='calc.py')
        response['reasoning_content'] = 'Inspect the exact source before deciding.'
        model = Model([response, finish(findings=[])])
        fr.RestrictedAgent(model, self.source, lambda *a: None).run('review', {})
        self.assertEqual(model.messages[1][2]['reasoning_content'], response['reasoning_content'])

    def test_background_review_posts_issue_with_exact_file_provenance(self):
        item = self.item('review_code', number=0, head_sha=self.base, source_ref='main')
        result, _, _ = self.execute(item, [finish(findings=[{'title': 'Wrong base value', 'body': 'Return zero in this case.', 'path': 'calc.py', 'line': 2}])])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['findings'][0]['target_ref'], 'main')
        self.assertEqual(self.gh.writes[0][0], 'repos/owner/repo/issues')

    def test_pushed_feature_branch_becomes_a_draft_pull_request(self):
        item = self.item('open_pr', number=0, head_sha=self.head, source_ref='feature',
                         base_ref='main', repo_id=51)
        result, _, _ = self.execute(item, [])
        self.assertEqual(result['status'], 'done', result)
        path, body = self.gh.writes[0]
        self.assertEqual(path, 'repos/owner/repo/pulls')
        self.assertTrue(body['draft'])
        self.assertEqual((body['head'], body['base']), ('feature', 'main'))

    def test_dry_run_review_records_the_write_and_sends_nothing(self):
        item = self.item()
        item['fleet'] = {**item['fleet'], 'dry_run': True}
        result, _, _ = self.execute(item, [finish(findings=[
            {'title': 'Wrong result', 'body': 'Return zero for the documented case.',
             'path': 'calc.py', 'line': 2}])])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.gh.writes, [])
        actions = result['dry_run_actions']
        self.assertEqual(actions[0]['kind'], 'comment')
        self.assertIn('Return zero', actions[0]['body']['body'])

    def test_dry_run_promotion_does_not_create_a_pull_request(self):
        item = self.item('open_pr', number=0, head_sha=self.head,
                         source_ref='feature', base_ref='main')
        item['fleet'] = {**item['fleet'], 'dry_run': True}
        result, _, _ = self.execute(item, [])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.gh.writes, [])
        self.assertIn('dry run', result['summary'].lower())

    def test_dry_run_fix_records_the_push_and_sends_nothing(self):
        item = self.item('fix_comment', thread_id='PRT_fixture', comment_id='inline:9',
                         head_sha=self.head, source_ref='feature')
        item['fleet'] = {**item['fleet'], 'dry_run': True}
        result, _, _ = self.execute(item, self.fixing())
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.pushes, [])
        self.assertEqual(result['dry_run_actions'][0]['kind'], 'push')
        self.assertEqual(self.gh.writes, [])

    def merged_fix(self):
        item = self.item('fix_comment', thread_id='PRT_fixture', comment_id='inline:9')
        result, _, store = self.execute(item, self.fixing())
        self.assertEqual(result['status'], 'done', result)
        correction = result['fix_pr']
        git(self.bare, 'update-ref', 'refs/heads/feature', correction['head_sha'])
        self.gh.prs[correction['number']].update(merged=True, merged_at='2026-10-25T12:00:00Z',
                                               merge_commit_sha=correction['head_sha'], state='closed')
        final = {**store.item, 'lease': 'finalize-lease', 'phase': 'resolve_thread', 'fix_pr': correction,
                 'merge_proof': {'repo': 'owner/repo', 'number': correction['number']}}
        return final, correction

    def test_confirmed_merged_correction_resolves_original_thread_without_model(self):
        item, correction = self.merged_fix()
        result, model, _ = self.execute(item, [])
        self.assertEqual(result['status'], 'done', result)
        self.assertTrue(result['resolved'])
        self.assertFalse(model.messages)
        self.assertTrue(self.gh.thread['isResolved'])

    def test_closed_unmerged_correction_never_resolves_thread(self):
        item, correction = self.merged_fix()
        self.gh.prs[correction['number']]['merged'] = False
        result, _, _ = self.execute(item, [])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(self.gh.thread['isResolved'])

    def test_new_feedback_prevents_premature_resolution(self):
        item, correction = self.merged_fix()
        self.gh.thread['comments']['nodes'][0]['databaseId'] = 10
        result, _, _ = self.execute(item, [])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('Newer feedback', result['summary'])
        self.assertFalse(self.gh.thread['isResolved'])

    def test_merged_background_finding_closes_only_its_own_issue(self):
        item, correction = self.merged_fix()
        item.pop('thread_id')
        item.update(posted_issue_number=50, marker='<!-- fixture finding -->')
        self.gh.issues.append({'id': 505, 'number': 50, 'state': 'open', 'body': item['marker'], 'user': {'id': 42}})
        result, _, _ = self.execute(item, [])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.gh.issues[0]['state'], 'closed')

    def test_review_records_actual_checks_and_direct_review_link(self):
        result, _, _ = self.execute(self.item(), [finish(findings=[])])
        self.assertEqual(result['verified_head_sha'], self.head)
        self.assertTrue(result['checks'][0]['passed'])
        self.assertTrue(result['review_url'].startswith('https://github.com/'))

    def test_github_checks_verify_review_without_inventing_local_success(self):
        self.cfg.repos.clear()
        def response(state, head):
            return {'repository': {'pullRequest': {'headRefOid': head, 'commits': {'nodes': [
                {'commit': {'oid': head, 'statusCheckRollup': {'state': state}}}]}}}}
        with patch.object(self.gh, 'graphql', return_value=response('SUCCESS', self.head)):
            result, _, _ = self.execute(self.item(), [finish(findings=[])])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['verified_head_sha'], self.head)
        self.assertEqual(result['verification_source'], 'GitHub')
        self.assertEqual(result['checks'], [])
        for state, head in [('PENDING', self.head), ('SUCCESS', self.base)]:
            with self.subTest(state=state, head=head), patch.object(self.gh, 'graphql', return_value=response(state, head)):
                result, _, _ = self.execute(self.item(), [finish(findings=[])])
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['next_action'], 'configure_checks')

    def test_existing_finding_is_reused_without_second_remote_post(self):
        finding = {'title': 'Wrong result', 'body': 'Return zero for this case.', 'path': 'calc.py', 'line': 2}
        first, _, _ = self.execute(self.item(), [finish(findings=[finding])])
        existing = {**first['findings'][0], 'kind': 'fix_comment', 'status': 'queued'}
        item = self.item()
        store = Store(item)
        store.internal = lambda: {'items': {item['id']: copy.deepcopy(item), 'existing': existing}}
        second, _, _ = self.execute(item, [finish(findings=[finding])], store)
        self.assertEqual(second['status'], 'done', second)
        self.assertEqual(len(self.gh.writes), 1)
        self.assertEqual(second['findings'][0]['fingerprint'], existing['fingerprint'])

    def test_requested_new_test_and_deletion_stay_in_declared_scope(self):
        item = self.item('fix_comment', body='Fix calc.py, add a regression test, and remove obsolete other.py.')
        responses = [finish(actionable=True, paths=['calc.py', 'tests/test_calc.py', 'other.py']),
                     call('edit_file', path='calc.py', old_text='return 2', new_text='return 0'),
                     call('create_file', path='tests/test_calc.py', content='from calc import value\nassert value() == 0\n'),
                     call('delete_file', path='other.py'), finish()]
        result, _, _ = self.execute(item, responses)
        self.assertEqual(result['status'], 'done', result)
        sha = result['fix_pr']['head_sha']
        self.assertIn('assert value() == 0', git(self.bare, 'show', sha + ':tests/test_calc.py'))
        self.assertNotIn('other.py', git(self.bare, 'ls-tree', '--name-only', sha))

    def test_provider_rate_limit_keeps_upstream_delay_and_does_not_switch(self):
        from ghagent.model_client import ModelError
        error = ModelError('HTTP 429')
        error.http_status, error.retry_after = 429, 321
        with patch.object(Model, 'chat', side_effect=error):
            result, _, _ = self.execute(self.item(provider='deepseek'), [])
        self.assertEqual(result['status'], 'retrying')
        self.assertEqual(result['retry_kind'], 'rate_limit')
        self.assertEqual(result['retry_after'], 321)
        self.assertEqual(result['retry_scope'], 'provider')
        self.assertFalse(self.gh.writes)

    def test_per_item_call_limit_is_not_treated_as_provider_outage(self):
        from ghagent.model_client import SpendLimitExceeded
        with patch.object(Model, 'chat', side_effect=SpendLimitExceeded('Item limit reached')):
            result, _, _ = self.execute(self.item(), [])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['auto_recheck'])

    def test_fork_write_permission_blocks_without_silent_default_branch(self):
        source = self.gh.repo('other/fork')
        source.update(id=52, permissions={'push': False})
        self.gh.prs[1]['head']['repo'] = source
        original = self.gh.rest
        def api(method, path, **kwargs):
            if path == 'repos/other/fork':
                return source
            if path.startswith('repos/other/fork/git/ref/'):
                return {'object': {'sha': self.head}}
            return original(method, path, **kwargs)
        def process(argv, **kwargs):
            argv = [str(self.bare) if x == 'https://github.com/other/fork.git' else x for x in argv]
            return self.process(argv, **kwargs)
        item = self.item('fix_comment')
        with patch.object(self.gh, 'rest', api):
            result = fr.execute_item(self.cfg, {}, item, Store(item), gh=self.gh,
                                     model_factory=lambda *a: Model([]), runner=process)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('cannot publish', result['summary'])
        self.assertFalse(self.pushes)

    def test_failed_review_checks_remain_visible_and_can_retry_without_model(self):
        self.cfg.repos[0].checks = ['python3 -c "raise SystemExit(1)"']
        item = self.item()
        first, _, store = self.execute(item, [finish(findings=[])])
        self.assertEqual(first['status'], 'blocked')
        self.assertFalse(first['checks'][0]['passed'])
        self.assertTrue(first['review_url'])
        self.cfg.repos[0].checks = ['python3 -c "raise SystemExit(0)"']
        store.item['lease'] = 'retry-new-lease'
        second, model, _ = self.execute(store.item, [], store)
        self.assertEqual(second['status'], 'done', second)
        self.assertFalse(model.messages)
        self.assertEqual(len(self.gh.writes), 1)

    @unittest.skipUnless(sandbox_available(), 'Isolated Linux verification uses bubblewrap')
    def test_real_isolated_check_helper_runs_the_final_candidate(self):
        from ghagent.fleet_checks import run_repository_checks
        with patch.object(fr, '_run_repository_checks', run_repository_checks):
            result, _, _ = self.execute(self.item('fix_comment'), self.fixing())
        self.assertEqual(result['status'], 'done', result)
        self.assertTrue(result['checks'][0]['isolated'])


if __name__ == '__main__':
    unittest.main()
