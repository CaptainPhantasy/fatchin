"""write-docs: registry contract, validation, draft run, one explicit PR.

Offline only: git is a recorder standing in for subprocess.run, gh is a recorder
standing in for responder._run_gh, and the coding agent is a stub that writes
files. No network, no model, no real repository is ever touched.
"""
from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent import responder, tasks
from ghagent.interfaces import Config, Job, JobKind, JobState, RepoConfig
from ghagent.tasks import write_docs
from ghagent.tasks.types import PROFILES

from tests.test_executor_flow import FakeWT, agent_result, default_cfg, happy_gh, run_job

REPO = "owner/repo"
TITLE = "Explain how to install and run the project"
BODY = ("The README never said how to install the project.\n\n"
        "It now lists every setup step in order and how to run the tests.")
COMMIT = 'a' * 40


class Completed:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGitRun:
    """Stands in for subprocess.run inside write_docs."""

    def __init__(self, staged="README.md\ndocs/setup.md\n", failing=None):
        self.staged = staged
        self.failing = dict(failing or {})
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, cwd=None, capture_output=True, text=True,
                 timeout=None):
        args = list(argv)
        self.calls.append((args, cwd))
        command = args[1] if len(args) > 1 else ""
        if command in self.failing:
            return Completed(*self.failing[command])
        if command in {'rev-parse', 'write-tree'}:
            return Completed(0, COMMIT + '\n', '')
        if command == "diff":
            return Completed(0, ("\0".join(self.staged.splitlines()) + "\0")
                             if "-z" in args else self.staged, "")
        if command == "commit":
            return Completed(0, "[main abc1234] docs: refresh", "")
        return Completed(0, "", "")

    def commands(self, name):
        return [args for args, _ in self.calls if len(args) > 1 and args[1] == name]


