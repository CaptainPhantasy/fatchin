"""review-pr: registry entry, input rules, draft flow, and the one POST it makes."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ghagent import tasks
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.tasks import review_pr

from tests.test_executor_flow import (FakeWT, agent_result, default_cfg,
                                      happy_gh, make_job, run_job)
from tests.test_web_control import GuiFixture

REPO = "o/r"
NUM = 12


class RecordingGh:
    """Records every REST call; returns one canned review payload."""

    def __init__(self, response=None):
        self.calls = []
        self.response = {"id": 55, "state": "COMMENTED"} if response is None \
            else response

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append((method, path, body))
        return self.response


def review_context(body="The change is small and safe.", note=None):
    values = {} if note is None else {"note": note}
    return {"repo": REPO, "number": NUM, "values": values,
            "draft": {"name": "review", "title": "Small and safe", "body": body}}


def review_job(number=NUM):
    return Job(job_id="job_rv", kind=JobKind.REVIEW_PR, repo_full_name=REPO,
               number=number, command_comment_id=0, requested_by=1,
               dedup_key="review-pr:o:r:12:x")


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_registry_exposes_review_pr_as_a_draft_that_takes_a_pull_request(self):
        task = tasks.get("review-pr")
        self.assertIsNotNone(task)
        self.assertEqual(task.id, "review-pr")
        self.assertEqual(task.profile, "draft")
        self.assertEqual(task.takes, "pr")
        self.assertTrue(task.label.strip())
        self.assertTrue(task.help.strip())
        self.assertIs(task.build_instruction, review_pr.build_instruction)
        self.assertIs(task.validate, review_pr.validate)
        self.assertIs(task.apply, review_pr.apply)
        self.assertFalse(task.post_confirm_required)

    def test_at_most_one_required_input_and_none_are_required(self):
        task = tasks.get("review-pr")
        self.assertLessEqual(len(task.required_fields()), 1)
        self.assertEqual(task.required_fields(), [])
        self.assertEqual(list(task.fields), ["note"])
        self.assertFalse(task.fields["note"]["required"])
        self.assertTrue(task.fields["note"]["label"].strip())

    def test_catalog_entry_is_plain_and_complete(self):
        entry = next(i for i in tasks.catalog() if i["id"] == "review-pr")
        self.assertEqual(entry["takes"], "pr")
        self.assertEqual(entry["profile"], "draft")
        self.assertEqual([f["name"] for f in entry["fields"]], ["note"])
        self.assertFalse(entry["fields"][0]["required"])


class ValidateTests(unittest.TestCase):
    def data(self, **over):
        value = {"repo": REPO, "number": NUM, "values": {"note": ""}}
        value.update(over)
        return value

    def test_accepts_a_pull_request_with_no_note(self):
        self.assertIsNone(review_pr.validate(self.data()))
        self.assertIsNone(review_pr.validate(self.data(values={})))

    def test_accepts_a_note(self):
        self.assertIsNone(review_pr.validate(
            self.data(values={"note": "check the retry loop"})))

    def test_blank_optional_note_is_not_an_error(self):
        for note in ("", "   ", None):
            with self.subTest(note=note):
                self.assertIsNone(
                    review_pr.validate(self.data(values={"note": note})))

    def test_rejects_a_missing_or_nonsense_pull_request_number(self):
        for number in (0, None, "", "abc", -3):
            with self.subTest(number=number), \
                    self.assertRaises(ValueError) as caught:
                review_pr.validate(self.data(number=number))
            self.assertIn("Choose the pull request", str(caught.exception))


class InstructionTests(unittest.TestCase):
    def context(self, **values):
        return {"repo": REPO, "number": NUM, "values": values,
                "base_branch": "main", "worktree": "/tmp/wt"}

    def test_instruction_asks_for_plain_words_checks_and_the_draft_file(self):
        text = review_pr.build_instruction(self.context())
        self.assertIn(f"pull request #{NUM}", text)
        self.assertIn(".gha-drafts/review.md", text)
        self.assertIn("first-time GitHub user", text)
        self.assertIn("path:line", text)
        self.assertIn(f"gh pr diff {NUM} --repo {REPO}", text)
        self.assertIn(f"gh pr checks {NUM} --repo {REPO}", text)
        self.assertIn("Whether the repository's checks pass", text)
        self.assertIn("Never approve or request changes", text)
        self.assertIn("first line is a short title", text)

    def test_the_person_note_is_carried_into_the_instruction(self):
        text = review_pr.build_instruction(
            self.context(note="  focus on the  retry loop "))
        self.assertIn("focus on the retry loop", text)
        self.assertNotIn("focus on the  retry", text)

    def test_no_note_means_no_invented_focus(self):
        text = review_pr.build_instruction(self.context())
        self.assertNotIn("pay particular attention to", text)

    def test_instruction_survives_an_empty_context(self):
        self.assertIsInstance(review_pr.build_instruction({}), str)


class DraftingAgent:
    """Writes the review draft into the worktree, like the real agent would."""

    def __init__(self, text):
        self.text = text
        self.wt = None
        self.instructions = []

    def __call__(self, worktree_path=None, checks=None):
        self.wt = worktree_path
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        folder = Path(self.wt) / ".gha-drafts"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "review.md").write_text(self.text, encoding="utf-8")
        return agent_result(changed=[])


class DraftFlowTests(unittest.TestCase):
    DRAFT = ("Explain the retry change\nThe retry loop now waits twice as long "
             "before giving up (`src/retry.py:41`).")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # FakeWT hands back whatever add_worktree is given, so the worktree
        # root itself must be temporary: a leftover .gha-drafts/ would leak
        # between tests.
        self.cfg = replace(default_cfg(), worktree_root=self._tmp.name)

    def test_draft_run_waits_for_the_person_and_writes_nothing_remotely(self):
        agent = DraftingAgent(self.DRAFT)
        gh = happy_gh()
        job = make_job(kind=JobKind.REVIEW_PR, number=NUM,
                       notes={"values": {"note": "check the retry path"}})
        returned, queue, wt, agent, git_rec, gh_rec = run_job(
            job, gh, cfg=self.cfg, agent=agent)

        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        added = [c for c in wt.calls if c[0] == "add_worktree"]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0][1].startswith(self._tmp.name))
        self.assertEqual(returned.notes["drafts"][0]["name"], "review")
        self.assertEqual(returned.notes["drafts"][0]["title"],
                         "Explain the retry change")
        self.assertIn("waits twice as long",
                      returned.notes["drafts"][0]["body"])
        self.assertIn("gha-drafts/review.md", agent.instructions[0])
        self.assertIn("check the retry path", agent.instructions[0])

        states = [s for s, _ in queue.history]
        self.assertIn("working", states)
        self.assertIn("waiting-on-you", states)
        self.assertNotIn("responding", states)   # nothing reaches GitHub

        self.assertEqual([c for c in wt.calls if c[0] == "commit_all"], [])
        self.assertEqual(git_rec.calls, [])      # no push
        self.assertEqual(gh_rec.calls, [])       # no gh CLI writes
        self.assertEqual([c for c in gh.rest_calls if c[0] == "POST"], [])

    def test_missing_draft_file_fails_without_posting(self):
        agent = DraftingAgent("")
        agent.run = lambda instruction: agent_result(changed=[])
        gh = happy_gh()
        job = make_job(kind=JobKind.REVIEW_PR, number=NUM, notes={"values": {}})
        returned, queue, wt, agent, git_rec, gh_rec = run_job(
            job, gh, cfg=self.cfg, agent=agent)
        self.assertEqual(returned.state, JobState.FAILED)
        self.assertIn("no candidate texts", returned.error)
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh_rec.calls, [])


class ApplyTests(unittest.TestCase):
    def test_posts_exactly_one_comment_review_and_returns_the_result(self):
        gh = RecordingGh()
        result = review_pr.apply(review_context(), gh, review_job())
        self.assertEqual(len(gh.calls), 1)
        method, path, body = gh.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"repos/{REPO}/pulls/{NUM}/reviews")
        self.assertEqual(body["body"], "The change is small and safe.")
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(result, {"result": f"Posted review comment on PR #{NUM}"})

    def test_never_approves_or_requests_changes(self):
        gh = RecordingGh()
        review_pr.apply(review_context(body="APPROVE: looks great, ship it"),
                        gh, review_job())
        self.assertEqual(gh.calls[0][2]["event"], "COMMENT")
        self.assertNotIn(gh.calls[0][2]["event"], {"APPROVE", "REQUEST_CHANGES"})

    def test_empty_comment_posts_nothing(self):
        gh = RecordingGh()
        with self.assertRaises(ValueError) as caught:
            review_pr.apply(review_context(body="   "), gh, review_job())
        self.assertIn("nothing was posted", str(caught.exception))
        self.assertEqual(gh.calls, [])

    def test_unknown_pull_request_posts_nothing(self):
        gh = RecordingGh()
        context = review_context()
        context["number"] = 0
        with self.assertRaises(ValueError):
            review_pr.apply(context, gh, review_job(number=0))
        self.assertEqual(gh.calls, [])


class PostThroughWebControlTests(GuiFixture, unittest.TestCase):
    """The real GUI path: the approved draft reaches GitHub through apply()."""

    def setUp(self):
        self.build_gui()
        self.gh = RecordingGh()
        self.control.gh = self.gh

    def test_post_sends_one_comment_review_and_finishes_the_job(self):
        job = Job(job_id="job_rv", kind=JobKind.REVIEW_PR,
                  repo_full_name="owner/repo", number=7, command_comment_id=0,
                  requested_by=42, dedup_key="review-pr:owner/repo:7:x",
                  state=JobState.WAITING_ON_YOU,
                  notes={"drafts": [{"name": "review",
                                     "title": "Small and safe",
                                     "body": "One comment, in plain words."}],
                         "output_dir": str(self.output), "member": "alice",
                         "values": {"note": ""}})
        self.assertTrue(self.control.queue().add(job))

        result = self.control.draft_action("job_rv", {"action": "post",
                                                      "name": "review"})

        self.assertEqual(result["job"]["state"], "done")
        self.assertEqual(len(self.gh.calls), 1)
        method, path, body = self.gh.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "repos/owner/repo/pulls/7/reviews")
        self.assertEqual(body["event"], "COMMENT")
        self.assertEqual(body["body"], "One comment, in plain words.")
        self.assertIn("Posted review comment on PR #7", result["hint"])


if __name__ == "__main__":
    unittest.main()
