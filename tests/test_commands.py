"""Tests for ghagent.commands — phrase parsing, authorization by numeric id
only, dedup keys, own-comment detection."""
from __future__ import annotations
import unittest

from ghagent.commands import (
    CommandRequest,
    dedup_key_for,
    ignore_own,
    is_authorized,
    is_own_comment,
    parse_command,
)
from ghagent.interfaces import COMMENT_MARKER, Job, JobKind

HUGE_ID = 2**33  # 8589934592 — GitHub ids already exceed 2^31


def comment(login="alice", user_id=111, body=""):
    return {"user": {"id": user_id, "login": login, "type": "User"},
            "body": body}


class ParseFixTest(unittest.TestCase):
    def test_bare_fix_on_pr_target(self):
        req = parse_command("/agent fix", "pr", 7)
        self.assertEqual(req.kind, JobKind.FIX_PR)
        self.assertIsNone(req.repo_full_name)
        self.assertEqual(req.number, 7)
        self.assertEqual(req.raw, "/agent fix")

    def test_fix_with_repo_and_number_argument(self):
        req = parse_command("/agent fix owner/repo#12", "pr", 7)
        self.assertEqual(req.repo_full_name, "owner/repo")
        self.assertEqual(req.number, 12)

    def test_fix_with_hash_number_argument(self):
        req = parse_command("/agent fix #12", "pr", 7)
        self.assertIsNone(req.repo_full_name)
        self.assertEqual(req.number, 12)

    def test_fix_argument_followed_by_words(self):
        req = parse_command("/agent fix #12 please look at CI", "pr", 7)
        self.assertEqual(req.number, 12)

    def test_leading_whitespace_allowed(self):
        self.assertEqual(parse_command("   /agent fix", "pr", 7).number, 7)

    def test_command_on_later_line(self):
        req = parse_command("hello team\ncould someone look?\n/agent fix", "pr", 7)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, JobKind.FIX_PR)

    def test_fix_on_issue_target_is_ignored(self):
        self.assertIsNone(parse_command("/agent fix", "issue", 7))

    def test_first_command_wins(self):
        req = parse_command("/agent status\n/agent fix", "pr", 7)
        self.assertEqual(req.kind, JobKind.STATUS)


class ParseImplementStatusStopTest(unittest.TestCase):
    def test_implement_on_issue(self):
        req = parse_command("/agent implement", "issue", 5)
        self.assertEqual(req.kind, JobKind.IMPLEMENT_ISSUE)
        self.assertEqual(req.number, 5)

    def test_implement_with_argument(self):
        req = parse_command("/agent implement other/repo#8", "issue", 5)
        self.assertEqual((req.repo_full_name, req.number), ("other/repo", 8))

    def test_implement_on_pr_target_is_ignored(self):
        self.assertIsNone(parse_command("/agent implement", "pr", 5))

    def test_status_and_stop_on_either_target(self):
        for target in ("pr", "issue"):
            with self.subTest(target=target):
                self.assertEqual(
                    parse_command("/agent status", target, 3).kind, JobKind.STATUS)
                self.assertEqual(
                    parse_command("/agent stop", target, 3).kind, JobKind.STOP)

    def test_stop_ignores_trailing_words(self):
        self.assertEqual(parse_command("/agent stop now please", "pr", 3).kind,
                         JobKind.STOP)


