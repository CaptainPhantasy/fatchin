"""triage-issues task type: registry contract, validation, draft run, one POST.

Offline only: the GitHub client is a recording fake and the coding agent is a
stub that writes files, so no network and no model are ever touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ghagent import tasks
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.tasks import triage_issues
from ghagent.tasks.types import PROFILES
from ghagent.web_control import ApiError

from tests.test_executor_flow import FakeWT, agent_result, default_cfg, happy_gh, run_job
from tests.test_web_control import GuiFixture

WORDS = "The contact form loses messages. Also the Save button does nothing."


class RecordingGH:
    """Stands in for GitHubClient: records every REST call, sends nothing."""

    def __init__(self, created=None, error=None):
        self.created = ({"number": 31, "title": "Contact form loses messages"}
                        if created is None else created)
        self.error = error
        self.calls = []

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append({"method": method, "path": path, "body": body,
                           "paginate": paginate})
        if self.error:
            raise self.error
        return self.created


class DraftingAgent:
    """Coding-agent factory stub: writes the drafts the model would have written."""

    def __init__(self, files):
        self.files = files
        self.instructions = []

    def __call__(self, worktree_path=None, checks=None):
        self.worktree = worktree_path
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        folder = Path(self.worktree) / ".gha-drafts"
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in self.files.items():
            (folder / name).write_text(text, encoding="utf-8")
        return agent_result()


def draft_context(title="Contact form loses messages", body="Steps and done."):
    return {"repo": "owner/repo", "number": 0,
            "draft": {"name": "contact-form", "title": title, "body": body},
            "values": {"words": WORDS}}


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["triage-issues"]
        self.assertIs(task, triage_issues.TASK)
        self.assertEqual(task.id, "triage-issues")
        self.assertEqual(task.profile, "draft")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")
        self.assertEqual(task.label, "Turn your words into issues")
        self.assertIn("issue", task.help.lower())
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))
        self.assertTrue(callable(task.apply))

    def test_one_required_input_and_it_is_words(self):
        task = triage_issues.TASK
        self.assertEqual(task.required_fields(), ["words"])
        self.assertLessEqual(len(task.required_fields()), 1)
        self.assertTrue(task.fields["words"]["required"])

    def test_the_gui_catalog_lists_it(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("triage-issues", listed)
        entry = listed["triage-issues"]
        self.assertEqual(entry["profile"], "draft")
        self.assertEqual(entry["takes"], "repo")
        self.assertEqual([field["name"] for field in entry["fields"]], ["words"])


class ValidationTests(unittest.TestCase):
    def test_empty_words_are_refused_with_a_plain_instruction(self):
        for values in ({}, {"words": ""}, {"words": "   \n\t "}):
            with self.subTest(values=values):
                with self.assertRaises(ApiError) as caught:
                    triage_issues.validate({"repo": "o/r", "number": 0,
                                            "values": values})
                self.assertIn("in your own words", str(caught.exception))
                self.assertEqual(caught.exception.status, 400)

    def test_missing_values_block_is_refused_not_crashed(self):
        for data in ({"repo": "o/r"}, None, {}):
            with self.subTest(data=data), self.assertRaises(ApiError):
                triage_issues.validate(data)

    def test_real_words_pass(self):
        self.assertIsNone(triage_issues.validate(
            {"repo": "o/r", "number": 0, "values": {"words": WORDS}}))


class InstructionTests(unittest.TestCase):
    def test_instruction_carries_the_words_and_the_draft_layout(self):
        text = triage_issues.build_instruction(
            {"repo": "owner/repo", "values": {"words": WORDS}})
        self.assertIn(WORDS, text)
        self.assertIn("owner/repo", text)
        self.assertIn(".gha-drafts/", text)
        self.assertIn("first line", text)
        self.assertIn("file and line", text)
        self.assertIn("first-time GitHub user", text)
        self.assertIn("what 'done' means", text)
        self.assertIn("Do not post, close, edit, or delete", text)

    def test_instruction_without_values_still_reads(self):
        text = triage_issues.build_instruction({"repo": "owner/repo"})
        self.assertIn("owner/repo", text)
        self.assertIn(".gha-drafts/", text)


class DraftExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(self.tmp / "worktrees")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def job(self):
        return Job(job_id="job_triage", kind=JobKind.TRIAGE_ISSUES,
                   repo_full_name="o/r", number=0, command_comment_id=0,
                   requested_by=1, dedup_key="k",
                   notes={"values": {"words": WORDS}})

    def test_draft_run_waits_for_the_person_and_touches_no_remote(self):
        agent = DraftingAgent({
            "b-save-button.md": "Save button does nothing\nThe Save button on the "
                                "profile page has no effect.\n\nDone when saving "
                                "shows a confirmation.\n",
            "a-contact-form.md": "Contact form loses messages\nSubmitting the "
                                 "contact form reports success but no message "
                                 "arrives.\n\nDone when a test message arrives.\n",
        })
        gh = happy_gh()
        returned, queue, wt, _, git_rec, _ = run_job(
            self.job(), gh, cfg=self.cfg, wt=FakeWT(), agent=agent)

        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertEqual(returned.notes["phase"], "waiting-on-you")
        states = [state for state, _ in queue.history]
        self.assertIn("working", states)
        self.assertEqual(states[-1], "waiting-on-you")
        self.assertNotIn("responding", states)

        drafts = returned.notes["drafts"]
        self.assertEqual([d["name"] for d in drafts],
                         ["a-contact-form", "b-save-button"])
        self.assertEqual(drafts[0]["title"], "Contact form loses messages")
        self.assertIn("no message arrives", drafts[0]["body"])
        self.assertNotIn("Contact form loses messages", drafts[0]["body"])

        self.assertIn("one person's message", agent.instructions[0])
        self.assertIn(WORDS, agent.instructions[0])
        self.assertIn(".gha-drafts/", agent.instructions[0])

        self.assertFalse([c for c in wt.calls if c[0] == "commit_all"])
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])

    def test_a_run_without_drafts_fails_without_posting(self):
        gh = happy_gh()
        returned, _, wt, _, git_rec, _ = run_job(
            self.job(), gh, cfg=self.cfg, wt=FakeWT(),
            agent=DraftingAgent({}))
        self.assertEqual(returned.state, JobState.FAILED)
        self.assertIn("no candidate texts", returned.error)
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])


class ApplyTests(unittest.TestCase):
    def job(self):
        return Job(job_id="job_triage", kind=JobKind.TRIAGE_ISSUES,
                   repo_full_name="owner/repo", number=0, command_comment_id=0,
                   requested_by=1, dedup_key="k")

    def test_exactly_one_post_to_the_issues_collection(self):
        gh = RecordingGH()
        outcome = triage_issues.TASK.apply(
            draft_context(body="Submitting reports success but nothing arrives."),
            gh, self.job())

        self.assertEqual(len(gh.calls), 1)
        call = gh.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "repos/owner/repo/issues")
        self.assertEqual(call["body"],
                         {"title": "Contact form loses messages",
                          "body": "Submitting reports success but nothing arrives."})
        self.assertFalse(call["paginate"])
        self.assertEqual(outcome,
                         {"result": "Created issue #31 — Contact form loses messages"})

    def test_missing_title_refuses_before_any_remote_call(self):
        gh = RecordingGH()
        with self.assertRaises(ApiError) as caught:
            triage_issues.TASK.apply(draft_context(title="   "), gh, self.job())
        self.assertIn("no title", str(caught.exception))
        self.assertEqual(gh.calls, [])

    def test_unconfirmed_number_is_reported_plainly(self):
        gh = RecordingGH(created={})
        outcome = triage_issues.TASK.apply(draft_context(), gh, self.job())
        self.assertEqual(len(gh.calls), 1)
        self.assertIn("did not return its number", outcome["result"])


class PostThroughWebControlTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.gh_recorder = RecordingGH()
        self.control.gh = self.gh_recorder
        tasks.types._REGISTRY = None

    def tearDown(self):
        tasks.types._REGISTRY = None

    def waiting_job(self):
        job = Job(job_id="job_triage", kind=JobKind.TRIAGE_ISSUES,
                  repo_full_name="owner/repo", number=0, command_comment_id=0,
                  requested_by=42, dedup_key="triage-issues:owner/repo:0:x",
                  state=JobState.WAITING_ON_YOU,
                  notes={"drafts": [{"name": "contact-form",
                                     "title": "Contact form loses messages",
                                     "body": "Steps and done."}],
                         "output_dir": str(self.output), "member": "alice"})
        self.assertTrue(self.control.queue().add(job))
        return job

    def test_post_creates_one_issue_and_finishes_the_task(self):
        self.waiting_job()
        result = self.control.draft_action(
            "job_triage", {"action": "post", "name": "contact-form"})
        self.assertEqual(len(self.gh_recorder.calls), 1)
        self.assertEqual(self.gh_recorder.calls[0]["path"],
                         "repos/owner/repo/issues")
        self.assertEqual(result["job"]["state"], "done")
        self.assertIn("Created issue #31", result["hint"])

    def test_edit_and_discard_never_reach_github(self):
        self.waiting_job()
        self.control.draft_action("job_triage",
                                  {"action": "edit", "name": "contact-form",
                                   "body": "Sharper words."})
        self.assertEqual(self.gh_recorder.calls, [])
        self.control.draft_action("job_triage", {"action": "discard"})
        self.assertEqual(self.gh_recorder.calls, [])


if __name__ == "__main__":
    unittest.main()
