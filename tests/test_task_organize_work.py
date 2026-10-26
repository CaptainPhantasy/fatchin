"""organize-work: registry contract, validation, draft run, label creation.

Offline only: the GitHub client is a recording fake, git and gh never run, and
the coding agent is a stub that writes the plan file. No network is touched and
no real repository is changed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ghagent import tasks
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.tasks import organize_work
from ghagent.tasks.types import PROFILES

from tests.test_executor_flow import FakeWT, agent_result, default_cfg, happy_gh, run_job

REPO = "owner/repo"

PLAN = (
    "Organise this repository\n"
    "LABEL: bug — something in the product is broken.\n"
    "LABEL: needs-triage — nobody has decided what it is yet.\n"
    "LABEL: documentation — the words need writing or fixing.\n"
    "\n"
    "Assigned:\n"
    "bug: #3, #9\n"
    "needs-triage: #11\n"
    "documentation: #9\n"
)


class RecordingGH:
    """Stands in for GitHubClient: records every REST call, sends nothing."""

    def __init__(self, existing=None, get_error=None, post_error=None):
        self.existing = ([{"name": "bug"}]
                         if existing is None else existing)
        self.get_error = get_error
        self.post_error = post_error
        self.calls = []

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append({"method": method, "path": path, "body": body,
                           "paginate": paginate})
        if method == "GET":
            if self.get_error:
                raise self.get_error
            return self.existing
        if self.post_error:
            raise self.post_error
        return {"name": (body or {}).get("name")}

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


class DraftingAgent:
    """Coding-agent factory stub: writes the plan the model would have written."""

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


def make_job(**over):
    data = dict(job_id="job_organize", kind=JobKind.ORGANIZE_WORK,
                repo_full_name=REPO, number=0, command_comment_id=0,
                requested_by=1, dedup_key="k", notes={"values": {}})
    data.update(over)
    return Job(**data)


def draft_context(body=PLAN, title=organize_work.PLAN_TITLE):
    return {"repo": REPO, "number": 0,
            "draft": {"name": "plan", "title": title, "body": body},
            "values": {}}


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["organize-work"]
        self.assertIs(task, organize_work.TASK)
        self.assertEqual(task.id, "organize-work")
        self.assertEqual(task.profile, "draft")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")
        self.assertFalse(task.post_confirm_required)
        self.assertIn("label", task.label.lower())
        self.assertIn("issue", task.help.lower())
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))
        self.assertTrue(callable(task.apply))

    def test_it_asks_for_no_extra_inputs_and_stays_within_the_cap(self):
        task = organize_work.TASK
        self.assertEqual(task.fields, {})
        self.assertEqual(task.required_fields(), [])
        self.assertLessEqual(len(task.required_fields()), 3)

    def test_the_gui_catalog_lists_it(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("organize-work", listed)
        entry = listed["organize-work"]
        self.assertEqual(entry["profile"], "draft")
        self.assertEqual(entry["takes"], "repo")
        self.assertEqual(entry["fields"], [])


class ValidationTests(unittest.TestCase):
    def test_a_repository_wide_plan_accepts_every_input(self):
        for data in (None, {}, {"repo": REPO}, {"repo": REPO, "values": {}},
                     {"repo": REPO, "values": {"anything": "goes"}}):
            with self.subTest(data=data):
                self.assertIsNone(organize_work.validate(data))


class InstructionTests(unittest.TestCase):
    def test_instruction_reads_the_issues_and_fixes_the_plan_layout(self):
        text = organize_work.build_instruction({"repo": REPO})
        self.assertIn(REPO, text)
        self.assertIn("gh issue list", text)
        self.assertIn("--state open", text)
        self.assertIn(organize_work.DRAFT_FILE, text)
        self.assertIn(organize_work.PLAN_TITLE, text)
        self.assertIn("first line", text)
        self.assertIn("LABEL: <name> — <one-line description>", text)
        self.assertIn("Assigned:", text)
        self.assertIn("Do not create labels", text)
        self.assertIn("close, reopen, label, or edit", text)

    def test_instruction_without_a_repository_still_reads(self):
        text = organize_work.build_instruction({})
        self.assertIn("this repository", text)
        self.assertIn(organize_work.DRAFT_FILE, text)


class LabelParsingTests(unittest.TestCase):
    def test_label_lines_keep_order_and_split_name_from_description(self):
        self.assertEqual(organize_work._labels_from_body(
            "Title\nLABEL: bug — broken.\nLABEL: good-first-issue – small job.\n"),
            [("bug", "broken."), ("good-first-issue", "small job.")])

    def test_other_lines_and_repeats_are_ignored(self):
        labels = organize_work._labels_from_body(
            "Assigned:\nbug: #1\nLABEL: bug — broken.\nLABEL: Bug — again.\n")
        self.assertEqual(labels, [("bug", "broken.")])

    def test_a_label_without_a_description_is_kept(self):
        self.assertEqual(organize_work._labels_from_body("LABEL: cleanup\n"),
                         [("cleanup", "")])


class DraftExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(self.tmp / "worktrees")
        (self.tmp / "wt").mkdir()

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_draft_run_waits_for_the_person_and_touches_no_remote(self):
        agent = DraftingAgent({"plan.md": PLAN})
        gh = happy_gh()
        returned, queue, wt, _, git_rec, _ = run_job(
            make_job(), gh, cfg=self.cfg, wt=FakeWT(path=str(self.tmp / "wt")),
            agent=agent)

        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertEqual(returned.notes["phase"], "waiting-on-you")
        states = [state for state, _ in queue.history]
        self.assertIn("working", states)
        self.assertEqual(states[-1], "waiting-on-you")
        self.assertNotIn("responding", states)

        drafts = returned.notes["drafts"]
        self.assertEqual([d["name"] for d in drafts], ["plan"])
        self.assertEqual(drafts[0]["title"], organize_work.PLAN_TITLE)
        self.assertIn("LABEL: bug", drafts[0]["body"])
        self.assertIn("Assigned:", drafts[0]["body"])
        self.assertNotIn(organize_work.PLAN_TITLE, drafts[0]["body"])

        self.assertIn("gh issue list", agent.instructions[0])
        self.assertIn(organize_work.DRAFT_FILE, agent.instructions[0])
        self.assertFalse([c for c in wt.calls if c[0] == "commit_all"])
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])


class ApplyTests(unittest.TestCase):
    def test_only_missing_labels_are_created_with_their_plan_colours(self):
        gh = RecordingGH(existing=[{"name": "bug"}, {"name": "Documentation"}])
        outcome = organize_work.TASK.apply(draft_context(), gh, make_job())

        self.assertEqual(gh.calls[0],
                         {"method": "GET", "path": f"repos/{REPO}/labels",
                          "body": {"per_page": 100}, "paginate": True})
        self.assertEqual(gh.posts(), [
            {"method": "POST", "path": f"repos/{REPO}/labels",
             "body": {"name": "needs-triage",
                      "description": "nobody has decided what it is yet.",
                      "color": organize_work.COLORS[1]},
             "paginate": False},
        ])
        self.assertEqual(outcome, {
            "result": "Created 1 labels. Assigning labels to issues stays with "
                      "you; the plan lists every match."})

    def test_a_plan_of_known_labels_creates_nothing(self):
        gh = RecordingGH(existing=[{"name": "bug"}, {"name": "needs-triage"},
                                   {"name": "documentation"}])
        outcome = organize_work.TASK.apply(draft_context(), gh, make_job())
        self.assertEqual(gh.posts(), [])
        self.assertEqual(len(gh.calls), 1)
        self.assertIn("Created 0 labels.", outcome["result"])

    def test_every_label_line_is_created_in_order(self):
        gh = RecordingGH(existing=[])
        body = ("Organise this repository\n"
                "LABEL: bug — broken.\n"
                "LABEL: docs — words.\n")
        organize_work.TASK.apply(draft_context(body=body), gh, make_job())
        self.assertEqual([c["body"]["name"] for c in gh.posts()],
                         ["bug", "docs"])
        self.assertEqual([c["body"]["color"] for c in gh.posts()],
                         [organize_work.COLORS[0], organize_work.COLORS[1]])

    def test_no_issue_is_closed_labelled_or_edited(self):
        gh = RecordingGH(existing=[])
        organize_work.TASK.apply(draft_context(), gh, make_job())
        for call in gh.calls:
            self.assertNotIn("/issues", call["path"])
            self.assertNotIn("/pulls", call["path"])
        self.assertEqual({c["method"] for c in gh.calls}, {"GET", "POST"})

    def test_a_plan_without_label_lines_refuses_before_any_remote_call(self):
        gh = RecordingGH()
        with self.assertRaises(ValueError) as caught:
            organize_work.TASK.apply(
                draft_context(body="Organise this repository\nAssigned:\n"
                                   "bug: #3\n"), gh, make_job())
        self.assertIn("lists no labels", str(caught.exception))
        self.assertEqual(gh.calls, [])

    def test_unreadable_existing_labels_stop_with_one_plain_sentence(self):
        gh = RecordingGH(get_error=RuntimeError("gh api failed: 404 Not Found"))
        with self.assertRaises(ValueError) as caught:
            organize_work.TASK.apply(draft_context(), gh, make_job())
        message = str(caught.exception)
        self.assertIn("could not be read", message)
        self.assertIn("404 Not Found", message)
        self.assertEqual(gh.posts(), [])

    def test_a_refused_label_names_what_was_already_created(self):
        gh = RecordingGH(existing=[], post_error=RuntimeError("422 already_exists"))
        with self.assertRaises(ValueError) as caught:
            organize_work.TASK.apply(draft_context(), gh, make_job())
        message = str(caught.exception)
        self.assertIn("refused the label", message)
        self.assertIn("0 label(s) were created", message)
        self.assertEqual(len(gh.posts()), 1)

    def test_a_missing_repository_is_refused_plainly(self):
        gh = RecordingGH()
        context = draft_context()
        context.pop("repo")
        with self.assertRaises(ValueError) as caught:
            organize_work.TASK.apply(context, gh, make_job(repo_full_name=""))
        self.assertIn("could not be identified", str(caught.exception))
        self.assertEqual(gh.calls, [])


if __name__ == "__main__":
    unittest.main()
