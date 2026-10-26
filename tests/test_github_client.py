"""Tests for ghagent.github_client — all via the FakeGh runner: no network,
no gh binary required."""
from __future__ import annotations

import json
import os
import unittest

from ghagent.github_client import (
    DEFAULT_GH_PATH,
    AuthError,
    GitHubClient,
    GitHubError,
    NotFound,
    PR_CHECK_FIELDS,
    PR_VIEW_FIELDS,
    RateLimited,
    ServerError,
    ValidationError,
    build_env,
    classify_gh_failure,
    coerce_github_id,
    find_gh,
    redact_text,
)
from tests.mock_github import make_client

HUGE_ID = 2**53 + 1  # 9007199254740993 — corrupts if round-tripped through float


class RestTest(unittest.TestCase):
    def test_get_is_explicit_method_with_api_version_header(self):
        client = make_client()
        client.runner.expect_json({"login": "octocat"})
        data = client.rest("GET", "/user")
        self.assertEqual(data, {"login": "octocat"})
        argv = client.runner.last_call.argv
        self.assertEqual(argv[0], "api")
        self.assertEqual(argv[1:3], ["--method", "GET"])
        self.assertIn("-H", argv)
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", argv)
        self.assertNotIn("--input", argv)

    def test_write_posts_json_body_via_stdin(self):
        client = make_client()
        client.runner.expect_json({"number": 1})
        body = {"title": "hi", "labels": ["bug"]}
        client.rest("POST", "/repos/o/r/issues", body=body)
        call = client.runner.last_call
        self.assertIn("--input", call.argv)
        self.assertEqual(call.argv[call.argv.index("--method") + 1], "POST")
        self.assertEqual(json.loads(call.input), body)

    def test_lowercase_method_is_uppered(self):
        client = make_client()
        client.runner.expect_json({})
        client.rest("get", "/user")
        self.assertEqual(client.runner.last_call.argv[2], "GET")

    def test_failure_raises_classified_error(self):
        client = make_client()
        client.runner.expect_error("gh: Not Found (HTTP 404)", returncode=1)
        with self.assertRaises(NotFound):
            client.rest("GET", "/repos/o/r/nope")


class PaginationTest(unittest.TestCase):
    def test_three_pages_flatten_to_one_list(self):
        client = make_client()
        client.runner.expect_json([[{"n": 1}, {"n": 2}], [{"n": 3}], [{"n": 4}]])
        flat = client.rest("GET", "/repos/o/r/issues", paginate=True)
        self.assertEqual([item["n"] for item in flat], [1, 2, 3, 4])
        argv = client.runner.last_call.argv
        self.assertIn("--paginate", argv)
        self.assertIn("--slurp", argv)

    def test_paginate_with_scalar_pages_appends_them(self):
        client = make_client()
        client.runner.expect_json([{"a": 1}, {"a": 2}])
        flat = client.rest("GET", "/x", paginate=True)
        self.assertEqual(flat, [{"a": 1}, {"a": 2}])

    def test_paginate_non_array_payload_raises(self):
        client = make_client()
        client.runner.expect_json({"unexpected": True})
        with self.assertRaises(GitHubError):
            client.rest("GET", "/x", paginate=True)


class TypedViewsTest(unittest.TestCase):
    PR_FIELDS = ("number,url,state,title,body,headRefOid,headRefName,"
                 "baseRefName,baseRefOid,headRepository,headRepositoryOwner,"
                 "isCrossRepository,maintainerCanModify")

    def test_pr_view_argv_and_fields(self):
        client = make_client()
        view = {"number": 7, "state": "OPEN", "headRefOid": "a" * 40}
        client.runner.expect_json(view)
        self.assertEqual(client.pr_view("o/r", 7), view)
        argv = client.runner.last_call.argv
        self.assertEqual(argv[:3], ["pr", "view", "7"])
        self.assertEqual(argv[3:5], ["--repo", "o/r"])
        self.assertIn(PR_VIEW_FIELDS, argv)

    def test_issue_view(self):
        client = make_client()
        client.runner.expect_json({"number": 3, "title": "t", "state": "OPEN"})
        data = client.issue_view("o/r", 3)
        self.assertEqual(data["number"], 3)
        argv = client.runner.last_call.argv
        self.assertEqual(argv[:3], ["issue", "view", "3"])

    def test_pr_checks_exit_8_pending_is_successful_data(self):
        client = make_client()
        checks = [{"name": "ci", "state": "PENDING", "bucket": "pending",
                   "link": "http://x", "workflow": "CI", "event": "pull_request"}]
        client.runner.expect(stdout=json.dumps(checks), returncode=8)
        result = client.pr_checks("o/r", 7)
        self.assertEqual(result, checks)  # parsed despite exit code 8
        argv = client.runner.last_call.argv
        self.assertEqual(argv[:3], ["pr", "checks", "7"])
        self.assertIn(PR_CHECK_FIELDS, argv)

    def test_pr_checks_success(self):
        client = make_client()
        client.runner.expect_json([])
        self.assertEqual(client.pr_checks("o/r", 7), [])

    def test_pr_checks_real_failure_raises(self):
        client = make_client()
        client.runner.expect_error("gh: run git (HTTP 422)", returncode=1)
        with self.assertRaises(ValidationError):
            client.pr_checks("o/r", 7)

    def test_run_view(self):
        client = make_client()
        run = {"databaseId": 999, "headSha": "b" * 40, "conclusion": "SUCCESS"}
        client.runner.expect_json(run)
        self.assertEqual(client.run_view("o/r", 999), run)
        argv = client.runner.last_call.argv
        self.assertEqual(argv[:3], ["run", "view", "999"])


