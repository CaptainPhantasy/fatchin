"""sync-branch: registry contract, validation, the plan draft, and the merge.

Offline only: the executor runs against the fakes in
tests/test_executor_flow.py, git is a recorder standing in for
subprocess.run, and gh is a recorder standing in for responder._run_gh. No
network, no model, and no real repository is ever touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent import responder, tasks
from ghagent.interfaces import Config, Job, JobKind, JobState, RepoConfig
from ghagent.tasks import sync_branch
from ghagent.tasks.types import PROFILES
from ghagent.web_control import ApiError

from tests.test_executor_flow import FakeWT, agent_result, default_cfg, happy_gh, run_job
from tests.test_web_control import GuiFixture

REPO = "owner/repo"
SOURCE = "develop"
TARGET = "main"
TITLE = "Bring main up to date from develop"
BODY = ("1. Git fetches both branches from GitHub.\n"
        "2. main receives one merge commit carrying develop's work.\n"
        "3. The result is pushed straight to main, with no pull request.\n"
        "Risk: the merge may conflict and need a person.")
COMMIT = 'a' * 40


class Completed:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGitRun:
    """Stands in for subprocess.run inside sync_branch.

    `results` maps a git subcommand ("fetch", "checkout", "merge", "push") to
    the (returncode, stdout, stderr) it should answer with.
    """

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.calls: list[tuple[list[str], dict]] = []

    KNOWN = ("fetch", "checkout", "merge", "push")

    @classmethod
    def command(cls, args):
        for part in args[1:]:
            if part in cls.KNOWN:
                return part
        return ""

    def __call__(self, argv, **kwargs):
        args = list(argv)
        self.calls.append((args, dict(kwargs)))
        command = self.command(args)
        if command == "merge" and "--abort" in args:
            return Completed(0, "", "")
        if command in self.results:
            return Completed(*self.results[command])
        if args[1] in {'rev-parse', 'write-tree'}:
            return Completed(0, COMMIT + '\n', '')
        if args[1] == 'ls-remote':
            return Completed(0, ''.join(COMMIT + '\t' + name + '\n'
                                        for name in args[3:]), '')
        return Completed(0, "", "")

    def commands(self, name):
        return [args for args, _ in self.calls if self.command(args) == name]

    def aborts(self):
        return [args for args in self.commands("merge") if "--abort" in args]


class NoWriteGh:
    """Allows inspection, but fails if the task makes an API write."""

    def __init__(self):
        self.calls = []

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append((method, path))
        if method != 'GET':
            raise AssertionError(f"sync-branch must not write to the API: {method} {path}")
        if path == f'repos/{REPO}':
            return {'default_branch': 'main'}
        if path == f'repos/{REPO}/rulesets':
            return []
        raise ValueError('404 Not Found')


class RecordGhRun:
    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout=120.0):
        self.calls.append(list(args))
        return 0, "", ""


class DraftAgent:
    """Coding-agent factory stub: reads, then writes the plan draft."""

    def __init__(self, files=None):
        self.files = files or {
            ".gha-drafts/plan.md": f"{TITLE}\n{BODY}\n",
        }
        self.instructions: list[str] = []
        self.worktree = None

    def __call__(self, worktree_path=None, checks=None):
        self.worktree = worktree_path
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        root = Path(self.worktree)
        for name, text in self.files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return agent_result()


def make_job(**over):
    data = dict(job_id="job_20261022T101500Z_ab12cd34",
                kind=JobKind.SYNC_BRANCH, repo_full_name=REPO, number=0,
                command_comment_id=0, requested_by=1, dedup_key="k",
                notes={"values": {"source": SOURCE, "target": TARGET},
                       'request': {'accept_no_checks': True}})
    data.update(over)
    return Job(**data)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["sync-branch"]
        self.assertIs(task, sync_branch.TASK)
        self.assertEqual(task.id, "sync-branch")
        self.assertEqual(task.profile, "write")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")
        self.assertIn("branch", task.label.lower())
        self.assertIn("conflict", task.help.lower())
        self.assertTrue(task.post_confirm_required)
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))
        self.assertTrue(callable(task.apply))

    def test_it_requires_exactly_the_two_branch_names(self):
        task = sync_branch.TASK
        self.assertEqual(list(task.fields), ["source", "target"])
        self.assertTrue(task.fields["source"]["required"])
        self.assertTrue(task.fields["target"]["required"])
        self.assertEqual(task.required_fields(), ["source", "target"])
        self.assertLessEqual(len(task.required_fields()), 3)

    def test_the_gui_catalog_lists_it(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("sync-branch", listed)
        entry = listed["sync-branch"]
        self.assertEqual(entry["profile"], "write")
        self.assertEqual(entry["takes"], "repo")
        self.assertEqual([field["name"] for field in entry["fields"]],
                         ["source", "target"])
        self.assertTrue(all(field["required"] for field in entry["fields"]))


class ValidationTests(unittest.TestCase):
    def data(self, source=SOURCE, target=TARGET):
        return {"repo": REPO, "number": 0,
                "values": {"source": source, "target": target},
                "accept_no_checks": False}

    def test_two_named_branches_are_valid(self):
        self.assertIsNone(sync_branch.validate(self.data()))

    def test_missing_branch_names_are_refused_with_the_field_label(self):
        for source, target, label in (("", TARGET, "Branch to take changes from"),
                                      ("   ", TARGET, "Branch to take changes from"),
                                      (None, TARGET, "Branch to take changes from"),
                                      (SOURCE, "", "Branch to bring up to date"),
                                      (SOURCE, None, "Branch to bring up to date")):
            with self.subTest(source=source, target=target):
                with self.assertRaises(ApiError) as caught:
                    sync_branch.validate(self.data(source, target))
                self.assertIn("Fill in", str(caught.exception))
                self.assertIn(label, str(caught.exception))

    def test_the_same_branch_twice_is_refused(self):
        with self.assertRaises(ApiError) as caught:
            sync_branch.validate(self.data(TARGET, TARGET))
        self.assertIn("nothing to move", str(caught.exception))

    def test_names_git_would_read_as_options_are_refused(self):
        for name in ("-f", "--hard", "feature login", "a..b", "x@{"):
            with self.subTest(name=name):
                with self.assertRaises(ApiError) as caught:
                    sync_branch.validate(self.data(name, TARGET))
                self.assertIn("safely", str(caught.exception))

    def test_empty_and_missing_input_read_as_plain_sentences(self):
        for data in (None, {}, {"values": {}}):
            with self.subTest(data=data):
                with self.assertRaises(ApiError):
                    sync_branch.validate(data)


class InstructionTests(unittest.TestCase):
    def test_instruction_names_both_branches_the_plan_and_the_risks(self):
        text = sync_branch.build_instruction(
            {"repo": REPO, "values": {"source": SOURCE, "target": TARGET}})
        self.assertIn(REPO, text)
        self.assertIn(SOURCE, text)
        self.assertIn(TARGET, text)
        self.assertIn(sync_branch.DRAFT_FILE, text)
        self.assertIn("first line", text)
        self.assertIn("conflict", text.lower())
        self.assertIn("no pull request", text)
        self.assertIn("summary", text)
        self.assertIn("Do not merge, commit, or push", text)

    def test_instruction_without_values_still_reads(self):
        text = sync_branch.build_instruction({"repo": REPO})
        self.assertIn(REPO, text)
        self.assertIn("the source branch", text)
        self.assertIn(sync_branch.DRAFT_FILE, text)


class DraftExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(Path(temp.name) / "worktrees")
        self.wt_path = str(Path(temp.name) / "wt")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_plan_reaches_waiting_on_you_with_no_remote_write(self):
        agent = DraftAgent()
        gh = happy_gh()
        returned, queue, wt, _, git_rec, gh_rec = run_job(
            make_job(), gh, cfg=self.cfg, wt=FakeWT(path=self.wt_path),
            agent=agent)

        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertEqual(returned.notes["phase"], "waiting-on-you")
        states = [state for state, _ in queue.history]
        self.assertIn("working", states)
        self.assertEqual(states[-1], "waiting-on-you")
        self.assertNotIn("responding", states)

        drafts = returned.notes["drafts"]
        self.assertEqual([d["name"] for d in drafts], ["plan"])
        self.assertEqual(drafts[0]["title"], TITLE)
        self.assertIn("Risk", drafts[0]["body"])
        self.assertNotIn(TITLE, drafts[0]["body"])

        self.assertIn(sync_branch.DRAFT_FILE, agent.instructions[0])
        self.assertIn(REPO, agent.instructions[0])
        self.assertFalse([c for c in wt.calls if c[0] == "commit_all"])
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])
        self.assertEqual(gh_rec.calls, [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.worktree = Path(temp.name) / "wt"
        self.worktree.mkdir()

    def context(self, **over):
        data = {"repo": REPO, "number": 0,
                "draft": {"name": "plan", "title": TITLE, "body": BODY},
                "values": {"source": SOURCE, "target": TARGET},
                'config': Config(repos=[RepoConfig('owner', 'repo')]),
                "worktree": str(self.worktree)}
        data.update(over)
        return data

    def run_apply(self, git=None, gh=None, context=None, job=None):
        git = git or FakeGitRun()
        gh = gh or NoWriteGh()
        gh_run = RecordGhRun()
        with patch.object(sync_branch.subprocess, "run", git), \
                patch.object(responder, "_run_gh", gh_run):
            outcome = sync_branch.TASK.apply(context or self.context(), gh,
                                             job or make_job())
        return outcome, git, gh, gh_run

    def test_fetch_checkout_merge_and_push_are_exact_and_never_forced(self):
        outcome, git, gh, gh_run = self.run_apply()
        self.assertEqual([args for args, _ in git.calls if git.command(args)], [
            ["git", "fetch", "origin",
             f'+refs/heads/{SOURCE}:refs/remotes/origin/{SOURCE}',
             f'+refs/heads/{TARGET}:refs/remotes/origin/{TARGET}'],
            ["git", "checkout", "--detach", COMMIT],
            ["git", "-c", "user.name=gha-worker",
             "-c", "user.email=gha-worker@inference",
             "merge", "--no-ff", COMMIT, "-m",
             f"merge {SOURCE} into {TARGET}"],
            ["git", "push", "origin", f"{COMMIT}:refs/heads/{TARGET}"],
        ])
        self.assertTrue(all(kw.get("cwd") == str(self.worktree)
                            for _, kw in git.calls))
        pushes = git.commands("push")
        self.assertEqual(len(pushes), 1)
        self.assertNotIn("--force", pushes[0])
        self.assertNotIn("-f", pushes[0])
        self.assertEqual(outcome,
                         {"result": f"Moved {SOURCE} into {TARGET} and pushed."})
        self.assertTrue(gh.calls)
        self.assertTrue(all(method == 'GET' for method, _ in gh.calls))
        self.assertEqual(gh_run.calls, [])

    def test_git_runs_without_a_shell_and_without_a_password_prompt(self):
        _, git, _, _ = self.run_apply()
        self.assertTrue(git.calls)
        for args, kwargs in git.calls:
            self.assertIsInstance(args, list)
            self.assertNotEqual(kwargs.get("shell"), True)
            env = kwargs.get("env") or {}
            self.assertEqual(set(env),
                             {"PATH", "HOME", "GIT_TERMINAL_PROMPT"})
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_a_conflict_is_undone_and_left_for_a_person(self):
        git = FakeGitRun(results={"merge": (
            1, "",
            "Auto-merging README.md\nCONFLICT (content): Merge conflict in "
            "README.md\nAutomatic merge failed; fix conflicts and then commit "
            "the result.\n")})
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git)
        self.assertEqual(str(caught.exception), sync_branch.CONFLICT_MESSAGE)
        self.assertEqual(git.aborts(), [["git", "merge", "--abort"]])
        self.assertEqual(git.commands("push"), [])
        self.assertEqual(len(git.commands("fetch")), 1)
        self.assertEqual(len(git.commands("checkout")), 1)

    def test_a_failed_merge_without_the_word_conflict_still_aborts(self):
        git = FakeGitRun(results={"merge": (1, "", "fatal: unrelated histories\n")})
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git)
        self.assertEqual(str(caught.exception), sync_branch.CONFLICT_MESSAGE)
        self.assertEqual(git.aborts(), [["git", "merge", "--abort"]])
        self.assertEqual(git.commands("push"), [])

    def test_fetch_failure_stops_before_any_merge_or_push(self):
        git = FakeGitRun(results={"fetch": (128, "", "fatal: could not read Username")})
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git)
        message = str(caught.exception)
        self.assertIn("fetch", message)
        self.assertIn("could not read Username", message)
        self.assertEqual(git.commands("checkout"), [])
        self.assertEqual(git.commands("merge"), [])
        self.assertEqual(git.commands("push"), [])

    def test_missing_target_branch_stops_before_the_merge(self):
        git = FakeGitRun(results={"checkout": (
            1, "", "error: pathspec 'main' did not match any file(s) known to git")})
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git)
        message = str(caught.exception)
        self.assertIn(TARGET, message)
        self.assertIn("exists", message)
        self.assertEqual(git.commands("merge"), [])
        self.assertEqual(git.commands("push"), [])

    def test_push_failure_requires_checking_the_uncertain_remote_outcome(self):
        git = FakeGitRun(results={"push": (128, "", "fatal: could not read Username")})
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git)
        message = str(caught.exception)
        self.assertIn("push", message)
        self.assertIn("uncertain", message)
        self.assertIn("Check main on GitHub", message)
        self.assertEqual(len(git.commands("merge")), 1)

    def test_missing_worktree_raises_one_plain_sentence(self):
        git = FakeGitRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git,
                           context=self.context(worktree="/nonexistent/wt"))
        self.assertIn("scratch checkout", str(caught.exception))
        self.assertEqual(git.calls, [])

    def test_worktree_falls_back_to_the_job_notes(self):
        job = make_job(notes={"values": {"source": SOURCE, "target": TARGET},
                              'request': {'accept_no_checks': True},
                              "worktree_path": str(self.worktree)})
        context = self.context()
        context.pop("worktree")
        outcome, git, _, _ = self.run_apply(context=context, job=job)
        self.assertIn(f"{SOURCE} into {TARGET}", outcome["result"])
        self.assertEqual(len(git.commands("push")), 1)

    def test_branch_names_are_checked_before_git_runs(self):
        git = FakeGitRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, context=self.context(values={"target": TARGET}))
        self.assertIn("Fill in", str(caught.exception))
        self.assertEqual(git.calls, [])

        git = FakeGitRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, context=self.context(
                values={"source": TARGET, "target": TARGET}))
        self.assertIn("nothing to move", str(caught.exception))
        self.assertEqual(git.calls, [])


class EnqueueTests(GuiFixture, unittest.TestCase):
    """The real GUI door: two required inputs, both checked before queueing."""

    def setUp(self):
        tasks.types._REGISTRY = None
        self.build_gui()

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_the_gui_queues_it_with_the_two_branch_names(self):
        job = self.control.enqueue(self.task_data(
            kind="sync-branch", number=None, source=SOURCE,
            target=TARGET))["job"]
        self.assertEqual(job["kind"], "sync-branch")
        self.assertEqual(job["number"], 0)
        self.assertEqual(job["notes"]["values"],
                         {"source": SOURCE, "target": TARGET})

    def test_the_gui_refuses_a_blank_branch_name_with_its_label(self):
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data(
                kind="sync-branch", number=None, source="   ", target=TARGET))
        self.assertIn("Branch to take changes from", str(caught.exception))

    def test_post_requires_a_separate_explicit_merge_confirmation(self):
        self.persisted_job(kind=JobKind.SYNC_BRANCH, state=JobState.WAITING_ON_YOU,
                           notes={'drafts': [{'name': 'plan', 'title': TITLE,
                                              'body': BODY}],
                                  'values': {'source': SOURCE, 'target': TARGET}})
        with patch.object(sync_branch, '_run_git') as git:
            with self.assertRaises(ApiError) as caught:
                self.control.draft_action('job_test', {'action': 'post'})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('Confirm', str(caught.exception))
        git.assert_not_called()
        self.assertNotIn('posting', self.control.queue().get('job_test').notes)


if __name__ == "__main__":
    unittest.main()