class FakeGhRun:
    """Stands in for responder._run_gh; reads the --body-file it is given."""

    def __init__(self, rc=0, out=f"https://github.com/{REPO}/pull/44\n",
                 err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls: list[tuple[list[str], float]] = []
        self.bodies: list[str] = []

    def __call__(self, args, timeout=120.0):
        self.calls.append((list(args), timeout))
        if "--body-file" in args:
            path = args[args.index("--body-file") + 1]
            self.bodies.append(Path(path).read_text(encoding="utf-8"))
        return self.rc, self.out, self.err

    def creates(self):
        return [args for args, _ in self.calls if args[:2] == ["pr", "create"]]


class DraftAgent:
    """Coding-agent factory stub: edits documentation and writes the PR draft."""

    def __init__(self, files=None):
        self.files = files or {
            "README.md": "# shop\n\nInstall with pip install -e .\n",
            ".gha-drafts/pull-request.md": f"{TITLE}\n{BODY}\n",
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


def make_job(job_id="job_20261022T101500Z_ab12cd34", **over):
    data = dict(job_id=job_id, kind=JobKind.WRITE_DOCS, repo_full_name=REPO,
                number=0, command_comment_id=0, requested_by=1, dedup_key="k",
                notes={"values": {"focus": ""},
                       'request': {'accept_no_checks': True}})
    data.update(over)
    return Job(**data)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["write-docs"]
        self.assertIs(task, write_docs.TASK)
        self.assertEqual(task.id, "write-docs")
        self.assertEqual(task.profile, "draft")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")
        self.assertIn("documentation", task.label.lower())
        self.assertIn("readme", task.help.lower())
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))
        self.assertTrue(callable(task.apply))

    def test_the_only_field_is_an_optional_focus_note(self):
        task = write_docs.TASK
        self.assertEqual(list(task.fields), ["focus"])
        self.assertFalse(task.fields["focus"]["required"])
        self.assertEqual(task.required_fields(), [])
        self.assertLessEqual(len(task.required_fields()), 1)

    def test_the_gui_catalog_lists_it(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("write-docs", listed)
        self.assertEqual(listed["write-docs"]["profile"], "draft")
        self.assertEqual(listed["write-docs"]["takes"], "repo")
        self.assertEqual([field["name"] for field in listed["write-docs"]["fields"]],
                         ["focus"])


class ValidationTests(unittest.TestCase):
    def test_blank_focus_is_allowed(self):
        for data in ({"repo": REPO, "values": {}},
                     {"repo": REPO, "values": {"focus": ""}},
                     {"repo": REPO, "values": {"focus": "   \n "}},
                     {"repo": REPO}, None, {}):
            with self.subTest(data=data):
                self.assertIsNone(write_docs.validate(data))

    def test_a_focus_note_is_allowed_too(self):
        self.assertIsNone(write_docs.validate(
            {"repo": REPO, "values": {"focus": "make setup steps clearer"}}))


class InstructionTests(unittest.TestCase):
    def test_instruction_names_the_code_the_readme_and_the_draft(self):
        text = write_docs.build_instruction(
            {"repo": REPO, "values": {"focus": "Windows setup"}})
        self.assertIn(REPO, text)
        self.assertIn("README.md", text)
        self.assertIn(write_docs.DRAFT_FILE, text)
        self.assertIn("first line", text)
        self.assertIn("src/setup.py:12", text)
        self.assertIn("first-time", text)
        self.assertIn("Windows setup", text)
        self.assertIn("Do not commit, push", text)

    def test_instruction_without_values_still_reads(self):
        text = write_docs.build_instruction({"repo": REPO})
        self.assertIn(REPO, text)
        self.assertIn(write_docs.DRAFT_FILE, text)
        self.assertNotIn("pay particular attention", text)


class DraftExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(Path(temp.name) / "worktrees")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_draft_run_waits_for_the_person_and_touches_no_remote(self):
        agent = DraftAgent()
        gh = happy_gh()
        returned, queue, wt, _, git_rec, gh_rec = run_job(
            make_job(), gh, cfg=self.cfg, wt=FakeWT(), agent=agent)

        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertEqual(returned.notes["phase"], "waiting-on-you")
        states = [state for state, _ in queue.history]
        self.assertIn("working", states)
        self.assertEqual(states[-1], "waiting-on-you")
        self.assertNotIn("responding", states)

        drafts = returned.notes["drafts"]
        self.assertEqual([d["name"] for d in drafts], ["pull-request"])
        self.assertEqual(drafts[0]["title"], TITLE)
        self.assertIn("how to install", drafts[0]["body"])
        self.assertNotIn(TITLE, drafts[0]["body"])

        self.assertIn(write_docs.DRAFT_FILE, agent.instructions[0])
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
        (self.worktree / "README.md").write_text("old words\n", encoding="utf-8")

    def context(self, **over):
        data = {"repo": REPO, "number": 0,
                'config': Config(repos=[RepoConfig('owner', 'repo')]),
                "draft": {"name": "pull-request", "title": TITLE, "body": BODY},
                "values": {"focus": ""}, "worktree": str(self.worktree)}
        data.update(over)
        return data

    def run_apply(self, git=None, gh=None, context=None, job=None):
        git = git or FakeGitRun()
        gh = gh or FakeGhRun()
        with patch.object(write_docs.subprocess, "run", git), \
                patch.object(responder, "_run_gh", gh):
            from tests.test_task_sync_branch import NoWriteGh
            outcome = write_docs.TASK.apply(context or self.context(), NoWriteGh(),
                                            job or make_job())
        return outcome, git, gh

    def test_commit_push_and_pr_are_explicit_and_never_forced(self):
        outcome, git, gh = self.run_apply()

        self.assertEqual([args for args, _ in git.calls][0], ["git", "add", "-A"])
        self.assertTrue(all(cwd == str(self.worktree) for _, cwd in git.calls))
        commits = git.commands("commit")
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0][2], "-m")
        self.assertEqual(commits[0][3], write_docs.COMMIT_MESSAGE)
        self.assertTrue(commits[0][3].startswith("docs: "))
        pushes = git.commands("push")
        self.assertEqual(pushes,
                         [["git", "push", "origin",
                           f"{COMMIT}:refs/heads/gha/docs-ab12cd34"]])
        self.assertNotIn("force", " ".join(pushes[0]).lower())
        self.assertNotIn("-f", pushes[0])

        creates = gh.creates()
        self.assertEqual(len(creates), 1)
        args = creates[0]
        self.assertEqual(args[args.index("--repo") + 1], REPO)
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("--head") + 1], "gha/docs-ab12cd34")
        self.assertEqual(args[args.index("--title") + 1], TITLE)
        self.assertIn("--body-file", args)
        self.assertEqual(gh.bodies, [BODY])
        self.assertEqual(outcome,
                         {"result": f"Opened pull request #44 — {TITLE}"})

    def test_base_branch_comes_from_values_and_config_context(self):
        _, _, gh = self.run_apply(context=self.context(base_branch="release"))
        args = gh.creates()[0]
        self.assertEqual(args[args.index("--base") + 1], "release")

        _, _, gh = self.run_apply(context=self.context(values={"focus": "",
                                                               "base_branch": "develop"}))
        args = gh.creates()[0]
        self.assertEqual(args[args.index("--base") + 1], "develop")

        job = make_job(notes={"values": {"focus": ""}, "base_branch": "trunk",
                              'request': {'accept_no_checks': True}})
        _, _, gh = self.run_apply(job=job)
        args = gh.creates()[0]
        self.assertEqual(args[args.index("--base") + 1], "trunk")

    def test_build_artifacts_are_unstaged_and_never_committed(self):
        staged = ("README.md\n"
                  "src/__pycache__/app.cpython-311.pyc\n"
                  "stale.pyc\n"
                  "build/.pytest_cache/v/cache/nodeids\n"
                  ".mypy_cache/3.11/modules.json\n")
        git = FakeGitRun(staged=staged)
        outcome, git, gh = self.run_apply(git=git)

        resets = git.commands("reset")
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0][:5], ["git", "reset", "-q", "HEAD", "--"])
        self.assertEqual(sorted(resets[0][5:]),
                         sorted(["src/__pycache__/app.cpython-311.pyc",
                                 "stale.pyc",
                                 "build/.pytest_cache/v/cache/nodeids",
                                 ".mypy_cache/3.11/modules.json"]))
        self.assertNotIn("README.md", resets[0])
        commit_text = " ".join(git.commands("commit")[0])
        for artifact in ("__pycache__", ".pyc", ".pytest_cache", ".mypy_cache"):
            self.assertNotIn(artifact, commit_text)
        self.assertEqual(len(gh.creates()), 1)
        self.assertIn("pull request #44", outcome["result"])

    def test_draft_unstage_failure_prevents_commit_and_remote_writes(self):
        git = FakeGitRun(staged="README.md\n.gha-drafts/pull-request.md\n",
                         failing={"reset": (1, "", "index locked")})
        gh = FakeGhRun()
        with self.assertRaisesRegex(ValueError, "scratch.*could not be excluded"):
            self.run_apply(git=git, gh=gh)
        self.assertEqual(git.commands("commit"), [])
        self.assertEqual(gh.calls, [])

    def test_unreadable_staged_paths_prevent_commit_and_remote_writes(self):
        git = FakeGitRun(failing={"diff": (1, "", "index unreadable")})
        gh = FakeGhRun()
        with self.assertRaisesRegex(ValueError, "staged.*could not be read"):
            self.run_apply(git=git, gh=gh)
        self.assertEqual(git.commands("commit"), [])
        self.assertEqual(gh.calls, [])

    def test_real_commit_excludes_worker_drafts_and_keeps_documentation(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.worktree, check=True,
                                  capture_output=True, text=True).stdout
        git("init", "-q")
        git("config", "user.name", "Task test")
        git("config", "user.email", "task-test@example.invalid")
        git("add", "README.md")
        git("commit", "-qm", "Initial documentation")
        (self.worktree / "README.md").write_text("New setup instructions\n")
        draft = self.worktree / ".gha-drafts"
        draft.mkdir()
        (draft / "pull-request.md").write_text("Private worker scratch notes\n")
        (draft / "quoted\nnotes.md").write_text("More private scratch notes\n")
        (self.worktree / "docs").mkdir()
        (self.worktree / "docs" / "setup.md").write_text("Run the app\n")
        real_run_git = write_docs._run_git
        def local_git(args, cwd, timeout=write_docs.GIT_TIMEOUT_SECONDS):
            if args[0] == "push":
                return 0, "", ""
            return real_run_git(args, cwd, timeout)
        with patch.object(write_docs, "_run_git", local_git), \
                patch.object(write_docs, "verify_before_write", return_value="tested"), \
                patch.object(write_docs, "ensure_prepared_tree", return_value="commit"), \
                patch.object(write_docs, "_open_pull_request", return_value={}):
            write_docs.apply(self.context(), None, make_job())
        self.assertEqual(git("ls-tree", "-r", "--name-only", "HEAD").splitlines(),
                         ["README.md", "docs/setup.md"])
        self.assertEqual(git("show", "HEAD:README.md"), "New setup instructions\n")
        self.assertTrue((draft / "pull-request.md").exists())

    def test_missing_worktree_raises_one_plain_sentence(self):
        git, gh = FakeGitRun(), FakeGhRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, gh=gh,
                           context=self.context(worktree="/nonexistent/wt"))
        message = str(caught.exception)
        self.assertIn("scratch checkout", message)
        self.assertNotIn("Traceback", message)
        self.assertEqual(git.calls, [])
        self.assertEqual(gh.calls, [])

    def test_missing_worktree_falls_back_to_job_notes(self):
        git, gh = FakeGitRun(), FakeGhRun()
        job = make_job(notes={"values": {}, "worktree_path": str(self.worktree),
                              'request': {'accept_no_checks': True}})
        context = self.context()
        context.pop("worktree")
        outcome, git, gh = self.run_apply(git=git, gh=gh, context=context, job=job)
        self.assertIn("pull request #44", outcome["result"])
        self.assertEqual(len(gh.creates()), 1)

    def test_nothing_to_commit_raises_without_pushing_or_opening(self):
        for staged in ("", "\n", "src/__pycache__/only.pyc\n"):
            with self.subTest(staged=staged):
                git, gh = FakeGitRun(staged=staged), FakeGhRun()
                with self.assertRaises(ValueError) as caught:
                    self.run_apply(git=git, gh=gh)
                self.assertIn("nothing to commit", str(caught.exception))
                self.assertEqual(git.commands("commit"), [])
                self.assertEqual(git.commands("push"), [])
                self.assertEqual(gh.calls, [])

    def test_missing_title_refuses_before_touching_git(self):
        git, gh = FakeGitRun(), FakeGhRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, gh=gh,
                           context=self.context(
                               draft={"name": "pull-request", "title": "  ",
                                      "body": BODY}))
        self.assertIn("no title", str(caught.exception))
        self.assertEqual(git.calls, [])
        self.assertEqual(gh.calls, [])

    def test_push_failure_reads_as_a_plain_sentence(self):
        git = FakeGitRun(failing={"push": (128, "", "fatal: could not read Username")})
        gh = FakeGhRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, gh=gh)
        message = str(caught.exception)
        self.assertIn("could not be pushed", message)
        self.assertIn("could not read Username", message)
        self.assertEqual(gh.calls, [])

    def test_unreadable_pr_number_is_reported_not_raised(self):
        outcome, git, gh = self.run_apply(gh=FakeGhRun(out="warning: created\n"))
        self.assertIn("could not be read", outcome["result"])
        self.assertNotIn("Traceback", str(outcome))

    def test_gh_refusal_reads_as_a_plain_sentence(self):
        gh = FakeGhRun(rc=1, out="", err="a pull request already exists")
        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=gh)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(len(gh.creates()), 1)


if __name__ == "__main__":
    unittest.main()
