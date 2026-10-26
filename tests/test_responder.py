"""Responder tests: fakes only, no network, no sibling-module imports."""
import json
import os
import unittest
from unittest.mock import patch

from ghagent import interfaces, responder
from ghagent.responder import ResponderError

REPO = "o/r"


def body(job_id="job_x", text="Here is what I did."):
    return responder.build_comment_body(job_id, text)


class RecordGh:
    """Stands in for ghagent.responder._run_gh (the gh CLI subprocess)."""

    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls = []
        self.bodies = []

    def __call__(self, args, timeout=responder.DEFAULT_TIMEOUT_SECONDS):
        self.calls.append((list(args), timeout))
        if "--body-file" in args:
            path = args[args.index("--body-file") + 1]
        elif "-F" in args:
            path = args[args.index("-F") + 1].split("@", 1)[1]
        else:
            path = None
        if path:
            with open(path, encoding="utf-8") as fh:
                self.bodies.append(fh.read())
        return self.rc, self.out, self.err

    def temp_paths(self):
        paths = []
        for args, _ in self.calls:
            if "--body-file" in args:
                paths.append(args[args.index("--body-file") + 1])
            if "-F" in args:
                paths.append(args[args.index("-F") + 1].split("@", 1)[1])
        return paths


class FakeGH:
    """Read-side GitHubClient fake (interfaces.GitHubClientProto)."""

    def __init__(self):
        self.rest_calls = []
        self.graphql_calls = []
        self.issue_comments = []
        self.parents = {}
        self.login = "gha-worker-bot"
        self.resolve_reply = {}

    def rest(self, method, path, body=None, paginate=False):
        self.rest_calls.append((method, path, paginate))
        if path.startswith(f"repos/{REPO}/pulls/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            return self.parents[cid]
        if path.startswith(f"repos/{REPO}/issues/"):
            return self.issue_comments
        raise AssertionError(f"unexpected rest call: {method} {path}")

    def graphql(self, query, variables=None, paginate_path=None):
        self.graphql_calls.append((query, variables))
        if "resolveReviewThread" in query:
            return self.resolve_reply
        raise AssertionError(f"unexpected graphql call: {query[:60]}")

    def pr_view(self, repo, number): raise AssertionError("pr_view not expected")
    def issue_view(self, repo, number): raise AssertionError("issue_view not expected")
    def pr_checks(self, repo, number): raise AssertionError("pr_checks not expected")
    def run_view(self, repo, run_id): raise AssertionError("run_view not expected")
    def current_login(self): return self.login
    def current_user_id(self): return 424242


class BuildBodyTests(unittest.TestCase):
    def test_marker_metadata_then_text(self):
        text = body("job_1", "Line one.\nLine two.")
        self.assertTrue(text.startswith(interfaces.COMMENT_MARKER))
        first, rest = text.split("\n", 1)
        self.assertIn("job_id=job_1", first)
        self.assertTrue(rest.startswith("Line one."))

    def test_distinct_job_ids_give_distinct_headers(self):
        self.assertNotEqual(body("a"), body("b"))


class PostPrCommentTests(unittest.TestCase):
    def test_parses_id_from_url_tail_and_cleans_temp(self):
        rec = RecordGh(rc=0, out="https://github.com/o/r/pull/12#issuecomment-9001\n")
        with patch.object(responder, "_run_gh", rec):
            cid = responder.post_pr_comment(FakeGH(), REPO, 12, body("job_1"))
        self.assertEqual(cid, 9001)
        args, _ = rec.calls[0]
        self.assertEqual(args[:4], ["pr", "comment", "12", "--repo"])
        self.assertIn(REPO, args)
        self.assertIn("--body-file", args)
        self.assertEqual(rec.bodies[0], body("job_1"))
        for path in rec.temp_paths():
            self.assertFalse(os.path.exists(path))

    def test_utf8_body_written_losslessly(self):
        rec = RecordGh(rc=0, out="https://github.com/o/r/pull/3#issuecomment-1\n")
        text = body("job_u", "Unicode check: héllo — ✓ 日本語")
        with patch.object(responder, "_run_gh", rec):
            responder.post_pr_comment(FakeGH(), REPO, 3, text)
        self.assertEqual(rec.bodies[0], text)

    def test_rejects_body_without_marker_before_any_call(self):
        rec = RecordGh(rc=0, out="https://github.com/o/r/pull/3#issuecomment-1\n")
        with patch.object(responder, "_run_gh", rec):
            with self.assertRaises(ResponderError):
                responder.post_pr_comment(FakeGH(), REPO, 3, "plain text, no marker")
        self.assertEqual(rec.calls, [])

    def test_falls_back_to_rereading_latest_own_comment(self):
        gh = FakeGH()
        text = body("job_2")
        gh.issue_comments = [
            {"id": 55, "user": {"login": "gha-worker-bot"}, "body": text,
             "created_at": "2026-10-17T08:00:00Z"},
            {"id": 77, "user": {"login": "gha-worker-bot"}, "body": text,
             "created_at": "2026-10-17T10:00:00Z"},
            {"id": 66, "user": {"login": "someone-else"}, "body": text,
             "created_at": "2026-10-17T11:00:00Z"},
        ]
        rec = RecordGh(rc=0, out="Comment created (no URL in output)")
        with patch.object(responder, "_run_gh", rec):
            cid = responder.post_pr_comment(gh, REPO, 12, text)
        self.assertEqual(cid, 77)
        self.assertEqual(gh.rest_calls[0], ("GET", f"repos/{REPO}/issues/12/comments", True))

    def test_timeout_is_verified_by_reread_not_retried(self):
        gh = FakeGH()
        text = body("job_3")
        gh.issue_comments = [{"id": 808, "user": {"login": "gha-worker-bot"},
                              "body": text, "created_at": "2026-10-17T10:00:00Z"}]
        rec = RecordGh(rc=124, out="", err="timed out")
        with patch.object(responder, "_run_gh", rec):
            cid = responder.post_pr_comment(gh, REPO, 12, text)
        self.assertEqual(cid, 808)
        self.assertEqual(len(rec.calls), 1)  # no blind retry after the timeout

    def test_hard_failure_raises(self):
        rec = RecordGh(rc=1, out="", err="boom")
        with patch.object(responder, "_run_gh", rec):
            with self.assertRaises(ResponderError):
                responder.post_pr_comment(FakeGH(), REPO, 12, body("job_4"))


class ReplyInlineTests(unittest.TestCase):
    def test_posts_reply_to_numeric_root_id(self):
        rec = RecordGh(rc=0, out=json.dumps({"id": 4242}))
        with patch.object(responder, "_run_gh", rec):
            cid = responder.reply_inline(FakeGH(), REPO, 12, 321, body("job_5"))
        self.assertEqual(cid, 4242)
        args, _ = rec.calls[0]
        self.assertEqual(args[0], "api")
        self.assertEqual(args[args.index("--method") + 1], "POST")
        self.assertIn(f"repos/{REPO}/pulls/12/comments/321/replies", args)
        header_at = args.index("-H")
        self.assertIn("X-GitHub-Api-Version:", args[header_at + 1])
        flag_at = args.index("-F")
        self.assertTrue(args[flag_at + 1].startswith("body=@"))
        self.assertEqual(rec.bodies[0], body("job_5"))
        self.assertFalse(os.path.exists(args[args.index("-F") + 1].split("@", 1)[1]))

    def test_rejects_graphql_node_ids_and_nonpositive_ids(self):
        rec = RecordGh(rc=0, out="{}")
        for bad in ("PRRCT_nodeAbc123", 0, -5, True, None):
            with patch.object(responder, "_run_gh", rec):
                with self.assertRaises(ResponderError):
                    responder.reply_inline(FakeGH(), REPO, 12, bad, body("j"))
        self.assertEqual(rec.calls, [])

    def test_rejects_body_without_marker(self):
        rec = RecordGh(rc=0, out="{}")
        with patch.object(responder, "_run_gh", rec):
            with self.assertRaises(ResponderError):
                responder.reply_inline(FakeGH(), REPO, 12, 321, "no marker")
        self.assertEqual(rec.calls, [])

    def test_cli_failure_raises(self):
        rec = RecordGh(rc=4, out="", err="HTTP 422")
        with patch.object(responder, "_run_gh", rec):
            with self.assertRaises(ResponderError):
                responder.reply_inline(FakeGH(), REPO, 12, 321, body("j"))


class ResolveRootTests(unittest.TestCase):
    def test_walks_in_reply_to_chain_to_the_root(self):
        gh = FakeGH()
        gh.parents = {
            222: {"id": 222, "node_id": "N2", "in_reply_to_id": 111},
            111: {"id": 111, "node_id": "N1", "in_reply_to_id": None},
        }
        root = responder.resolve_root(gh, REPO, 12, gh.parents[222])
        self.assertEqual(root["id"], 111)
        self.assertEqual(gh.rest_calls, [
            ("GET", f"repos/{REPO}/pulls/comments/111", False)])

    def test_root_comment_returned_unchanged(self):
        gh = FakeGH()
        root = {"id": 5, "node_id": "N5", "in_reply_to_id": None}
        self.assertEqual(responder.resolve_root(gh, REPO, 12, root)["id"], 5)
        self.assertEqual(gh.rest_calls, [])


class ResolveThreadTests(unittest.TestCase):
    def test_resolve_confirmed(self):
        gh = FakeGH()
        gh.resolve_reply = {"resolveReviewThread": {"thread": {
            "id": "PRRT_1", "isResolved": True}}}
        thread = responder.resolve_thread(gh, "PRRT_1")
        self.assertTrue(thread["isResolved"])
        query, variables = gh.graphql_calls[0]
        self.assertIn("resolveReviewThread", query)
        self.assertEqual(variables, {"threadId": "PRRT_1"})

    def test_resolve_not_confirmed_raises(self):
        gh = FakeGH()
        gh.resolve_reply = {"resolveReviewThread": {"thread": {
            "id": "PRRT_1", "isResolved": False}}}
        with self.assertRaises(ResponderError):
            responder.resolve_thread(gh, "PRRT_1")

    def test_missing_thread_raises(self):
        gh = FakeGH()
        gh.resolve_reply = {"resolveReviewThread": {"thread": None}}
        with self.assertRaises(ResponderError):
            responder.resolve_thread(gh, "PRRT_missing")


class CanResolveTests(unittest.TestCase):
    def test_matrix(self):
        self.assertTrue(responder.can_resolve(
            {"isResolved": False, "viewerCanResolve": True}))
        self.assertFalse(responder.can_resolve(
            {"isResolved": True, "viewerCanResolve": True}))
        self.assertFalse(responder.can_resolve(
            {"isResolved": False, "viewerCanResolve": False}))
        self.assertFalse(responder.can_resolve({}))
        self.assertFalse(responder.can_resolve(None))


if __name__ == "__main__":
    unittest.main()
