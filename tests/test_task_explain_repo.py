"""explain-repo: the read-only briefing task, tested offline.

No network, no real gh, no model: the executor runs against the fakes in
tests/test_executor_flow.py and the registry is read from the real module.
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from ghagent import tasks
from ghagent.interfaces import AgentResult, Job, JobKind, JobState
from ghagent.tasks import explain_repo
from ghagent.tasks.types import PROFILES
from ghagent.web_control import ApiError

from tests.test_executor_flow import FakeWT, default_cfg, happy_gh, run_job
from tests.test_web_control import GuiFixture

REPO = "owner/repo"
BRIEFING = ("This repository keeps shop orders in one small program. "
            "The entry point is src/main.py:1.")


def context(question=None, repo=REPO):
    values = {} if question is None else {"question": question}
    return {"repo": repo, "values": values, "worktree": "/tmp/wt",
            "base_branch": "main"}


def make_job(question="How does login work?", **notes_over):
    notes = {"values": {} if question is None else {"question": question}}
    notes.update(notes_over)
    return Job(job_id="job_explain", kind=JobKind.EXPLAIN_REPO,
               repo_full_name=REPO, number=0, command_comment_id=0,
               requested_by=1, dedup_key="explain-repo:owner/repo:0:x",
               notes=notes)


class BriefingAgent:
    """Writes proof into the scratch checkout and returns a plain summary."""

    def __init__(self, summary=BRIEFING):
        self.summary = summary
        self.instructions: list[str] = []
        self.wt = None
        self.instantiations = 0

    def __call__(self, worktree_path=None, checks=None):
        self.instantiations += 1
        self.wt = worktree_path
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        (Path(self.wt) / "notes.txt").write_text("a briefing", encoding="utf-8")
        return AgentResult(summary=self.summary, changed_files=[],
                           worktree_path=self.wt, model_calls_used=2)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def task(self):
        return tasks.load_registry()["explain-repo"]

    def test_discovered_with_read_only_profile(self):
        task = self.task()
        self.assertIs(task, explain_repo.TASK)
        self.assertEqual(task.id, "explain-repo")
        self.assertEqual(task.profile, "read-only")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")

    def test_label_and_help_are_plain_words(self):
        task = self.task()
        self.assertEqual(task.label, "Explain a repository")
        self.assertTrue(task.help)
        self.assertIn("plain words", task.help)
        self.assertIn("nothing on GitHub is changed", task.help)

    def test_catalog_exposes_the_question_as_optional(self):
        entry = next(item for item in tasks.catalog()
                     if item["id"] == "explain-repo")
        self.assertEqual(entry["profile"], "read-only")
        self.assertEqual(entry["takes"], "repo")
        self.assertEqual([field["name"] for field in entry["fields"]],
                         ["question"])
        self.assertFalse(entry["fields"][0]["required"])

    def test_at_most_one_required_input_and_none_here(self):
        task = self.task()
        self.assertLessEqual(len(task.required_fields()), 1)
        self.assertEqual(task.required_fields(), [])

    def test_read_only_profile_carries_no_apply(self):
        self.assertIsNone(self.task().apply)


class BuildInstructionTests(unittest.TestCase):
    def test_uses_the_person_s_question(self):
        text = explain_repo.build_instruction(
            context("Why do orders sometimes vanish?"))
        self.assertIn("Why do orders sometimes vanish?", text)
        self.assertNotIn(explain_repo.DEFAULT_OVERVIEW, text)

    def test_no_question_asks_for_the_default_overview(self):
        text = explain_repo.build_instruction(context())
        self.assertIn("what does this repository do", text)
        self.assertIn("how is it organized", text)
        self.assertIn("what changed recently", text)

    def test_blank_question_falls_back_to_the_overview(self):
        text = explain_repo.build_instruction(context("   "))
        self.assertIn(explain_repo.DEFAULT_OVERVIEW, text)

    def test_demands_file_and_line_citations(self):
        text = explain_repo.build_instruction(context())
        self.assertIn("file and the line", text)
        self.assertIn("src/example.py:42", text)

    def test_written_for_a_first_time_github_user(self):
        text = explain_repo.build_instruction(context())
        self.assertIn("never used GitHub", text)
        self.assertIn("everyday English", text)
        self.assertIn("does not read code", text)

    def test_names_the_repository_and_forbids_writes(self):
        text = explain_repo.build_instruction(context("What is this?"))
        self.assertIn(REPO, text)
        self.assertIn("Change no files", text)
        self.assertIn("post, push or comment nothing", text)


class ValidateTests(unittest.TestCase):
    def test_accepts_no_question(self):
        explain_repo.validate({"repo": REPO, "number": 0, "values": {}})
        explain_repo.validate({"repo": REPO, "number": 0,
                               "values": {"question": None}})

    def test_accepts_a_blank_optional_question(self):
        explain_repo.validate({"values": {"question": ""}})
        explain_repo.validate({"values": {"question": "   "}})

    def test_accepts_a_real_question(self):
        explain_repo.validate({"values": {"question": "How do refunds work?"}})

    def test_rejects_a_question_with_no_words(self):
        with self.assertRaises(ApiError) as caught:
            explain_repo.validate({"values": {"question": "???"}})
        self.assertIn("in your own words", str(caught.exception))
        self.assertEqual(caught.exception.status, 400)


class ScratchWT(FakeWT):
    """A fake checkout that really exists on disk, so the agent can write."""

    def add_worktree(self, path, base_ref, repo_full_name=None):
        super().add_worktree(path, base_ref, repo_full_name)
        Path(path).mkdir(parents=True, exist_ok=True)
        return path


class ExecutorFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = dataclasses.replace(default_cfg(),
                                       worktree_root=self._tmp.name)
        self.gh = happy_gh()
        self.agent = BriefingAgent()

    def run_explain(self, job=None, agent=None):
        return run_job(job or make_job(), self.gh, cfg=self.cfg,
                       wt=ScratchWT(), agent=agent or self.agent)

    def test_reaches_done_without_any_remote_write(self):
        job, queue, wt, agent, git_rec, gh_rec = self.run_explain()
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(gh_rec.calls, [])
        self.assertEqual(self.gh.rest_calls, [])
        self.assertEqual(git_rec.calls, [])
        self.assertFalse([call for call in wt.calls if call[0] == "commit_all"])

    def test_never_enters_verifying_or_responding(self):
        job, queue, *_ = self.run_explain()
        states = [state for state, _ in queue.history]
        self.assertIn("retrieving", states)
        self.assertIn("working", states)
        self.assertEqual(states[-1], "done")
        self.assertNotIn("verifying", states)
        self.assertNotIn("responding", states)

    def test_briefing_lands_in_the_task_record(self):
        job, *_ = self.run_explain()
        self.assertEqual(job.notes["agent_summary"], BRIEFING)
        self.assertEqual(job.model_calls_used, 2)
        self.assertIn("changes nothing on GitHub", job.notes["verification"])
        self.assertEqual(job.produced_commits, [])
        self.assertEqual(job.posted_comment_ids, [])

    def test_agent_receives_the_question_briefing(self):
        job, _, wt, agent, *_ = self.run_explain()
        self.assertEqual(len(agent.instructions), 1)
        self.assertIn("How does login work?", agent.instructions[0])
        self.assertEqual(agent.instructions[0],
                         explain_repo.build_instruction(
                             {"repo": REPO, "worktree": agent.wt,
                              "base_branch": "main",
                              "values": job.notes["values"],
                              "title": ""}))
        self.assertIn(("add_worktree", agent.wt, "main"), wt.calls)

    def test_overview_briefing_when_no_question_given(self):
        job, _, _, agent, *_ = self.run_explain(make_job(question=None))
        self.assertIn(explain_repo.DEFAULT_OVERVIEW, agent.instructions[0])
        self.assertNotIn("The person asked, in their own words",
                         agent.instructions[0])

    def test_scratch_checkout_is_removed_after_the_report(self):
        job, _, wt, agent, *_ = self.run_explain()
        created = [call for call in wt.calls if call[0] == "add_worktree"][0]
        self.assertEqual(created[1], agent.wt)
        self.assertTrue(Path(agent.wt, "notes.txt").exists())
        self.assertIn(("remove_worktree", agent.wt), wt.calls)
        self.assertIn(("prune",), wt.calls)
        self.assertNotIn("worktree_path", job.notes)

    def test_agent_error_fails_without_naming_a_github_change(self):
        agent = BriefingAgent(summary=BRIEFING)
        agent.run = lambda instruction: AgentResult(
            summary="", changed_files=[], worktree_path=self._tmp.name,
            error="model stopped early")
        job, queue, _, _, git_rec, gh_rec = self.run_explain(agent=agent)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("model stopped early", job.error)
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh_rec.calls, [])
        self.assertEqual(self.gh.rest_calls, [])


class EnqueueTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()

    def test_repo_level_task_enqueues_with_the_question(self):
        job = self.control.enqueue(self.task_data(
            kind="explain-repo", number=None,
            question="Where does login live?"))["job"]
        self.assertEqual(job["number"], 0)
        self.assertEqual(job["kind"], "explain-repo")
        self.assertEqual(job["notes"]["values"],
                         {"question": "Where does login live?"})
        self.assertEqual(job["notes"]["task_type"], "explain-repo")

    def test_blank_question_is_accepted(self):
        job = self.control.enqueue(self.task_data(
            kind="explain-repo", number=None, question=""))["job"]
        self.assertEqual(job["notes"]["values"]["question"], "")

    def test_question_with_no_words_is_refused_before_queueing(self):
        with self.assertRaises(ApiError) as caught:
            self.control.enqueue(self.task_data(
                kind="explain-repo", number=None, question="???"))
        self.assertIn("in your own words", str(caught.exception))
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.control.queue().all(), [])


if __name__ == "__main__":
    unittest.main()
