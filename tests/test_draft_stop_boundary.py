"""Stop at each REST/CLI mutation boundary; every external call is a fake."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ghagent import responder
from ghagent.task_write_guard import GuardedGitHub, PreWriteRefusal, ensure_not_stopped
from ghagent.tasks import new_project, organize_work, triage_issues
from tests.test_task_new_project import ApplyTests, FakeGhRun, make_job


class RecordingGitHub:
    host = 'github.example.invalid'

    def __init__(self, after_write=None, error=None):
        self.calls = []
        self.after_write = after_write
        self.error = error

    def rest(self, method, path, *args, **kwargs):
        self.calls.append((method, path, args, kwargs))
        if method.upper() in {'GET', 'HEAD', 'OPTIONS'}:
            return []
        if self.after_write:
            self.after_write()
        if self.error:
            raise self.error
        return {'number': 7}

    def pr_view(self, repo, number):
        return {'repo': repo, 'number': number}


class RestStopBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stopped = False
        self.context = {'repo': 'owner/repo', 'stop_check': lambda: self.stopped}

    def stop(self):
        self.stopped = True

    def test_initial_stop_blocks_every_mutation_method_as_safe_refusal(self):
        self.stop()
        for method in ('POST', 'put', 'PATCH', 'DELETE', 'CUSTOM'):
            with self.subTest(method=method):
                gh = RecordingGitHub()
                guarded = GuardedGitHub(gh, dict(self.context))
                with self.assertRaises(PreWriteRefusal):
                    guarded.rest(method, 'repos/owner/repo/issues', body={})
                self.assertEqual(gh.calls, [])
                self.assertFalse(guarded.write_attempted)

    def test_unreadable_stop_blocks_before_any_attempt(self):
        def unreadable():
            raise OSError('The stop state is unreadable.')
        for callback in (unreadable, 'invalid callback'):
            with self.subTest(callback=type(callback).__name__):
                gh = RecordingGitHub()
                guarded = GuardedGitHub(gh, {'stop_check': callback})
                with self.assertRaises(PreWriteRefusal):
                    guarded.rest('POST', 'repos/owner/repo/issues')
                self.assertEqual(gh.calls, [])

    def test_read_methods_and_attributes_are_delegated_during_stop(self):
        self.stop()
        gh = RecordingGitHub()
        guarded = GuardedGitHub(gh, self.context)
        self.assertEqual(guarded.host, gh.host)
        self.assertEqual(guarded.pr_view('owner/repo', 8), {'repo': 'owner/repo', 'number': 8})
        for method in ('GET', 'head', 'OPTIONS'):
            self.assertEqual(guarded.rest(method, 'repos/owner/repo/labels',
                                          {'per_page': 100}, paginate=True), [])
        self.assertEqual(len(gh.calls), 3)
        self.assertEqual(gh.calls[0][2:], (({'per_page': 100},), {'paginate': True}))
        self.assertFalse(guarded.write_attempted)
        self.assertNotIn('_github_write_attempted', self.context)

    def test_stop_after_a_real_triage_post_blocks_the_next_issue(self):
        gh = RecordingGitHub(after_write=self.stop)
        guarded = GuardedGitHub(gh, self.context)
        context = {**self.context, 'draft': {'name': 'first', 'title': 'First', 'body': 'Words'}}
        first = triage_issues.apply(context, guarded, object())
        self.assertIn('Created issue', first['result'])
        context['draft']['title'] = 'Second'
        with self.assertRaises(ValueError) as caught:
            triage_issues.apply(context, guarded, object())
        self.assertNotIsInstance(caught.exception, PreWriteRefusal)
        self.assertEqual(len(gh.calls), 1)
        self.assertTrue(guarded.write_attempted)

    def test_stop_inside_real_label_plan_blocks_later_label_writes(self):
        gh = RecordingGitHub(after_write=self.stop)
        guarded = GuardedGitHub(gh, self.context)
        context = {**self.context, 'draft': {'body': 'LABEL: bug — A broken feature\n'
                                                         'LABEL: docs — Improve instructions'}}
        with self.assertRaises(ValueError) as caught:
            organize_work.apply(context, guarded, object())
        self.assertNotIsInstance(caught.exception, PreWriteRefusal)
        self.assertIn('1 label(s) were created', str(caught.exception))
        self.assertEqual([c[0] for c in gh.calls], ['GET', 'POST'])

    def test_failed_remote_attempt_is_recorded_before_client_raises(self):
        gh = RecordingGitHub(after_write=self.stop, error=RuntimeError('No reply after sending.'))
        guarded = GuardedGitHub(gh, self.context)
        with self.assertRaisesRegex(RuntimeError, 'No reply'):
            guarded.rest('POST', 'repos/owner/repo/issues')
        self.assertTrue(guarded.write_attempted)
        self.assertTrue(self.context['_github_write_attempted'])
        with self.assertRaises(ValueError) as caught:
            guarded.rest('POST', 'repos/owner/repo/issues')
        self.assertNotIsInstance(caught.exception, PreWriteRefusal)
        self.assertEqual(len(gh.calls), 1)

    def test_existing_git_push_is_not_reclassified_safe_before_rest_write(self):
        gh = RecordingGitHub()
        guarded = GuardedGitHub(gh, self.context)
        ensure_not_stopped(self.context, after_write=True)
        self.stop()
        with self.assertRaises(ValueError) as caught:
            guarded.rest('POST', 'repos/owner/repo/pulls')
        self.assertNotIsInstance(caught.exception, PreWriteRefusal)
        self.assertEqual(gh.calls, [])
        self.assertNotIn('branch was already pushed', str(caught.exception))

    def test_replacement_wrapper_preserves_an_earlier_attempt_in_context(self):
        gh = RecordingGitHub()
        GuardedGitHub(gh, self.context).rest('POST', 'repos/owner/repo/issues')
        self.stop()
        with self.assertRaises(ValueError) as caught:
            GuardedGitHub(gh, self.context).rest('DELETE', 'repos/owner/repo/issues/7')
        self.assertNotIsInstance(caught.exception, PreWriteRefusal)
        self.assertEqual(len(gh.calls), 1)

    def test_independent_contexts_do_not_share_stop_or_attempt_state(self):
        first = RecordingGitHub()
        other = RecordingGitHub()
        first_guard = GuardedGitHub(first, self.context)
        other_guard = GuardedGitHub(other, {'stop_check': lambda: False})
        first_guard.rest('POST', 'repos/owner/repo/issues')
        self.stop()
        with self.assertRaises(ValueError):
            first_guard.rest('POST', 'repos/owner/repo/issues')
        other_guard.rest('POST', 'repos/other/project/issues')
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(other.calls), 1)


class NewProjectStopBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Reuse existing temporary scaffold construction only. Every git and
        # gh process call is replaced below, including the initial commit.
        self.fixture = ApplyTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_stop_during_local_commit_prevents_repo_create_and_push(self):
        stopped = False
        calls = []
        def git(args, cwd):
            nonlocal stopped
            calls.append(list(args))
            if args[0] == 'commit':
                stopped = True
            return 0, '', ''
        remote = FakeGhRun()
        with patch.object(new_project, '_run_git', git), patch.object(responder, '_run_gh', remote):
            with self.assertRaises(PreWriteRefusal):
                new_project.apply(self.fixture.context(stop_check=lambda: stopped), None, make_job())
        self.assertIn('commit', [args[0] for args in calls])
        self.assertEqual(remote.calls, [])

    def test_unreadable_stop_prevents_repo_creation(self):
        def unreadable():
            raise OSError('Cannot verify stop.')
        remote = FakeGhRun()
        with patch.object(new_project, '_run_git', return_value=(0, '', '')), \
                patch.object(responder, '_run_gh', remote):
            with self.assertRaises(PreWriteRefusal):
                new_project.apply(self.fixture.context(stop_check=unreadable), None, make_job())
        self.assertEqual(remote.calls, [])

    def test_unstopped_project_keeps_the_existing_explicit_create_command(self):
        remote = FakeGhRun()
        with patch.object(new_project, '_run_git', return_value=(0, '', '')), \
                patch.object(responder, '_run_gh', remote):
            result = new_project.apply(self.fixture.context(stop_check=lambda: False), None, make_job())
        self.assertEqual(len(remote.creates()), 1)
        self.assertIn('--private', remote.creates()[0])
        self.assertIn('--push', remote.creates()[0])
        self.assertIn('Created', result['result'])


if __name__ == '__main__':
    unittest.main()
