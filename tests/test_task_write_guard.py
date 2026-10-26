"""Repository writes must pass real checks and automation inspection first."""
from __future__ import annotations

import tempfile
import unittest
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from ghagent import triggers
from ghagent.interfaces import Config, Job, JobKind, RepoConfig
from ghagent.tasks import sync_branch, write_docs
from ghagent.task_write_guard import PreWriteRefusal


class InspectionGh:
    """Read-only GitHub boundary with normal absent workflows and rulesets."""
    def __init__(self, inaccessible=False, rulesets_unknown=False, workflow=None):
        self.inaccessible = inaccessible
        self.rulesets_unknown = rulesets_unknown
        self.workflow = workflow
        self.calls = []

    def rest(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method != 'GET':
            raise AssertionError('Unexpected external mutation')
        if self.inaccessible:
            raise ValueError('403 metadata inaccessible')
        if path == 'repos/owner/repo':
            return {'default_branch': 'main'}
        if path == 'repos/owner/repo/rulesets':
            if self.rulesets_unknown:
                raise ValueError('403 plan-limited rulesets')
            return []
        decoded = unquote(path)
        if self.workflow and '/contents/.github/workflows' in decoded:
            if '/contents/.github/workflows?' in decoded:
                return [{'type': 'file', 'name': 'ci.yml',
                         'path': '.github/workflows/ci.yml', 'size': len(self.workflow)}]
            return {'content': self.workflow, 'encoding': 'utf-8'}
        raise ValueError('404 Not Found')


class RealRepositoryTests(unittest.TestCase):
    """Checks read real candidate files; every remote is a temporary local bare repo."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Every test keeps PR creation fake even when an expected guard fails.
        pr_boundary = patch.object(write_docs, '_open_pull_request',
                                   return_value={'result': 'created'})
        pr_boundary.start()
        self.addCleanup(pr_boundary.stop)
        self.root = Path(tmp.name)
        self.remote = self.root / 'origin.git'
        self.seed = self.root / 'seed'
        self.checkout = self.root / 'checkout'
        self.seed.mkdir()
        self.git('init', '--bare', str(self.remote), cwd=self.root)
        self.git('init', '-b', 'main', cwd=self.seed)
        self.git('config', 'user.name', 'Fixture', cwd=self.seed)
        self.git('config', 'user.email', 'fixture@example.com', cwd=self.seed)
        (self.seed / 'README.md').write_text('Initial documentation\n')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'initial', cwd=self.seed)
        self.git('remote', 'add', 'origin', str(self.remote), cwd=self.seed)
        self.git('push', 'origin', 'main', cwd=self.seed)
        self.git('checkout', '-b', 'develop', cwd=self.seed)
        (self.seed / 'source.txt').write_text('from source\n')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'source work', cwd=self.seed)
        self.git('push', 'origin', 'develop', cwd=self.seed)
        self.git('checkout', 'main', cwd=self.seed)
        (self.seed / 'target.txt').write_text('from target\n')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'target work', cwd=self.seed)
        self.git('push', 'origin', 'main', cwd=self.seed)
        self.git('clone', '-b', 'main', str(self.remote), str(self.checkout), cwd=self.root)
        self.git('config', 'user.name', 'Fixture', cwd=self.checkout)
        self.git('config', 'user.email', 'fixture@example.com', cwd=self.checkout)
        self.repo_cfg = RepoConfig('owner', 'repo')
        self.cfg = Config(repos=[self.repo_cfg])
        self.gh = InspectionGh()

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=cwd or self.checkout,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def command(self, code):
        return shlex.join([sys.executable, '-c', code])

    def job(self, module, accept=False, **notes):
        return Job('job_guard', JobKind(module.TASK.id), 'owner/repo', 0, 0, 1,
                   notes={'request': {'accept_no_checks': accept}, **notes})

    def context(self, **over):
        result = {'repo': 'owner/repo', 'config': self.cfg,
                  'worktree': str(self.checkout),
                  'values': {'source': 'develop', 'target': 'main'},
                  'draft': {'title': 'Document setup', 'body': 'Installation steps'}}
        result.update(over)
        return result

    def prepare_docs(self):
        (self.checkout / 'README.md').write_text('Reviewed installation steps\n')

    def branches(self):
        return self.git('show-ref', cwd=self.remote)

    def test_sync_checks_the_exact_merged_result_then_pushes_non_forced(self):
        self.repo_cfg.checks = [self.command(
            "from pathlib import Path; "
            "assert Path('source.txt').read_text() == 'from source\\n'; "
            "assert Path('target.txt').read_text() == 'from target\\n'")]
        job = self.job(sync_branch)
        original = sync_branch._run_git
        calls = []
        def record(args, cwd, **kwargs):
            calls.append(list(args))
            return original(args, cwd=cwd, **kwargs)
        with patch.object(sync_branch, '_run_git', record):
            sync_branch.apply(self.context(), self.gh, job)
        pushed = [args for args in calls if args[0] == 'push']
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(pushed, [['push', 'origin', head + ':refs/heads/main']])
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.remote), head)
        self.assertEqual(len(self.git('show', '-s', '--format=%P', head).split()), 2)
        self.assertEqual(job.notes['checks'][0]['passed'], True)
        self.assertIn('passed', job.notes['verification'])
        self.assertEqual(job.notes['tested_tree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertTrue(all(method == 'GET' for method, _ in self.gh.calls))

    def test_docs_checks_actual_staged_files_before_commit_push_or_pr(self):
        self.prepare_docs()
        self.repo_cfg.checks = [self.command(
            "from pathlib import Path; assert Path('README.md').read_text() == "
            "'Reviewed installation steps\\n'")]
        job = self.job(write_docs)
        with patch.object(write_docs, '_open_pull_request', return_value={'result': 'created'}) as pr:
            self.assertEqual(write_docs.apply(self.context(), self.gh, job), {'result': 'created'})
        self.assertEqual(self.git('show', 'gha/docs-guard:README.md', cwd=self.remote),
                         'Reviewed installation steps')
        pr.assert_called_once_with('owner/repo', 'main', 'gha/docs-guard',
                                   'Document setup', 'Installation steps')
        self.assertTrue(job.notes['checks'][0]['passed'])

    def test_failed_checks_hold_both_remote_branches_and_prs(self):
        self.repo_cfg.checks = [self.command('raise SystemExit(7)')]
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                before = self.branches()
                self.prepare_docs() if module is write_docs else None
                job = self.job(module)
                with patch.object(write_docs, '_open_pull_request') as pr:
                    with self.assertRaisesRegex(PreWriteRefusal, 'verification failed'):
                        module.apply(self.context(), self.gh, job)
                self.assertEqual(self.branches(), before)
                self.assertEqual(job.notes['checks'][0]['exit'], 7)
                pr.assert_not_called()

    def test_unknown_automation_holds_both_tasks_even_after_passing_checks(self):
        self.repo_cfg.checks = [self.command('pass')]
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                before = self.branches()
                self.prepare_docs() if module is write_docs else None
                job = self.job(module)
                with patch.object(write_docs, '_open_pull_request') as pr:
                    with self.assertRaisesRegex(PreWriteRefusal, 'inspection held all writes'):
                        module.apply(self.context(), InspectionGh(inaccessible=True), job)
                self.assertEqual(self.branches(), before)
                self.assertTrue(job.notes['checks'][0]['passed'])
                self.assertEqual(job.notes['trigger_verdict']['verdict'], triggers.REVIEW)
                pr.assert_not_called()

    def test_no_checks_require_saved_one_task_acknowledgement_for_both_tasks(self):
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                before = self.branches()
                with self.assertRaisesRegex(PreWriteRefusal, 'no verification commands'):
                    module.apply(self.context(accept_no_checks=True,
                                              values={'source': 'develop', 'target': 'main',
                                                      'accept_no_checks': True}),
                                 self.gh, self.job(module))
                self.assertEqual(self.branches(), before)
        self.prepare_docs()
        job = self.job(write_docs, accept=True)
        with patch.object(write_docs, '_open_pull_request', return_value={}):
            write_docs.apply(self.context(), self.gh, job)
        self.assertEqual(job.notes['checks'], [])
        self.assertIn('explicitly accepts unverified', job.notes['verification'])

    def test_missing_or_unconfigured_trusted_settings_fail_closed(self):
        for cfg in (None, {'repos': [self.repo_cfg]}, Config()):
            for module in (sync_branch, write_docs):
                with self.subTest(config=type(cfg).__name__, task=module.TASK.id):
                    with self.assertRaises(PreWriteRefusal):
                        module.apply(self.context(config=cfg), self.gh,
                                     self.job(module, accept=True))

    def test_source_advancement_after_plan_is_refused(self):
        (self.seed / 'later.txt').write_text('Not in the reviewed plan\n')
        self.git('checkout', 'develop', cwd=self.seed)
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'later source work', cwd=self.seed)
        self.git('push', 'origin', 'develop', cwd=self.seed)
        before = self.branches()
        with self.assertRaisesRegex(PreWriteRefusal, 'source branch moved'):
            sync_branch.apply(self.context(), self.gh, self.job(sync_branch, accept=True))
        self.assertEqual(self.branches(), before)

    def test_prepared_workflow_cannot_hide_behind_safe_remote_workflows(self):
        self.prepare_docs()
        workflow = self.checkout / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('on: push\njobs: {}\n')
        before = self.branches()
        with patch.object(write_docs, '_open_pull_request') as pr:
            with self.assertRaisesRegex(PreWriteRefusal, 'triggered by push'):
                write_docs.apply(self.context(), self.gh, self.job(write_docs, accept=True))
        self.assertEqual(self.branches(), before)
        pr.assert_not_called()

    def test_sync_inspects_workflows_introduced_in_the_merge(self):
        self.git('checkout', 'develop', cwd=self.seed)
        workflow = self.seed / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('on: push\njobs: {}\n')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'source automation', cwd=self.seed)
        self.git('push', 'origin', 'develop', cwd=self.seed)
        self.git('fetch', 'origin')  # this is the source the plan has inspected
        before = self.branches()
        with self.assertRaisesRegex(PreWriteRefusal, 'triggered by push'):
            sync_branch.apply(self.context(), self.gh, self.job(sync_branch, accept=True))
        self.assertEqual(self.branches(), before)

    def test_source_moving_during_inspection_invalidates_the_merge(self):
        original = self.gh.rest
        moved = False
        def inspect(method, path, **kwargs):
            nonlocal moved
            if not moved:
                moved = True
                self.git('checkout', 'develop', cwd=self.seed)
                (self.seed / 'later.txt').write_text('not reviewed\n')
                self.git('add', '.', cwd=self.seed)
                self.git('commit', '-m', 'advance during inspection', cwd=self.seed)
                self.git('push', 'origin', 'develop', cwd=self.seed)
            return original(method, path, **kwargs)
        target_before = self.git('rev-parse', 'main', cwd=self.remote)
        with patch.object(self.gh, 'rest', inspect):
            with self.assertRaisesRegex(PreWriteRefusal, 'source or target branch moved'):
                sync_branch.apply(self.context(), self.gh, self.job(sync_branch, accept=True))
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.remote), target_before)

    def test_prepared_lfs_attributes_are_held(self):
        self.prepare_docs()
        (self.checkout / '.gitattributes').write_text('*.bin filter=lfs diff=lfs merge=lfs -text\n')
        before = self.branches()
        with self.assertRaisesRegex(PreWriteRefusal, 'Git LFS'):
            write_docs.apply(self.context(), self.gh, self.job(write_docs, accept=True))
        self.assertEqual(self.branches(), before)

    def test_push_errors_are_uncertain_and_not_a_retryable_pre_write_refusal(self):
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                self.prepare_docs() if module is write_docs else None
                original = module._run_git
                def disconnected(args, cwd, **kwargs):
                    if args[0] == 'push':
                        return 128, '', 'connection lost after push started'
                    return original(args, cwd=cwd, **kwargs)
                with patch.object(module, '_run_git', disconnected), \
                        patch.object(write_docs, '_open_pull_request') as pr:
                    with self.assertRaises(ValueError) as caught:
                        module.apply(self.context(), self.gh, self.job(module, accept=True))
                self.assertNotIsInstance(caught.exception, PreWriteRefusal)
                pr.assert_not_called()

    def test_only_trusted_workflow_and_ruleset_policy_can_waive_inspection(self):
        self.prepare_docs()
        gh = InspectionGh(rulesets_unknown=True, workflow='on: push\njobs: {}\n')
        context = self.context(allow_workflows=True, allow_unknown_rulesets=True)
        with self.assertRaises(PreWriteRefusal):
            write_docs.apply(context, gh, self.job(write_docs, accept=True))
        self.repo_cfg.allow_unknown_rulesets = True
        with patch.object(write_docs, '_open_pull_request', return_value={}):
            write_docs.apply(context, gh, self.job(write_docs, accept=True, allow_workflows=True))

    def test_checks_scrub_tokens_and_never_invoke_a_shell(self):
        self.prepare_docs()
        self.repo_cfg.checks = [self.command(
            "import os; assert 'GH_TOKEN' not in os.environ; "
            "assert 'GHA_MODEL_API_KEY' not in os.environ")]
        with patch.dict(os.environ, {'GH_TOKEN': 'dummy', 'GHA_MODEL_API_KEY': 'dummy'}), \
                patch.object(write_docs, '_open_pull_request', return_value={}):
            write_docs.apply(self.context(), self.gh, self.job(write_docs))

    def test_checks_that_change_prepared_files_cannot_publish_untested_changes(self):
        self.prepare_docs()
        self.repo_cfg.checks = [self.command("from pathlib import Path; Path('README.md').write_text('changed by check')")]
        before = self.branches()
        with self.assertRaisesRegex(PreWriteRefusal, 'staged files and the checkout'):
            write_docs.apply(self.context(), self.gh, self.job(write_docs))
        self.assertEqual(self.branches(), before)

    def test_stop_during_real_check_skips_remaining_checks_and_all_writes(self):
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                stop = self.checkout / (module.TASK.id + '-stop')
                later = self.checkout / (module.TASK.id + '-later-check')
                self.repo_cfg.checks = [
                    self.command(f'from pathlib import Path; Path({str(stop)!r}).touch()'),
                    self.command(f'from pathlib import Path; Path({str(later)!r}).touch()'),
                ]
                self.prepare_docs() if module is write_docs else None
                before = self.branches()
                job = self.job(module)
                with patch.object(write_docs, '_open_pull_request') as pr:
                    with self.assertRaisesRegex(PreWriteRefusal, 'stopped'):
                        module.apply(self.context(stop_check=stop.exists), self.gh, job)
                self.assertTrue(stop.exists())
                self.assertFalse(later.exists())
                self.assertEqual(self.branches(), before)
                self.assertEqual(len(job.notes['checks']), 1)
                self.assertTrue(job.notes['checks'][0]['passed'])
                pr.assert_not_called()

    def test_stop_arriving_during_preflight_holds_both_task_pushes(self):
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                stopped = False
                original = self.gh.rest
                def inspect(method, path, **kwargs):
                    nonlocal stopped
                    stopped = True
                    return original(method, path, **kwargs)
                self.prepare_docs() if module is write_docs else None
                before = self.branches()
                with patch.object(self.gh, 'rest', inspect), \
                        patch.object(write_docs, '_open_pull_request') as pr:
                    with self.assertRaisesRegex(PreWriteRefusal, 'stopped'):
                        module.apply(self.context(stop_check=lambda: stopped), self.gh,
                                     self.job(module, accept=True))
                self.assertEqual(self.branches(), before)
                pr.assert_not_called()

    def test_stop_after_final_tree_check_is_rechecked_immediately_before_push(self):
        for module in (sync_branch, write_docs):
            with self.subTest(task=module.TASK.id):
                stopped = False
                original = module.ensure_prepared_tree
                def prepared(*args, **kwargs):
                    nonlocal stopped
                    result = original(*args, **kwargs)
                    stopped = True
                    return result
                self.prepare_docs() if module is write_docs else None
                before = self.branches()
                with patch.object(module, 'ensure_prepared_tree', prepared), \
                        patch.object(write_docs, '_open_pull_request') as pr:
                    with self.assertRaisesRegex(PreWriteRefusal, 'stopped'):
                        module.apply(self.context(stop_check=lambda: stopped), self.gh,
                                     self.job(module, accept=True))
                self.assertEqual(self.branches(), before)
                pr.assert_not_called()

    def test_initial_stop_or_unreadable_stop_status_fails_closed(self):
        def unreadable():
            raise OSError('stop state unreadable')
        for stop_check in (lambda: True, unreadable, 'invalid callback'):
            for module in (sync_branch, write_docs):
                with self.subTest(stop=type(stop_check).__name__, task=module.TASK.id):
                    with patch.object(module, '_run_git', side_effect=AssertionError(
                            'Stop must be checked before reaching git')) as git, \
                            patch.object(write_docs, '_open_pull_request'):
                        with self.assertRaises(PreWriteRefusal):
                            module.apply(self.context(stop_check=stop_check), self.gh,
                                         self.job(module, accept=True))
                    git.assert_not_called()

    def test_stop_after_docs_push_retains_partial_write_and_skips_pr(self):
        self.prepare_docs()
        stopped = False
        original = write_docs._run_git
        def pushed(args, cwd, **kwargs):
            nonlocal stopped
            result = original(args, cwd=cwd, **kwargs)
            if args[0] == 'push':
                stopped = True
            return result
        with patch.object(write_docs, '_run_git', pushed), \
                patch.object(write_docs, '_open_pull_request') as pr:
            with self.assertRaisesRegex(ValueError, 'stopped') as caught:
                write_docs.apply(self.context(stop_check=lambda: stopped), self.gh,
                                 self.job(write_docs, accept=True))
        self.assertNotIsInstance(caught.exception, PreWriteRefusal)
        self.assertEqual(self.git('show', 'gha/docs-guard:README.md', cwd=self.remote),
                         'Reviewed installation steps')
        pr.assert_not_called()


class GuardRegressionTests(unittest.TestCase):
    def test_both_task_pushes_are_held_by_automation_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            for module, kind in ((sync_branch, JobKind.SYNC_BRANCH),
                                 (write_docs, JobKind.WRITE_DOCS)):
                with self.subTest(task=kind.value):
                    job = Job('job_guard', kind, 'owner/repo', 0, 0, 1,
                              notes={'request': {'accept_no_checks': True}})
                    calls = []
                    def git(args, cwd, **kwargs):
                        calls.append(list(args))
                        if args[:3] == ['diff', '--cached', '--name-only']:
                            return 0, 'README.md\n', ''
                        if args[0] in {'rev-parse', 'write-tree'}:
                            return 0, 'a' * 40 + '\n', ''
                        if args[0] == 'ls-remote':
                            return 0, ''.join('a' * 40 + '\t' + name + '\n'
                                              for name in args[2:]), ''
                        return 0, '', ''
                    context = {'repo': 'owner/repo', 'worktree': root,
                               'config': Config(repos=[RepoConfig('owner', 'repo')]),
                               'values': {'source': 'develop', 'target': 'main'},
                               'draft': {'title': 'Document setup', 'body': 'Steps'}}
                    held = triggers.TriggerReport(triggers.REVIEW, ['unsafe automation'])
                    with patch.object(module, '_run_git', git), \
                            patch.object(triggers, 'preflight', return_value=held) as inspect, \
                            patch.object(write_docs, '_open_pull_request'):
                        with self.assertRaisesRegex(ValueError, 'unsafe automation'):
                            module.apply(context, object(), job)
                    self.assertTrue(inspect.called)
                    self.assertFalse([args for args in calls if args[0] == 'push'])


if __name__ == '__main__':
    unittest.main()