class ParseEdgeCasesTest(unittest.TestCase):
    def test_mid_sentence_mention_is_not_a_command(self):
        self.assertIsNone(parse_command("please /agent fix this pr", "pr", 7))

    def test_quoted_prefix_line_is_not_a_command(self):
        self.assertIsNone(parse_command("> /agent fix", "pr", 7))

    def test_inside_code_fence_is_ignored(self):
        body = "look:\n```\n/agent fix\n```\nnothing to do"
        self.assertIsNone(parse_command(body, "pr", 7))

    def test_fence_with_language_marker(self):
        body = "```python\n/agent fix\n```"
        self.assertIsNone(parse_command(body, "pr", 7))

    def test_command_after_closing_fence_is_recognized(self):
        body = "```\n/agent fix\n```\n/agent fix"
        req = parse_command(body, "pr", 7)
        self.assertIsNotNone(req)
        self.assertEqual(req.number, 7)

    def test_case_sensitive_prefix(self):
        for line in ("/Agent fix", "/AGENT fix", " /agent Fix"):
            with self.subTest(line=line):
                self.assertIsNone(parse_command(line, "pr", 7))

    def test_unknown_verb_is_ignored(self):
        self.assertIsNone(parse_command("/agent dance", "pr", 7))

    def test_prefix_alone_is_ignored(self):
        self.assertIsNone(parse_command("/agent", "pr", 7))

    def test_no_number_and_no_argument_yields_none(self):
        self.assertIsNone(parse_command("/agent fix", "pr", None))

    def test_argument_supplies_number_even_without_target(self):
        req = parse_command("/agent fix #9", "pr", None)
        self.assertEqual(req.number, 9)

    def test_non_argument_word_after_fix_falls_back_to_target(self):
        req = parse_command("/agent fix readme", "pr", 7)
        self.assertEqual(req.number, 7)
        self.assertIsNone(req.repo_full_name)

    def test_invalid_target_kind_raises(self):
        with self.assertRaises(ValueError):
            parse_command("/agent fix", "gist", 7)

    def test_empty_body(self):
        self.assertIsNone(parse_command("", "pr", 7))
        self.assertIsNone(parse_command(None, "pr", 7))

    def test_configurable_prefix(self):
        req = parse_command("!bot fix", "pr", 7, command_prefix="!bot")
        self.assertEqual(req.kind, JobKind.FIX_PR)
        self.assertIsNone(parse_command("/agent fix", "pr", 7,
                                        command_prefix="!bot"))


class AuthorizationTest(unittest.TestCase):
    def test_author_id_in_allowed_list(self):
        self.assertTrue(is_authorized(comment(user_id=42), [42], {"me"}))

    def test_wrong_id_never_authorized_even_with_allowed_login(self):
        # display name / bot-flipped login matching an allowed account must
        # not authorize — only the numeric id counts
        c = comment(login="alice", user_id=999)
        self.assertFalse(is_authorized(c, [111], set()))

    def test_mention_in_body_does_not_authorize(self):
        c = comment(login="mallory", user_id=999,
                    body="@alice please /agent fix")
        self.assertFalse(is_authorized(c, [111], set()))

    def test_huge_ids_compare_exactly(self):
        self.assertTrue(is_authorized(comment(user_id=HUGE_ID), [HUGE_ID], set()))
        self.assertFalse(is_authorized(comment(user_id=HUGE_ID + 1), [HUGE_ID], set()))

    def test_missing_or_bad_id_never_authorizes(self):
        self.assertFalse(is_authorized({"user": {"login": "x"}}, [1], set()))
        self.assertFalse(is_authorized({"user": {"id": None}}, [1], set()))
        self.assertFalse(is_authorized({"user": {"id": True}}, [1], set()))
        self.assertFalse(is_authorized({}, [1], set()))

    def test_own_login_never_authorizes_even_by_id(self):
        c = comment(login="gha-worker[bot]", user_id=42)
        self.assertFalse(is_authorized(c, [42], {"gha-worker[bot]"}))

    def test_ignore_own_by_login_case_insensitive(self):
        self.assertTrue(ignore_own(comment(login="GHA-Worker[Bot]"),
                                   {"gha-worker[bot]"}))
        self.assertFalse(ignore_own(comment(login="alice"), {"gha-worker[bot]"}))

    def test_is_authorized_without_own_logins_argument(self):
        self.assertTrue(is_authorized(comment(user_id=1), [1]))


class OwnCommentAndDedupTest(unittest.TestCase):
    def test_is_own_comment_marker(self):
        body = f"{COMMENT_MARKER}fix-pr:o/r:1:abc--> done"
        self.assertTrue(is_own_comment(body))
        self.assertFalse(is_own_comment("plain words"))
        self.assertFalse(is_own_comment(""))

    def test_dedup_key_format_matches_job_contract(self):
        key = dedup_key_for(JobKind.FIX_PR, "o/r", 12, "a" * 40)
        self.assertEqual(key, "fix-pr:o/r:12:" + "a" * 40)
        job = Job(job_id="j", kind=JobKind.FIX_PR, repo_full_name="o/r",
                  number=12, command_comment_id=0, requested_by=1,
                  dedup_key=key)
        self.assertEqual(job.dedup_key, key)
        self.assertEqual(Job.from_json(job.to_json()).dedup_key, key)

    def test_dedup_key_accepts_kind_value_string(self):
        self.assertEqual(dedup_key_for("implement-issue", "o/r", 3, "rev1"),
                         "implement-issue:o/r:3:rev1")


if __name__ == "__main__":
    unittest.main()