class ErrorTaxonomyTest(unittest.TestCase):
    def _client_with(self, stderr, returncode=1):
        client = make_client()
        client.runner.expect_error(stderr, returncode=returncode)
        return client

    def test_401_is_auth_error(self):
        with self.assertRaises(AuthError):
            self._client_with("gh: Bad credentials (HTTP 401)").rest("GET", "/user")

    def test_403_without_rate_limit_is_auth_error(self):
        with self.assertRaises(AuthError):
            self._client_with(
                "gh: Resource not accessible by integration (HTTP 403)"
            ).rest("GET", "/user")

    def test_404(self):
        with self.assertRaises(NotFound):
            self._client_with("gh: Not Found (HTTP 404)").rest("GET", "/x")

    def test_422_and_400_are_validation(self):
        with self.assertRaises(ValidationError):
            self._client_with("gh: Validation Failed (HTTP 422)").rest("POST", "/x")
        with self.assertRaises(ValidationError):
            self._client_with("gh: Bad request (HTTP 400)").rest("GET", "/x")

    def test_5xx_is_server_error(self):
        for status in (500, 502, 503):
            with self.assertRaises(ServerError):
                self._client_with(f"boom (HTTP {status})").rest("GET", "/x")

    def test_rate_limit_403_wins_over_auth(self):
        client = self._client_with(
            "gh: API rate limit exceeded for 1.2.3.4. (HTTP 403)")
        with self.assertRaises(RateLimited) as caught:
            client.rest("GET", "/user")
        self.assertIsNone(caught.exception.retry_after_seconds)

    def test_rate_limit_message_includes_retry_after(self):
        stderr = ("gh: You have exceeded a secondary rate limit (HTTP 403)\n"
                  "Retry-After: 57")
        with self.assertRaises(RateLimited) as caught:
            self._client_with(stderr).rest("GET", "/user")
        self.assertEqual(caught.exception.retry_after_seconds, 57)
        self.assertIn("57", str(caught.exception))

    def test_gh_auth_login_hint_is_auth_error(self):
        stderr = "To get started with GitHub CLI, please run: gh auth login\n"
        with self.assertRaises(AuthError):
            self._client_with(stderr).rest("GET", "/user")

    def test_unknown_failure_is_base_error(self):
        with self.assertRaises(GitHubError) as caught:
            self._client_with("gh: wiring exploded", returncode=42).rest("GET", "/x")
        self.assertNotIsInstance(caught.exception, (AuthError, NotFound,
                                                    ValidationError, ServerError))
        self.assertEqual(caught.exception.exit_code, 42)

    def test_classify_direct(self):
        self.assertIsInstance(classify_gh_failure(1, "x (HTTP 404)"), NotFound)
        self.assertIsInstance(classify_gh_failure(1, "nothing"), GitHubError)


class RedactionTest(unittest.TestCase):
    def test_redact_text_cuts_all_shapes(self):
        text = ("tokens ghs_ABCDEF123456, ghp_PERSONAL123456, gho_OAUTH123456, "
                "ghu_USER123456, ghr_REFRESH123456, sk-liveSECRETKEY, "
                "github_pat_11AAAAA0_bbbbbbbbbb and Authorization: Bearer xyz.abc-9")
        red = redact_text(text)
        for secret in ("ABCDEF123456", "PERSONAL123456", "OAUTH123456",
                       "USER123456", "REFRESH123456", "liveSECRETKEY",
                       "11AAAAA0", "xyz.abc-9"):
            self.assertNotIn(secret, red)
        for prefix in ("ghs_***", "ghp_***", "gho_***", "ghu_***", "ghr_***",
                       "sk-***", "github_pat_***", "Bearer ***"):
            self.assertIn(prefix, red)

    def test_error_text_is_redacted(self):
        client = make_client()
        client.runner.expect_error(
            'gh: bad token ghs_SUPERSECRET123 (HTTP 401)', returncode=1)
        with self.assertRaises(AuthError) as caught:
            client.rest("GET", "/user")
        self.assertNotIn("SUPERSECRET", str(caught.exception))
        self.assertNotIn("SUPERSECRET", caught.exception.stderr)
        self.assertIn("ghs_***", str(caught.exception))

    def test_non_json_error_output_redacted(self):
        client = make_client()
        client.runner.expect(stdout="leak ghs_TOPSECRET999 tail")
        with self.assertRaises(GitHubError) as caught:
            client.rest("GET", "/x")
        self.assertNotIn("TOPSECRET", str(caught.exception))


