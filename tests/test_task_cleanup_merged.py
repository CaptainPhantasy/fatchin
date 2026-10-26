"""cleanup-merged: registry contract, validation, draft run, and one confirmed
cleanup.

Offline only: the GitHub client is a recording fake, the coding agent is a stub
that writes the draft, and no network, model, or real repository is touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent import tasks
from ghagent.interfaces import Job, JobKind, JobState
from ghagent.tasks import cleanup_merged
from ghagent.tasks.types import PROFILES
from ghagent.web_control import ApiError

from tests.test_executor_flow import (FakeWT, agent_result, default_cfg, happy_gh,
                                      run_job)
from tests.test_web_control import GuiFixture

REPO = "owner/repo"
NUMBER = 12
TITLE = "Contact form retries a failed send"
NOTES = ("Sending the contact form now retries once when the first attempt "
         "fails, so a short network blip no longer loses the message.\n\n"
         "Nothing changes on screen; the confirmation still says sent.")
TAG_VALUE = "v1.4.0"


def merged_pull(body="Closes #7 and fixes #8.\n\nAlso mentions #9.",
                head="feature/contact-form", base="main", merged=True,
                state=None):
    return {"number": NUMBER, "title": "Retry the contact form once",
            "state": state or ("closed" if merged else "open"),
            "merged": merged, "body": body,
            "head": {"ref": head, "repo": {"full_name": REPO}},
            "base": {"ref": base, "repo": {"full_name": REPO}}}


class RecordingGH:
    """Stands in for the GitHub client: records every call, sends nothing."""

    def __init__(self, pull=None, errors=None):
        self.pull = merged_pull() if pull is None else pull
        self.errors = dict(errors or {})   # "METHOD path-prefix" -> exception
        self.calls: list[dict] = []
        self.linked_issues = [7, 8]
        self.default_branch = "main"
        self.graphql_error = None

    def graphql(self, query, variables=None):
        if self.graphql_error:
            raise self.graphql_error
        return {"repository": {"defaultBranchRef": {"name": self.default_branch},
                "pullRequest": {"merged": self.pull.get("merged"),
                    "baseRefName": self.pull["base"]["ref"],
                    "closingIssuesReferences": {"nodes": [
                        {"number": issue, "repository": {"nameWithOwner": REPO}}
                        for issue in self.linked_issues],
                        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append({"method": method, "path": path, "body": body,
                           "paginate": paginate})
        for key, exc in self.errors.items():
            failing_method, _, prefix = key.partition(" ")
            if method == failing_method and path.startswith(prefix):
                raise exc
        if method == "GET" and path == f"repos/{REPO}/pulls/{NUMBER}":
            return self.pull
        if method == "POST" and path == f"repos/{REPO}/releases":
            return {"tag_name": (body or {}).get("tag_name")}
        return {}

    def writes(self):
        return [call for call in self.calls if call["method"] != "GET"]


class DraftingAgent:
    """Coding-agent factory stub: writes the release-notes draft itself."""

    def __init__(self, files=None):
        self.files = files if files is not None else {
            "release-notes.md": f"{TITLE}\n{NOTES}\n"}
        self.instructions: list[str] = []
        self.worktree = None

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


def make_job(job_id="job_20261022T101500Z_ab12cd34", **over):
    data = dict(job_id=job_id, kind=JobKind.CLEANUP_MERGED, repo_full_name=REPO,
                number=NUMBER, command_comment_id=0, requested_by=1,
                dedup_key="k", notes={"values": {"tag": TAG_VALUE}})
    data.update(over)
    return Job(**data)


def draft_context(**over):
    data = {"repo": REPO, "number": NUMBER,
            "draft": {"name": "release-notes", "title": TITLE, "body": NOTES},
            "values": {"tag": TAG_VALUE}}
    data.update(over)
    return data


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["cleanup-merged"]
        self.assertIs(task, cleanup_merged.TASK)
        self.assertEqual(task.id, "cleanup-merged")
        self.assertEqual(task.profile, "high-stakes")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "pr")
        self.assertTrue(task.post_confirm_required)
        self.assertIn("merge", task.label.lower())
        self.assertIn("release", task.help.lower())
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))
        self.assertTrue(callable(task.apply))

    def test_the_only_field_is_an_optional_tag(self):
        task = cleanup_merged.TASK
        self.assertEqual(list(task.fields), ["tag"])
        self.assertFalse(task.fields["tag"]["required"])
        self.assertIn("optional", task.fields["tag"]["label"].lower())
        self.assertEqual(task.required_fields(), [])

    def test_field_caps_hold(self):
        task = cleanup_merged.TASK
        self.assertLessEqual(len(task.fields), 3)
        self.assertLessEqual(len(task.required_fields()), 3)
        for name, spec in task.fields.items():
            self.assertIn("label", spec, name)

    def test_the_gui_catalog_lists_it(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("cleanup-merged", listed)
        entry = listed["cleanup-merged"]
        self.assertEqual(entry["profile"], "high-stakes")
        self.assertEqual(entry["takes"], "pr")
        self.assertEqual([field["name"] for field in entry["fields"]], ["tag"])


class ValidationTests(unittest.TestCase):
    def test_a_merge_without_a_tag_is_valid(self):
        for values in ({}, {"tag": ""}, {"tag": "   "}, {"tag": "v1.2.0-rc1"}):
            with self.subTest(values=values):
                self.assertIsNone(cleanup_merged.validate(
                    {"repo": REPO, "number": NUMBER, "values": values}))

    def test_missing_pull_request_number_is_refused_plainly(self):
        for data in ({"repo": REPO, "values": {}},
                     {"repo": REPO, "number": 0},
                     {"repo": REPO, "number": "abc"},
                     {"repo": REPO}, None, {}):
            with self.subTest(data=data):
                with self.assertRaises(ApiError) as caught:
                    cleanup_merged.validate(data)
                self.assertIn("Choose the merged pull request", str(caught.exception))
                self.assertEqual(caught.exception.status, 400)

    def test_tags_github_would_reject_are_refused_plainly(self):
        for tag in ("v1 2", "v1\t2", "-leading-dash"):
            with self.subTest(tag=tag):
                with self.assertRaises(ApiError) as caught:
                    cleanup_merged.validate({"repo": REPO, "number": NUMBER,
                                             "values": {"tag": tag}})
                self.assertIn("release tag", str(caught.exception))
                self.assertEqual(caught.exception.status, 400)


class InstructionTests(unittest.TestCase):
    def test_instruction_names_the_pull_request_and_the_draft(self):
        text = cleanup_merged.build_instruction(
            {"repo": REPO, "number": NUMBER, "values": {"tag": TAG_VALUE}})
        self.assertIn(REPO, text)
        self.assertIn(f"#{NUMBER}", text)
        self.assertIn(cleanup_merged.DRAFT_FILE, text)
        self.assertIn("first line", text)
        self.assertIn("src/app.py:42", text)
        self.assertIn("Closes #12", text)
        self.assertIn("Do not post, comment, close", text)
        self.assertIn(TAG_VALUE, text)

    def test_instruction_without_input_still_reads(self):
        text = cleanup_merged.build_instruction({"repo": REPO})
        self.assertIn(cleanup_merged.DRAFT_FILE, text)
        self.assertIn("merged", text)
        self.assertNotIn("The release tag the person wants", text)


class DraftExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(self.tmp / "worktrees")
        self.worktree = str(self.tmp / "wt")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_draft_run_waits_for_the_person_and_touches_no_remote(self):
        agent = DraftingAgent()
        gh = happy_gh()
        returned, queue, wt, _, git_rec, _ = run_job(
            make_job(), gh, cfg=self.cfg, wt=FakeWT(path=self.worktree), agent=agent)

        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertEqual(returned.notes["phase"], "waiting-on-you")
        states = [state for state, _ in queue.history]
        self.assertIn("working", states)
        self.assertEqual(states[-1], "waiting-on-you")
        self.assertNotIn("responding", states)

        drafts = returned.notes["drafts"]
        self.assertEqual([d["name"] for d in drafts], ["release-notes"])
        self.assertEqual(drafts[0]["title"], TITLE)
        self.assertIn("retries once", drafts[0]["body"])
        self.assertNotIn(TITLE, drafts[0]["body"])

        self.assertIn(cleanup_merged.DRAFT_FILE, agent.instructions[0])
        self.assertIn(f"#{NUMBER}", agent.instructions[0])
        self.assertIn(".gha-drafts/", agent.instructions[0])
        self.assertFalse([c for c in wt.calls if c[0] == "commit_all"])
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])


class ApplyTests(unittest.TestCase):
    def run_apply(self, gh=None, context=None, job=None):
        gh = gh or RecordingGH()
        outcome = cleanup_merged.TASK.apply(context or draft_context(), gh,
                                            job or make_job())
        return outcome, gh

    def test_confirmed_cleanup_calls_exactly_the_expected_paths(self):
        outcome, gh = self.run_apply()

        self.assertEqual(gh.calls, [
            {"method": "GET", "path": f"repos/{REPO}/pulls/{NUMBER}",
             "body": None, "paginate": False},
            {"method": "PATCH", "path": f"repos/{REPO}/issues/7",
             "body": {"state": "closed"}, "paginate": False},
            {"method": "PATCH", "path": f"repos/{REPO}/issues/8",
             "body": {"state": "closed"}, "paginate": False},
            {"method": "DELETE",
             "path": f"repos/{REPO}/git/refs/heads/feature/contact-form",
             "body": None, "paginate": False},
            {"method": "POST", "path": f"repos/{REPO}/releases",
             "body": {"tag_name": TAG_VALUE, "name": TITLE, "body": NOTES},
             "paginate": False},
        ])
        self.assertEqual(outcome, {"result": (
            f"Closed 2 issue(s), deleted the merged branch, published release "
            f"{TAG_VALUE}.")})

    def test_fork_or_unknown_head_repository_never_deletes_a_base_branch(self):
        for source in ({"full_name": "contributor/repo"}, None, {}):
            with self.subTest(source=source):
                pull = merged_pull(head="fix/login")
                pull["head"]["repo"] = source
                outcome, gh = self.run_apply(gh=RecordingGH(pull=pull))
                self.assertFalse([c for c in gh.calls if c["method"] == "DELETE"])
                self.assertIn("left the merged branch alone", outcome["result"])

    def test_only_authoritative_closing_links_close_issues(self):
        gh = RecordingGH(pull=merged_pull(body="Fixes #7. Closes #8."))
        gh.linked_issues = [8]
        self.run_apply(gh=gh)
        self.assertEqual([c["path"] for c in gh.calls if c["method"] == "PATCH"],
                         [f"repos/{REPO}/issues/8"])

    def test_merge_to_nondefault_branch_does_not_close_linked_issues(self):
        gh = RecordingGH(pull=merged_pull(base="release"))
        self.run_apply(gh=gh)
        self.assertFalse([c for c in gh.calls if c["method"] == "PATCH"])

    def test_unreadable_closing_evidence_stops_before_any_write(self):
        gh = RecordingGH()
        gh.graphql_error = RuntimeError("GitHub unavailable")
        with self.assertRaisesRegex(ValueError, "closing.*could not be verified"):
            self.run_apply(gh=gh)
        self.assertEqual(gh.writes(), [])

    def test_closing_links_are_complete_and_use_their_actual_repository(self):
        gh = RecordingGH()
        first = gh.graphql(None)
        links = first["repository"]["pullRequest"]["closingIssuesReferences"]
        links["nodes"] = [{"number": 9, "repository": {"nameWithOwner": "other/repo"}}]
        links["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        second = gh.graphql(None)
        with patch.object(gh, "graphql", side_effect=[first, second]) as query:
            self.run_apply(gh=gh)
        self.assertEqual(query.call_args_list[1].args[1]["cursor"], "next")
        self.assertEqual([c["path"] for c in gh.calls if c["method"] == "PATCH"],
                         [f"repos/{REPO}/issues/7", f"repos/{REPO}/issues/8"])

    def test_incomplete_closing_page_refuses_before_any_write(self):
        gh = RecordingGH()
        response = gh.graphql(None)
        del response["repository"]["pullRequest"]["closingIssuesReferences"]["pageInfo"]
        with patch.object(gh, "graphql", return_value=response), \
                self.assertRaisesRegex(ValueError, "could not be verified"):
            self.run_apply(gh=gh)
        self.assertEqual(gh.writes(), [])

    def test_the_base_branch_is_never_touched(self):
        _, gh = self.run_apply()
        self.assertFalse([call for call in gh.calls if "main" in call["path"]])
        self.assertNotIn("POST", [c["method"] for c in gh.writes()
                                  if c["path"].endswith("/merge")])

    def test_logged_tag_wins_and_missing_tag_falls_back_to_the_title_slug(self):
        _, gh = self.run_apply(context=draft_context(values={"tag": "  release-1  "}))
        release = [c for c in gh.calls if c["path"].endswith("/releases")][0]
        self.assertEqual(release["body"]["tag_name"], "release-1")

        _, gh = self.run_apply(context=draft_context(values={}))
        release = [c for c in gh.calls if c["path"].endswith("/releases")][0]
        self.assertEqual(release["body"]["tag_name"], "contact-form-retries-a-failed-send")

    def test_an_unmerged_pull_request_is_refused_before_any_write(self):
        for pull in (merged_pull(merged=False, state="open"),
                     merged_pull(merged=False, state="closed")):
            with self.subTest(pull=pull), self.assertRaises(ValueError) as caught:
                self.run_apply(gh=RecordingGH(pull=pull))
            self.assertEqual(str(caught.exception), cleanup_merged.NOT_MERGED)
            self.assertIn("merging stays your decision", str(caught.exception))

        gh = RecordingGH(pull=merged_pull(merged=False, state="closed"))
        with self.assertRaises(ValueError):
            self.run_apply(gh=gh)
        self.assertEqual([c["method"] for c in gh.calls], ["GET"])

    def test_a_pull_request_that_is_still_open_is_refused(self):
        gh = RecordingGH(pull=merged_pull(merged=False, state="open"))
        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=gh)
        self.assertEqual(str(caught.exception), cleanup_merged.NOT_MERGED)
        self.assertEqual(gh.writes(), [])

    def test_only_close_and_fix_lines_close_issues_and_each_once(self):
        body = ("Mentions #9 as related work.\nCloses #7, fixes #8.\n"
                "Fixes #7 again.")
        _, gh = self.run_apply(gh=RecordingGH(pull=merged_pull(body=body)))
        patched = [c["path"] for c in gh.calls if c["method"] == "PATCH"]
        self.assertEqual(patched, [f"repos/{REPO}/issues/7",
                                   f"repos/{REPO}/issues/8"])

    def test_a_main_branch_head_is_never_deleted(self):
        for head in ("main", "master"):
            with self.subTest(head=head):
                outcome, gh = self.run_apply(
                    gh=RecordingGH(pull=merged_pull(head=head)))
                self.assertFalse([c for c in gh.calls if c["method"] == "DELETE"])
                self.assertIn("left the merged branch alone", outcome["result"])
                self.assertIn("never deletes", outcome["result"])

    def test_a_head_equal_to_the_base_is_never_deleted(self):
        outcome, gh = self.run_apply(
            gh=RecordingGH(pull=merged_pull(head="trunk", base="trunk")))
        self.assertFalse([c for c in gh.calls if c["method"] == "DELETE"])
        self.assertIn("left the merged branch alone", outcome["result"])

    def test_an_existing_tag_is_reported_plainly(self):
        gh = RecordingGH(errors={
            f"POST repos/{REPO}/releases":
                RuntimeError("gh api failed: 422 Validation Failed (already_exists)")})
        outcome, _ = self.run_apply(gh=gh)
        self.assertIn(f"the tag {TAG_VALUE} already exists", outcome["result"])
        self.assertIn("no release was published", outcome["result"])
        self.assertIn("deleted the merged branch", outcome["result"])
        self.assertEqual([c["method"] for c in gh.calls][:2], ["GET", "PATCH"])

    def test_other_release_failures_read_as_one_plain_sentence(self):
        gh = RecordingGH(errors={
            f"POST repos/{REPO}/releases": RuntimeError("gh api failed: 500 Internal Server Error")})
        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=gh)
        message = str(caught.exception)
        self.assertIn("could not be published", message)
        self.assertIn("500", message)
        self.assertNotIn("Traceback", message)

    def test_a_failed_issue_close_is_reported_not_hidden(self):
        gh = RecordingGH(errors={
            f"PATCH repos/{REPO}/issues/7": RuntimeError("gh api failed: 403 Forbidden")})
        outcome, _ = self.run_apply(gh=gh)
        self.assertIn("Closed 1 issue(s)", outcome["result"])
        self.assertIn("#7 could not be closed", outcome["result"])
        self.assertIn("published release", outcome["result"])
        self.assertIn(f"repos/{REPO}/git/refs/heads/feature/contact-form",
                      [c["path"] for c in gh.calls])

    def test_missing_title_refuses_before_any_remote_call(self):
        gh = RecordingGH()
        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=gh, context=draft_context(
                draft={"name": "release-notes", "title": "  ", "body": NOTES}))
        self.assertIn("no title", str(caught.exception))
        self.assertEqual(gh.calls, [])

    def test_unidentified_pull_request_is_refused_plainly(self):
        gh = RecordingGH()
        context = draft_context()
        context.pop("number")
        with self.assertRaises(ValueError) as caught:
            self.run_apply(gh=gh, context=context, job=make_job(number=0))
        self.assertIn("could not be identified", str(caught.exception))
        self.assertEqual(gh.calls, [])


class PostThroughWebControlTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.gh_recorder = RecordingGH()
        self.control.gh = self.gh_recorder
        tasks.types._REGISTRY = None

    def tearDown(self):
        tasks.types._REGISTRY = None

    def waiting_job(self):
        job = Job(job_id="job_cleanup", kind=JobKind.CLEANUP_MERGED,
                  repo_full_name=REPO, number=NUMBER, command_comment_id=0,
                  requested_by=42, dedup_key="cleanup-merged:owner/repo:12:x",
                  state=JobState.WAITING_ON_YOU,
                  notes={"drafts": [{"name": "release-notes", "title": TITLE,
                                     "body": NOTES}],
                         "values": {"tag": TAG_VALUE},
                         "output_dir": str(self.output), "member": "alice"})
        self.assertTrue(self.control.queue().add(job))
        return job

    def test_confirmation_is_required_before_anything_reaches_github(self):
        self.waiting_job()
        with self.assertRaises(ApiError) as caught:
            self.control.draft_action("job_cleanup",
                                      {"action": "post", "name": "release-notes"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.gh_recorder.calls, [])

    def test_confirmed_post_runs_the_cleanup_once_and_finishes(self):
        self.waiting_job()
        result = self.control.draft_action(
            "job_cleanup", {"action": "post", "name": "release-notes",
                            "confirm": True})
        self.assertEqual(result["job"]["state"], "done")
        self.assertIn(f"published release {TAG_VALUE}", result["hint"])
        self.assertEqual([c["method"] for c in self.gh_recorder.calls],
                         ["GET", "PATCH", "PATCH", "DELETE", "POST"])

    def test_an_unmerged_pull_request_reads_as_a_hint_not_a_crash(self):
        self.waiting_job()
        self.gh_recorder.pull = merged_pull(merged=False, state="closed")
        with self.assertRaises(ApiError) as caught:
            self.control.draft_action(
                "job_cleanup", {"action": "post", "name": "release-notes",
                                "confirm": True})
        self.assertIn("not merged yet", str(caught.exception))
        self.assertEqual(self.gh_recorder.writes(), [])
        job = self.control.queue().get("job_cleanup")
        self.assertEqual(job.state, JobState.WAITING_ON_YOU)


if __name__ == "__main__":
    unittest.main()
