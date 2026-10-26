"""hygiene-sweep: registry contract, validation, and a read-only run.

Offline only: the executor runs against the fakes in
tests/test_executor_flow.py, so no network, no gh, no git, and no model are
ever touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ghagent import tasks
from ghagent.interfaces import AgentResult, Job, JobKind, JobState
from ghagent.tasks import hygiene_sweep
from ghagent.tasks.types import PROFILES

from tests.test_executor_flow import FakeWT, default_cfg, happy_gh, run_job

REPO = "owner/repo"
FINDINGS = ("1. Leaked secret — config/local.env:4 — move the file out of the "
            "repository and revoke the key.")


class ReportingAgent:
    """Coding-agent factory stub: reads, then reports; writes nothing."""

    def __init__(self, summary=FINDINGS):
        self.summary = summary
        self.instructions: list[str] = []
        self.worktree = None

    def __call__(self, worktree_path=None, checks=None):
        self.worktree = worktree_path
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        return AgentResult(summary=self.summary, changed_files=[],
                           worktree_path=self.worktree, model_calls_used=2)


def make_job(**over):
    data = dict(job_id="job_sweep", kind=JobKind.HYGIENE_SWEEP,
                repo_full_name=REPO, number=0, command_comment_id=0,
                requested_by=1, dedup_key="k", notes={"values": {}})
    data.update(over)
    return Job(**data)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["hygiene-sweep"]
        self.assertIs(task, hygiene_sweep.TASK)
        self.assertEqual(task.id, "hygiene-sweep")
        self.assertEqual(task.profile, "read-only")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")
        self.assertEqual(task.fields, {})
        self.assertIsNone(task.apply)
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))

    def test_it_requires_no_inputs_at_all(self):
        task = hygiene_sweep.TASK
        self.assertEqual(task.required_fields(), [])
        self.assertLessEqual(len(task.required_fields()), 1)

    def test_the_gui_catalog_lists_it_without_fields(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("hygiene-sweep", listed)
        entry = listed["hygiene-sweep"]
        self.assertEqual(entry["profile"], "read-only")
        self.assertEqual(entry["takes"], "repo")
        self.assertEqual(entry["fields"], [])
        self.assertIn("secret", entry["help"].lower())


class ValidationTests(unittest.TestCase):
    def test_every_input_shape_is_valid(self):
        for data in (None, {}, {"repo": REPO},
                     {"repo": REPO, "number": 0, "values": {}},
                     {"repo": REPO, "values": {"anything": "x"}}):
            with self.subTest(data=data):
                self.assertIsNone(hygiene_sweep.validate(data))


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.text = hygiene_sweep.build_instruction({"repo": REPO})

    def test_it_names_every_audit_area(self):
        self.assertIn(REPO, self.text)
        self.assertIn("secret", self.text.lower())
        self.assertIn("token", self.text.lower())
        self.assertIn(".env", self.text)
        self.assertIn("LICENSE", self.text)
        self.assertIn("README", self.text)
        self.assertIn("dependenc", self.text.lower())
        self.assertIn("git branch -a", self.text)
        self.assertIn("debug", self.text.lower())

    def test_every_finding_needs_a_path_line_and_a_fix(self):
        self.assertIn("path:line", self.text)
        self.assertIn("how to fix", self.text)
        self.assertIn("plain words", self.text)
        self.assertIn("first-time GitHub user", self.text)

    def test_it_forbids_installs_edits_and_network_use(self):
        self.assertIn("do not install anything", self.text.lower())
        self.assertIn("change nothing", self.text)
        self.assertIn("only reads", self.text)

    def test_instruction_without_a_repo_still_reads(self):
        text = hygiene_sweep.build_instruction({})
        self.assertIn("this repository", text)


class ReadOnlyExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(Path(temp.name) / "worktrees")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_run_reports_and_makes_zero_remote_or_commit_calls(self):
        tasks.types._REGISTRY = None
        agent = ReportingAgent()
        gh = happy_gh()
        returned, queue, wt, _, git_rec, gh_rec = run_job(
            make_job(), gh, cfg=self.cfg, wt=FakeWT(), agent=agent)

        self.assertEqual(returned.state, JobState.DONE, returned.error)
        self.assertEqual(returned.notes["phase"], "reporting")
        self.assertEqual(returned.notes["agent_summary"], FINDINGS)
        states = [state for state, _ in queue.history]
        self.assertIn("working", states)
        self.assertNotIn("responding", states)

        self.assertTrue(agent.instructions)
        self.assertIn(REPO, agent.instructions[0])
        self.assertIn("git branch -a", agent.instructions[0])
        self.assertIn("path:line", agent.instructions[0])

        self.assertFalse([c for c in wt.calls if c[0] == "commit_all"])
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])
        self.assertEqual(gh_rec.calls, [])
        self.assertEqual(returned.produced_commits, [])


if __name__ == "__main__":
    unittest.main()
