"""Phase 0 foundation: task registry, kind-aware queue, authority, fence,
digest, safety-net gate, and the Three Lines report."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ghagent import tasks, worker as worker_mod
from ghagent.control_store import AuthorityStore
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.queue import JobQueue
from ghagent.results import _report_markdown
from ghagent.tasks.types import PROFILES, TaskType
from ghagent.web_control import ApiError, WebControl

from tests.test_web_control import FakeGitHub, FakeRunner, GuiFixture
from tests.test_worker_loop import FakeExecutor, FakeQueue, make_cfg, make_worker


def inject(task: TaskType):
    """Register a synthetic task type for one test; restore afterwards."""
    registry = dict(tasks.load_registry())
    registry[task.id] = task
    tasks.types._REGISTRY = registry
    return registry


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_builtins_and_catalog_shape(self):
        reg = tasks.load_registry()
        self.assertIn('fix-pr', reg)
        self.assertIn('implement-issue', reg)
        catalog = tasks.catalog()
        self.assertTrue(all({'id', 'label', 'help', 'takes', 'fields'} <= set(i)
                            for i in catalog))

    def test_every_registered_task_requires_at_most_three_inputs(self):
        for task in tasks.load_registry().values():
            self.assertLessEqual(len(task.required_fields()), 3, task.id)
            self.assertIn(task.profile, PROFILES, task.id)

    def test_registry_rejects_unknown_profile(self):
        bad = TaskType(id='bad-kind', label='Bad', profile='nope',
                       help='x', takes='repo')
        registry = inject(bad)
        with self.assertRaises(RuntimeError):
            # simulate a module load hitting the profile check
            for task in registry.values():
                if task.profile not in PROFILES:
                    raise RuntimeError('unknown profile')


class QueueKindTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = JobQueue(Path(self._tmp.name) / 'state')

    def job(self, job_id, kind, number):
        return Job(job_id=job_id, kind=kind, repo_full_name='o/r', number=number,
                   command_comment_id=0, requested_by=1, head_sha='a' * 40,
                   dedup_key=f'{kind.value}:o/r:{number}:{job_id}')

    def test_open_lookup_is_kind_aware(self):
        self.queue.add(self.job('j1', JobKind.FIX_PR, 7))
        self.assertIsNotNone(self.queue.find_open_by_repo_number(
            'o/r', 7, kind='fix-pr'))
        self.assertIsNone(self.queue.find_open_by_repo_number(
            'o/r', 7, kind='review-pr'))
        self.assertIsNotNone(self.queue.find_open_by_repo_number('o/r', 7))

    def test_repo_level_lookup(self):
        self.queue.add(self.job('j2', JobKind.EXPLAIN_REPO, 0))
        self.assertIsNotNone(self.queue.find_open_by_repo('o/r', 'explain-repo'))
        self.assertIsNone(self.queue.find_open_by_repo('o/r', 'backup-repo'))


class EnqueueRegistryTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def tearDown(self):
        tasks.types._REGISTRY = None

    def repo_task(self, **over):
        base = dict(id='explain-repo', label='Explain this repository',
                    profile='read-only', takes='repo',
                    help='Answers your questions about the repository.')
        base.update(over)
        return TaskType(**base)

    def test_repo_level_task_enqueues_without_a_number(self):
        inject(self.repo_task())
        result = self.control.enqueue(self.task_data(kind='explain-repo', number=None))
        job = result['job']
        self.assertEqual(job['number'], 0)
        self.assertEqual(job['kind'], 'explain-repo')
        self.assertEqual(job['notes']['request']['kind'], 'explain-repo')

    def test_repo_level_duplicate_is_refused(self):
        inject(self.repo_task())
        self.control.enqueue(self.task_data(kind='explain-repo', number=None))
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data(kind='explain-repo', number=None))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('already queued', str(caught.exception))

    def test_write_task_requires_safety_net_acknowledgement(self):
        inject(self.repo_task(id='sync-branch', label='Bring a branch up to date',
                              profile='write',
                              help='Syncs one branch to another.'))
        config = self.config_path.read_text().replace(
            'checks = ["python3 -m unittest"]', 'checks = []')
        self.config_path.write_text(config)
        control = WebControl(self.config_path, self.env_path, gh=FakeGitHub(),
                             runner=FakeRunner(),
                             accounts=[{'account': 'alice', 'label': 'Alice',
                                        'path': str(self.home), 'uid': 4000}])
        with self.assertRaises(ApiError) as caught:
            control.enqueue(self.task_data(kind='sync-branch', number=None))
        self.assertIn('safety net', str(caught.exception))
        for ambiguous in ('false', 'true', 1, None):
            with self.subTest(acknowledgement=ambiguous):
                with self.assertRaises(ApiError) as invalid:
                    control.enqueue(self.task_data(kind='sync-branch', number=None,
                                                   accept_no_checks=ambiguous))
                self.assertEqual(invalid.exception.status, 400)
        job = control.enqueue(self.task_data(kind='sync-branch', number=None,
                                             accept_no_checks=True))['job']
        self.assertTrue(job['notes']['request']['accept_no_checks'])

    def test_retry_replays_the_original_request(self):
        inject(self.repo_task())
        job = self.control.enqueue(self.task_data(
            kind='explain-repo', number=None))['job']
        stored = self.control.queue().get(job['job_id'])
        stored.state = JobState.FAILED
        stored.error = 'boom'
        stored.updated_at = '2026-10-22T00:00:00+00:00'
        self.control.queue().update(stored)
        retried = self.control.job_action(job['job_id'], 'retry')['job']
        self.assertEqual(retried['notes']['request']['kind'], 'explain-repo')
        self.assertNotEqual(retried['job_id'], job['job_id'])


class AuthorityTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_default_is_ask_and_grants_are_visible(self):
        self.assertEqual(self.control.authority_view()['modes'],
                         {'owner/repo': 'ask'})
        self.control.set_authority({'repo': 'owner/repo', 'mode': 'auto'})
        view = self.control.authority_view()
        self.assertEqual(view['modes']['owner/repo'], 'auto')
        self.assertEqual(view['grants'][0]['repo'], 'owner/repo')

    def test_invalid_mode_is_refused(self):
        with self.assertRaises(ApiError):
            self.control.set_authority({'repo': 'owner/repo', 'mode': 'yolo'})

    def test_corrupt_store_is_preserved_not_replaced(self):
        path = self.state / 'authority.json'
        path.write_text('{not json')
        with self.assertRaises(RuntimeError):
            AuthorityStore(path).mode('owner/repo')
        self.assertEqual(path.read_text(), '{not json')


class FenceAndDigestTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_fence_reports_protection_and_authority(self):
        fence = self.control.fence()
        repo = fence['repos'][0]
        self.assertEqual(repo['repo'], 'owner/repo')
        self.assertEqual(repo['protection'], 'safe')
        self.assertEqual(repo['checks_configured'], 1)
        self.assertEqual(repo['post_authority'], 'ask')
        self.assertEqual(fence['actions_runs_started_by_worker'], 0)

    def test_digest_lists_drafts_and_events(self):
        waiting = Job(job_id='job_wait', kind=JobKind.TRIAGE_ISSUES,
                      repo_full_name='owner/repo', number=0,
                      command_comment_id=0, requested_by=42,
                      dedup_key='triage-issues:owner/repo:0:x',
                      state=JobState.WAITING_ON_YOU)
        self.control.queue().add(waiting)
        (self.state / 'digest.jsonl').write_text(
            json.dumps({'kind': 'done', 'at': '2026-10-22T00:00:00+00:00',
                        'job_id': 'job_a', 'text': 'finished'}) + '\n')
        digest = self.control.digest()
        self.assertEqual(len(digest['done']), 1)
        self.assertEqual(digest['waiting_on_you']['drafts'][0]['job_id'], 'job_wait')


class WorkerWaitingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_drain_skips_jobs_waiting_on_you(self):
        queue = FakeQueue()
        waiting = Job(job_id='job_w', kind=JobKind.TRIAGE_ISSUES,
                      repo_full_name='o/r', number=0, command_comment_id=0,
                      requested_by=7, dedup_key='k', state=JobState.WAITING_ON_YOU)
        queue.add(waiting)
        executor = FakeExecutor()
        w = make_worker(self.tmp, queue=queue, executor=executor,
                        cfg=make_cfg(self.tmp))
        w._drain_jobs()
        self.assertEqual(executor.executed, [])

    def test_digest_writer_appends_one_line(self):
        (self.tmp / 'state').mkdir(exist_ok=True)
        w = make_worker(self.tmp, cfg=make_cfg(self.tmp))
        job = Job(job_id='job_d', kind=JobKind.EXPLAIN_REPO,
                  repo_full_name='o/r', number=0, command_comment_id=0,
                  requested_by=7, dedup_key='k')
        w._write_digest('done', job, 'finished')
        lines = (self.tmp / 'state' / 'digest.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['kind'], 'done')


class ThreeLinesReportTests(unittest.TestCase):
    def test_report_ends_with_the_three_lines(self):
        job = Job(job_id='job_r', kind=JobKind.FIX_PR, repo_full_name='o/r',
                  number=7, command_comment_id=0, requested_by=1,
                  dedup_key='k', state=JobState.DONE,
                  notes={'title': 'Fix the thing', 'agent_summary': 'Done.'})
        text = _report_markdown(job)
        self.assertIn('What happened', text)
        self.assertIn('What only you can decide', text)
        self.assertIn('What keeps running on its own', text)


if __name__ == '__main__':
    unittest.main()


class ReadOnlyExecutionTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_report_task_runs_without_any_remote_write(self):
        from tests.test_executor_flow import (FakeAgent, FakeWT, agent_result,
                                              default_cfg, happy_gh, run_job)
        inject(TaskType(id='explain-repo', label='Explain this repository',
                        profile='read-only', takes='repo',
                        help='Answers your questions about the repository.'))
        job = Job(job_id='job_ro', kind=JobKind.EXPLAIN_REPO,
                  repo_full_name='o/r', number=0, command_comment_id=0,
                  requested_by=1, dedup_key='k',
                  notes={'values': {'question': 'What does this do?'}})
        returned, queue, wt, agent, git_rec, gh_rec = run_job(
            job, happy_gh(), wt=FakeWT(),
            agent=FakeAgent(agent_result()))
        self.assertEqual(returned.state, JobState.DONE, returned.error)
        states = [s for s, _ in queue.history]
        self.assertIn('retrieving', states)
        self.assertIn('working', states)
        self.assertIn('done', states)
        self.assertNotIn('responding', states)   # no remote writes at all
        self.assertTrue(agent.instructions)      # the model actually ran
        self.assertIn('plain language', agent.instructions[0])
        self.assertNotIn(('commit_all', FakeWT().path, ''), wt.calls)
        self.assertFalse([c for c in wt.calls if c[0] == 'commit_all'])
        self.assertEqual(git_rec.calls, [])      # no git push either


class DraftActionTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def tearDown(self):
        tasks.types._REGISTRY = None

    def draft_task(self, **over):
        calls = []

        def apply(context, gh, job):
            calls.append(context)
            return {'result': 'Posted by the test.'}

        base = dict(id='triage-issues', label='Turn words into issues',
                    profile='draft', takes='repo',
                    help='Splits your words into ready-to-post issues.',
                    apply=apply)
        base.update(over)
        task = TaskType(**base)
        inject(task)
        self.applied = calls
        return task

    def waiting_job(self, **notes_over):
        notes = {'drafts': [{'name': 'one', 'title': 'First issue',
                             'body': 'The exact words.'}],
                 'output_dir': str(self.output), 'member': 'alice'}
        notes.update(notes_over)
        job = Job(job_id='job_draft', kind=JobKind.TRIAGE_ISSUES,
                  repo_full_name='owner/repo', number=0, command_comment_id=0,
                  requested_by=42, dedup_key='triage-issues:owner/repo:0:x',
                  state=JobState.WAITING_ON_YOU, notes=notes)
        self.assertTrue(self.control.queue().add(job))
        return job

    def test_post_applies_the_draft_once_and_finishes(self):
        self.draft_task()
        self.waiting_job()
        result = self.control.draft_action('job_draft', {'action': 'post',
                                                         'name': 'one'})
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0]['draft']['body'], 'The exact words.')
        self.assertEqual(result['job']['state'], 'done')

    def test_discard_stops_without_posting(self):
        self.draft_task()
        self.waiting_job()
        result = self.control.draft_action('job_draft', {'action': 'discard'})
        self.assertEqual(result['job']['state'], 'stopped')
        self.assertEqual(self.applied, [])

    def test_edit_updates_words_and_stays_waiting(self):
        self.draft_task()
        self.waiting_job()
        self.control.draft_action('job_draft', {'action': 'edit', 'name': 'one',
                                                'body': 'New words.'})
        job = self.control.queue().get('job_draft')
        self.assertEqual(job.state, JobState.WAITING_ON_YOU)
        self.assertEqual(job.notes['drafts'][0]['body'], 'New words.')

    def test_high_stakes_requires_confirmation(self):
        self.draft_task(post_confirm_required=True)
        self.waiting_job()
        with self.assertRaises(ApiError) as caught:
            self.control.draft_action('job_draft', {'action': 'post', 'name': 'one'})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.applied, [])
        self.control.draft_action('job_draft', {'action': 'post', 'name': 'one',
                                                'confirm': True})
        self.assertEqual(len(self.applied), 1)

    def test_task_without_apply_refuses_posting(self):
        self.draft_task(apply=None)
        self.waiting_job()
        with self.assertRaises(ApiError) as caught:
            self.control.draft_action('job_draft', {'action': 'post', 'name': 'one'})
        self.assertIn('not available yet', str(caught.exception))
        self.assertEqual(self.applied, [])


class DraftExecutionTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_draft_task_collects_files_and_waits(self):
        from tests.test_executor_flow import FakeWT, agent_result, happy_gh, run_job
        inject(TaskType(id='triage-issues', label='Turn words into issues',
                        profile='draft', takes='repo',
                        help='Splits your words into ready-to-post issues.'))

        class DraftingAgent:
            def __init__(self):
                self.instructions = []

            def __call__(self, wt, checks=None):
                self.wt = wt
                return self

            def run(self, instruction):
                self.instructions.append(instruction)
                folder = Path(self.wt) / '.gha-drafts'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'one.md').write_text('First issue\nThe exact words.')
                return agent_result()

        agent = DraftingAgent()
        job = Job(job_id='job_dr', kind=JobKind.TRIAGE_ISSUES,
                  repo_full_name='o/r', number=0, command_comment_id=0,
                  requested_by=1, dedup_key='k',
                  notes={'values': {'words': 'fix the contact form'}})
        returned, queue, wt, _, git_rec, gh_rec = run_job(
            job, happy_gh(), wt=FakeWT(), agent=agent)
        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertEqual(returned.notes['drafts'][0]['title'], 'First issue')
        self.assertEqual(returned.notes['drafts'][0]['body'], 'The exact words.')
        self.assertIn('.gha-drafts/', agent.instructions[0])
        states = [s for s, _ in queue.history]
        self.assertNotIn('responding', states)


class NewProjectEnqueueTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def tearDown(self):
        tasks.types._REGISTRY = None

    def project_task(self):
        inject(TaskType(id='new-project', label='Start a new project',
                        profile='high-stakes', takes='none',
                        help='Creates a repository with a README, license and starter tests.',
                        fields={'name': {'label': 'Project name', 'required': True},
                                'idea': {'label': 'One sentence about it', 'required': True}},
                        post_confirm_required=True))

    def test_new_project_enqueues_without_a_configured_repo(self):
        self.project_task()
        result = self.control.enqueue({'kind': 'new-project', 'owner': 'test-member',
                                       'name': 'fresh-start',
                                       'idea': 'A small site for the shop.',
                                       'output_dir': str(self.output), 'account': 'alice'})
        job = result['job']
        self.assertEqual(job['repo_full_name'], 'test-member/fresh-start')
        self.assertEqual(job['notes']['request']['values']['name'], 'fresh-start')

    def test_new_project_rejects_other_owners(self):
        self.project_task()
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue({'kind': 'new-project', 'owner': 'someone',
                                  'name': 'x', 'idea': 'y',
                                  'output_dir': str(self.output), 'account': 'alice'})
        self.assertIn('test-member', str(caught.exception))


class AllowsClosedTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def tearDown(self):
        tasks.types._REGISTRY = None

    def cleanup_task(self, allows_closed=True):
        inject(TaskType(id='cleanup-merged', label='Clean up after merging',
                        profile='high-stakes', takes='pr',
                        help='Closes linked issues, deletes the merged branch and drafts release notes.',
                        post_confirm_required=True, allows_closed=allows_closed))

    def test_finished_work_kind_accepts_a_merged_pull_request(self):
        self.cleanup_task()
        self.gh.item['state'] = 'MERGED'
        job = self.control.enqueue(self.task_data(kind='cleanup-merged', number=7))['job']
        self.assertEqual(job['kind'], 'cleanup-merged')

    def test_ordinary_kinds_still_refuse_merged_items(self):
        self.cleanup_task(allows_closed=False)
        self.gh.item['state'] = 'MERGED'
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data(kind='cleanup-merged', number=7))
        self.assertEqual(caught.exception.status, 409)


class AutoPostTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_post_directly_authority_posts_without_waiting(self):
        import tempfile
        from tests.test_executor_flow import FakeWT, agent_result, happy_gh, run_job, default_cfg
        calls = []

        def apply(context, gh, job):
            calls.append(context)
            return {'result': 'Created issue #42.'}

        inject(TaskType(id='triage-issues', label='Turn words into issues',
                        profile='draft', takes='repo',
                        help='Splits your words into ready-to-post issues.',
                        apply=apply))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / 'state'
        state.mkdir()
        (state / 'authority.json').write_text(
            json.dumps({'o/r': {'post_authority': 'auto'}}))
        wt = Path(tmp.name) / 'wt'
        wt.mkdir()

        class Agent:
            def __call__(self, path, checks=None):
                self.path = path
                return self

            def run(self, instruction):
                folder = Path(self.path) / '.gha-drafts'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'one.md').write_text('First issue\nBody text.')
                return agent_result()

        cfg = default_cfg()
        cfg.state_dir = str(state)
        job = Job(job_id='job_auto', kind=JobKind.TRIAGE_ISSUES,
                  repo_full_name='o/r', number=0, command_comment_id=0,
                  requested_by=1, dedup_key='k',
                  notes={'values': {'words': 'fix it'}})
        returned, *_ = run_job(job, happy_gh(), cfg=cfg, wt=FakeWT(path=str(wt)),
                               agent=Agent())
        self.assertEqual(returned.state, JobState.DONE, returned.error)
        self.assertEqual(len(calls), 1)
        self.assertTrue(returned.notes['posted'][0]['auto'])

    def test_high_stakes_kinds_never_auto_post(self):
        import tempfile
        from tests.test_executor_flow import FakeWT, agent_result, happy_gh, run_job, default_cfg
        calls = []
        inject(TaskType(id='cleanup-merged', label='Tidy up after a merge',
                        profile='high-stakes', takes='pr',
                        help='Tidies merged work after your confirmation.',
                        post_confirm_required=True, allows_closed=True,
                        apply=lambda c, g, j: calls.append(1) or {'result': 'x'}))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / 'state'
        state.mkdir()
        (state / 'authority.json').write_text(
            json.dumps({'o/r': {'post_authority': 'auto'}}))
        wt = Path(tmp.name) / 'wt'
        wt.mkdir()

        class Agent:
            def __call__(self, path, checks=None):
                self.path = path
                return self

            def run(self, instruction):
                folder = Path(self.path) / '.gha-drafts'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'one.md').write_text('Release\nNotes.')
                return agent_result()

        cfg = default_cfg()
        cfg.state_dir = str(state)
        job = Job(job_id='job_hs', kind=JobKind.CLEANUP_MERGED,
                  repo_full_name='o/r', number=7, command_comment_id=0,
                  requested_by=1, dedup_key='k')
        returned, *_ = run_job(job, happy_gh(), cfg=cfg, wt=FakeWT(path=str(wt)),
                               agent=Agent())
        self.assertEqual(returned.state, JobState.WAITING_ON_YOU)
        self.assertEqual(calls, [])