class IdentityTest(unittest.TestCase):
    def test_current_login(self):
        client = make_client()
        client.runner.expect_json({"login": "octocat", "id": 1})
        self.assertEqual(client.current_login(), "octocat")

    def test_current_user_id_preserves_huge_ids_exactly(self):
        client = make_client()
        client.runner.expect_json({"login": "octocat", "id": HUGE_ID})
        user_id = client.current_user_id()
        self.assertIsInstance(user_id, int)
        self.assertEqual(user_id, HUGE_ID)

    def test_current_user_id_rejects_non_integer(self):
        client = make_client()
        client.runner.expect_json({"login": "octocat", "id": "abc"})
        with self.assertRaises(GitHubError):
            client.current_user_id()

    def test_coerce_github_id_accepts_digits_rejects_floats(self):
        self.assertEqual(coerce_github_id(str(HUGE_ID)), HUGE_ID)
        self.assertEqual(coerce_github_id(7), 7)
        with self.assertRaises(GitHubError):  # float round-trip loses precision
            coerce_github_id(float(HUGE_ID))


class GraphqlTest(unittest.TestCase):
    def test_variables_are_typed_f_vs_capital_f(self):
        client = make_client()
        client.runner.expect_json({"data": {"viewer": {"login": "o"}}})
        query = "query($n:Int!,$s:String!){x}"
        client.graphql(query, {"n": 3, "s": "hi", "flag": True, "skip": None})
        argv = client.runner.last_call.argv
        self.assertEqual(argv[:2], ["api", "graphql"])
        self.assertIn("-f", argv)
        self.assertIn(f"query={query}", argv)
        self.assertIn("-F", argv)
        self.assertIn("n=3", argv)
        self.assertIn("flag=true", argv)
        self.assertIn("s=hi", argv)
        self.assertNotIn("skip=", " ".join(argv))

    def test_graphql_failure_classified(self):
        client = make_client()
        client.runner.expect_error("gh: syntax error (HTTP 400)", returncode=1)
        with self.assertRaises(ValidationError):
            client.graphql("query { x }")

    def test_paginate_path_merges_lists(self):
        client = make_client()
        pages = [{"data": {"items": [1, 2]}}, {"data": {"items": [3]}}]
        client.runner.expect_json(pages)
        merged = client.graphql("query", paginate_path="items")
        self.assertEqual(merged, {"items": [1, 2, 3]})

    def test_graphql_returns_unwrapped_data(self):
        client = make_client()
        client.runner.expect_json({"data": {"repository": {"x": 1}}})
        self.assertEqual(client.graphql("query"), {"repository": {"x": 1}})

    def test_graphql_errors_raise_instead_of_empty_data(self):
        client = make_client()
        client.runner.expect_json(
            {"data": None, "errors": [{"message": "Could not resolve to a node"}]})
        with self.assertRaises(GitHubError) as ctx:
            client.graphql("query")
        self.assertIn("Could not resolve", str(ctx.exception))


class RunnerAndEnvTest(unittest.TestCase):
    def test_injected_runner_receives_argv_without_binary_and_no_env_lookup(self):
        calls = []

        def runner(argv, input=None):
            calls.append((argv, input))
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        client = GitHubClient(runner=runner)
        client.rest("GET", "/user")
        self.assertEqual(calls[0][0][0], "api")  # no gh binary prepended

    def test_runner_property_returns_injected_fake(self):
        client = make_client()
        client.runner.expect_json({})
        client.rest("GET", "/x")
        self.assertEqual(len(client.runner.calls), 1)

    def test_build_env_copies_environ_and_pins_gh_host(self):
        env = build_env("ghe.example.com")
        self.assertEqual(env["GH_HOST"], "ghe.example.com")
        self.assertEqual(env["PATH"], os.environ.get("PATH"))
        self.assertIsNot(env, os.environ)
        # mutating the copy must not leak into the real environment
        env["GH_HOST"] = "changed"
        self.assertNotEqual(os.environ.get("GH_HOST"), "changed")

    def test_default_construction_never_requires_gh(self):
        GitHubClient()  # gh resolved lazily on first real call only

    def test_find_gh_when_installed(self):
        try:
            path = find_gh()
        except GitHubError:
            return  # gh absent: laziness already proven above
        self.assertTrue(path.endswith("gh"))
        self.assertTrue(os.path.isfile(path))

    def test_default_gh_path_constant(self):
        self.assertEqual(DEFAULT_GH_PATH, "gh")


if __name__ == "__main__":
    unittest.main()
