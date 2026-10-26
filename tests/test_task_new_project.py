"""new-project: registry contract, validation, draft run, one explicit create.

Offline only: git is a recorder standing in for subprocess.run, gh is a recorder
standing in for responder._run_gh, and the coding agent is a stub that writes
files. No network, no model, and no GitHub repository is ever touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent import responder, tasks
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.tasks import new_project
from ghagent.tasks.types import PROFILES

from tests.test_executor_flow import (FakeWT, agent_result, default_cfg, happy_gh,
                                      run_job)

REPO = "example-owner/fresh-start"
NAME = "fresh-start"
IDEA = "A small website that lists the shop's opening hours."
PLAN = (f"Create {NAME}\n"
        "- README.md — what the project is and how to start it.\n"
        "- tests/test_smoke.py — one test that asserts something true.\n")


class Completed:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGitRun:
    """Stands in for subprocess.run inside new_project."""

    def __init__(self, failing=None):
        self.failing = dict(failing or {})
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, cwd=None, capture_output=True, text=True,
                 timeout=None):
        args = list(argv)
        self.calls.append((args, cwd))
        command = args[1] if len(args) > 1 else ""
        if command in self.failing:
            return Completed(*self.failing[command])
        return Completed(0, "", "")

    def commands(self, name):
        return [args for args, _ in self.calls
                if len(args) > 1 and args[1] == name]


class FakeGhRun:
    """Stands in for responder._run_gh."""

    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args, timeout=120.0):
        self.calls.append((list(args), timeout))
        return self.rc, self.out, self.err

    def creates(self):
        return [args for args, _ in self.calls if args[:2] == ["repo", "create"]]


class PlanAgent:
    """Coding-agent factory stub: writes the plan and the scaffold into the
    scratch folder the executor hands it."""

    def __init__(self, files=None):
        self.files = files or {
            "README.md": f"# {NAME}\n\nA small website for the shop.\n",
            ".gitignore": "__pycache__/\n",
            "LICENSE": "MIT License\n\nCopyright the repository owner\n",
            "tests/test_smoke.py": "import unittest\n",
            new_project.PLAN_FILE: PLAN,
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
    data = dict(job_id=job_id, kind=JobKind.NEW_PROJECT, repo_full_name=REPO,
                number=0, command_comment_id=0, requested_by=1, dedup_key="k",
                notes={"values": {"name": NAME, "idea": IDEA}})
    data.update(over)
    return Job(**data)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["new-project"]
        self.assertIs(task, new_project.TASK)
        self.assertEqual(task.id, "new-project")
        self.assertEqual(task.profile, "high-stakes")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "none")
        self.assertTrue(task.post_confirm_required)
        self.assertEqual(task.label, "Start a new project")
        self.assertIn("repository", task.help.lower())
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))
        self.assertTrue(callable(task.apply))

    def test_field_caps_and_labels(self):
        task = new_project.TASK
        self.assertEqual(list(task.fields), ["name", "idea"])
        self.assertEqual(task.required_fields(), ["name", "idea"])
        self.assertLessEqual(len(task.required_fields()), 3)
        self.assertEqual(task.fields["name"]["label"], "Project name")
        self.assertEqual(task.fields["idea"]["label"], "One sentence about it")

    def test_the_gui_catalog_lists_it(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("new-project", listed)
        self.assertEqual(listed["new-project"]["profile"], "high-stakes")
        self.assertEqual(listed["new-project"]["takes"], "none")
        self.assertEqual([field["name"] for field in listed["new-project"]["fields"]],
                         ["name", "idea"])


class ValidationTests(unittest.TestCase):
    def test_a_named_idea_is_accepted(self):
        for data in ({"values": {"name": NAME, "idea": IDEA}},
                     {"name": NAME, "idea": IDEA},
                     {"repo": REPO, "values": {"name": NAME, "idea": IDEA}}):
            with self.subTest(data=data):
                self.assertIsNone(new_project.validate(data))

    def test_a_missing_or_broken_name_is_refused(self):
        for name in ("", "   ", "no/slashes", "x" * 81):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    new_project.validate({"values": {"name": name,
                                                     "idea": IDEA}})
                self.assertIn("name", str(caught.exception).lower())

    def test_a_missing_idea_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            new_project.validate({"values": {"name": NAME, "idea": "  "}})
        self.assertIn("sentence", str(caught.exception))

    def test_an_unreasonably_long_idea_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            new_project.validate({"values": {"name": NAME,
                                             "idea": "x" * 501}})
        self.assertIn("500", str(caught.exception))


class InstructionTests(unittest.TestCase):
    def test_instruction_names_every_starter_file_and_the_plan_first_line(self):
        text = new_project.build_instruction(
            {"repo": REPO, "values": {"name": NAME, "idea": IDEA}})
        self.assertIn(NAME, text)
        self.assertIn("README.md", text)
        self.assertIn(".gitignore", text)
        self.assertIn("LICENSE", text)
        self.assertIn("MIT", text)
        self.assertIn("the repository owner", text)
        self.assertIn("tests/test_smoke.py", text)
        self.assertIn("unittest", text)
        self.assertIn(new_project.PLAN_FILE, text)
        self.assertIn(f"Create {NAME}", text)
        self.assertIn(IDEA, text)
        self.assertIn("do not stage, commit, push", text)

    def test_python_is_the_default_but_the_idea_can_choose_another_language(self):
        python = new_project.build_instruction({"values": {"name": NAME}})
        self.assertIn("tests/test_smoke.py", python)

        typed = new_project.build_instruction(
            {"values": {"name": NAME, "idea": "A TypeScript dashboard."}})
        self.assertIn("TypeScript", typed)
        self.assertNotIn("tests/test_smoke.py", typed)


class DraftExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(Path(temp.name) / "worktrees")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_draft_run_waits_for_the_person_and_touches_no_remote(self):
        agent = PlanAgent()
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
        self.assertEqual([d["name"] for d in drafts], ["plan"])
        self.assertEqual(drafts[0]["title"], f"Create {NAME}")
        self.assertIn("README.md", drafts[0]["body"])

        instruction = agent.instructions[0]
        self.assertIn(NAME, instruction)
        self.assertIn(IDEA, instruction)
        self.assertIn(new_project.PLAN_FILE, instruction)

        self.assertEqual(wt.calls, [])          # no worktree checkout, no commit
        self.assertEqual(gh.rest_calls, [])
        self.assertEqual(gh_rec.calls, [])
        # The scratch repository's empty first commit is expected; staging and
        # pushing belong to the confirmed Post action, not the draft run.
        self.assertFalse([c for c in git_rec.calls
                          if "add" in c[0] or "push" in c[0]])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.worktree = Path(temp.name) / "wt"
        self.worktree.mkdir()
        (self.worktree / "README.md").write_text("# fresh-start\n", encoding="utf-8")
        (self.worktree / "tests").mkdir()
        (self.worktree / "tests" / "test_smoke.py").write_text(
            "import unittest\n", encoding="utf-8")
        (self.worktree / new_project.DRAFT_DIR).mkdir()
        (self.worktree / new_project.PLAN_FILE).write_text(PLAN, encoding="utf-8")

    def context(self, **over):
        data = {"repo": REPO, "number": 0,
                "draft": {"name": "plan", "title": f"Create {NAME}", "body": PLAN},
                "values": {"name": NAME, "idea": IDEA},
                "worktree": str(self.worktree)}
        data.update(over)
        return data

    def run_apply(self, git=None, gh=None, context=None, job=None):
        git = git or FakeGitRun()
        gh = gh or FakeGhRun()
        with patch.object(new_project.subprocess, "run", git), \
                patch.object(responder, "_run_gh", gh):
            outcome = new_project.TASK.apply(context or self.context(), None,
                                             job or make_job())
        return outcome, git, gh

    def test_commit_is_clean_and_repo_create_is_explicit(self):
        outcome, git, gh = self.run_apply()

        self.assertEqual(git.calls[0], (["git", "add", "-A"], str(self.worktree)))
        resets = git.commands("reset")
        self.assertEqual(resets,
                         [["git", "reset", "-q", "HEAD", "--", ".gha-drafts"]])
        commits = git.commands("commit")
        self.assertEqual(commits, [["git", "commit", "-m",
                                    "feat: initial project scaffold"]])
        order = [args[1] for args, _ in git.calls]
        self.assertLess(order.index("reset"), order.index("commit"))

        creates = gh.creates()
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0], ["repo", "create", REPO, "--private",
                                      "--source", str(self.worktree), "--push"])
        self.assertNotIn("force", " ".join(creates[0]).lower())
        self.assertNotIn("force", " ".join(commits[0]).lower())
        self.assertEqual(outcome, {"result": f"Created {REPO} — README, license, "
                                             "and starter tests are in place."})

    def test_without_drafts_no_reset_runs(self):
        (self.worktree / new_project.PLAN_FILE).unlink()
        (self.worktree / new_project.DRAFT_DIR).rmdir()
        outcome, git, gh = self.run_apply()
        self.assertEqual(git.commands("reset"), [])
        self.assertEqual(len(gh.creates()), 1)
        self.assertIn("Created", outcome["result"])

    def test_missing_worktree_raises_one_plain_sentence(self):
        git, gh = FakeGitRun(), FakeGhRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, gh=gh,
                           context=self.context(worktree="/nonexistent/wt"))
        message = str(caught.exception)
        self.assertIn("scratch folder", message)
        self.assertNotIn("Traceback", message)
        self.assertEqual(git.calls, [])
        self.assertEqual(gh.calls, [])

    def test_missing_worktree_falls_back_to_job_notes(self):
        job = make_job(notes={"values": {"name": NAME, "idea": IDEA},
                              "worktree_path": str(self.worktree)})
        context = self.context()
        context.pop("worktree")
        outcome, git, gh = self.run_apply(context=context, job=job)
        self.assertIn("Created", outcome["result"])
        self.assertEqual(len(gh.creates()), 1)

    def test_unstage_failure_stops_before_commit_and_create(self):
        git = FakeGitRun(failing={"reset": (1, "", "fatal: pathspec error")})
        gh = FakeGhRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, gh=gh)
        self.assertIn("draft notes", str(caught.exception))
        self.assertEqual(git.commands("commit"), [])
        self.assertEqual(gh.calls, [])

    def test_commit_failure_creates_nothing_on_github(self):
        git = FakeGitRun(failing={"commit": (1, "", "fatal: nothing to commit")})
        gh = FakeGhRun()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(git=git, gh=gh)
        message = str(caught.exception)
        self.assertIn("could not be committed", message)
        self.assertEqual(gh.calls, [])

    def test_repo_create_refusal_reads_as_a_plain_sentence(self):
        gh = FakeGhRun(rc=1, out="", err="repository already exists")
        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=gh)
        message = str(caught.exception)
        self.assertIn("repository already exists", message)
        self.assertIn("Nothing was pushed", message)
        self.assertEqual(len(gh.creates()), 1)

    def test_gh_that_cannot_start_reads_as_a_plain_sentence(self):
        def broken(args, timeout=120.0):
            raise OSError("gh is not installed")

        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=broken)
        message = str(caught.exception)
        self.assertIn("could not run", message)
        self.assertIn("gh is not installed", message)
        self.assertNotIn("Traceback", message)

    def test_repo_is_built_from_the_name_when_context_lacks_one(self):
        context = self.context()
        context.pop("repo")
        outcome, _, gh = self.run_apply(context=context)
        self.assertEqual(gh.creates()[0][2], f"example-owner/{NAME}")
        self.assertIn(f"Created example-owner/{NAME}", outcome["result"])


if __name__ == "__main__":
    unittest.main()
