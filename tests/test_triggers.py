"""Trigger-guard tests: fakes only, no network, no sibling-module imports."""
import base64
import unittest
from urllib.parse import unquote

from ghagent import triggers

REPO = "octo/infra"
NOT_FOUND = "GET failed: 404 Not Found (gh: request failed)"


def blob(text, name="ci.yml", path=".github/workflows/ci.yml"):
    data = text.encode("utf-8")
    return {"type": "file", "name": name, "path": path,
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "size": len(data)}


def entry(name, path, size):
    return {"type": "file", "name": name, "path": path, "size": size}


class TriggerGH:
    """Read-side GitHubClient fake for the contents/rulesets API surface."""

    def __init__(self):
        self.repo_payload = {"default_branch": "main", "full_name": REPO}
        self.rulesets = []
        self.contents = {}   # (ref, path) -> entries list | blob dict | "404"
        self.errors = {}     # "repo" | "rulesets" | (ref, path) -> Exception
        self.calls = []

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append((method, path))
        if path == f"repos/{REPO}":
            exc = self.errors.get("repo")
            if exc:
                raise exc
            return self.repo_payload
        if path == f"repos/{REPO}/rulesets":
            exc = self.errors.get("rulesets")
            if exc:
                raise exc
            return self.rulesets
        if path.startswith(f"repos/{REPO}/contents/"):
            base, _, qs = path.partition("?")
            ref = unquote(qs.split("ref=", 1)[1].split("&", 1)[0]) if "ref=" in qs else ""
            rel = unquote(base.split("/contents/", 1)[1])
            exc = self.errors.get((ref, rel))
            if exc:
                raise exc
            val = self.contents.get((ref, rel), "404")
            if val == "404":
                raise RuntimeError(NOT_FOUND)
            return val
        raise AssertionError(f"unexpected rest call: {method} {path}")

    # unused proto surface
    def graphql(self, *a, **k): raise AssertionError("no graphql expected")
    def pr_view(self, *a, **k): raise AssertionError
    def issue_view(self, *a, **k): raise AssertionError
    def pr_checks(self, *a, **k): raise AssertionError
    def run_view(self, *a, **k): raise AssertionError
    def current_login(self): return "bot"
    def current_user_id(self): return 1


class PreFlightTests(unittest.TestCase):
    def test_missing_workflows_everywhere_is_safe(self):
        gh = TriggerGH()
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.SAFE)
        self.assertEqual(report.reasons, [])
        self.assertEqual(report.details["refs_inspected"], ["main"])

    def test_pull_request_target_blocks_and_names_the_trigger(self):
        gh = TriggerGH()
        gh.contents[("feature", ".github/workflows")] = [
            entry("ci.yml", ".github/workflows/ci.yml", 100)]
        gh.contents[("feature", ".github/workflows/ci.yml")] = blob(
            "on: [push, pull_request_target]\n"
            "jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")
        report = triggers.preflight(gh, REPO, head_ref="feature")
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("pull_request_target" in r for r in report.reasons))
        self.assertTrue(any("GitHub-hosted runners" in r for r in report.reasons))
        line = triggers.verdict_line(report)
        self.assertIn("NEEDS_REVIEW", line)
        self.assertNotIn("\n", line)

    def test_push_trigger_is_waivable_only_by_allow_workflows(self):
        text = "on:\n  push:\n    branches:\n      - main\njobs:\n  b:\n    steps:\n      - run: echo hi\n"
        gh = TriggerGH()
        gh.contents[("main", ".github/workflows")] = [
            entry("ci.yml", ".github/workflows/ci.yml", 100)]
        gh.contents[("main", ".github/workflows/ci.yml")] = blob(text)
        blocked = triggers.preflight(gh, REPO)
        self.assertEqual(blocked.verdict, triggers.REVIEW)
        self.assertTrue(any("push" in r for r in blocked.reasons))
        waived = triggers.preflight(gh, REPO, allow_workflows=True)
        self.assertEqual(waived.verdict, triggers.SAFE)
        self.assertEqual(waived.details["allow_workflows"], True)

    def test_pull_request_trigger_waivable_only_by_allow_workflows(self):
        # Spec: push/pull_request workflows force NEEDS_REVIEW unless the repo
        # is explicitly allowlisted; allow_workflows=True is that allowlist.
        text = ("on:\n  pull_request:\n    branches:\n      - main\n"
                "jobs:\n  b:\n    steps:\n      - run: echo hi\n")
        gh = TriggerGH()
        gh.contents[("main", ".github/workflows")] = [
            entry("pr.yml", ".github/workflows/pr.yml", 90)]
        gh.contents[("main", ".github/workflows/pr.yml")] = blob(text)
        blocked = triggers.preflight(gh, REPO)
        self.assertEqual(blocked.verdict, triggers.REVIEW)
        self.assertTrue(any("pull_request" in r for r in blocked.reasons))
        waived = triggers.preflight(gh, REPO, allow_workflows=True)
        self.assertEqual(waived.verdict, triggers.SAFE)

    def test_issue_comment_trigger_blocks(self):
        gh = TriggerGH()
        gh.contents[("main", ".github/workflows")] = [
            entry("c.yml", ".github/workflows/c.yml", 60)]
        gh.contents[("main", ".github/workflows/c.yml")] = blob(
            "on:\n  issue_comment:\n    types: [created]\njobs:\n  b:\n    steps:\n      - run: echo hi\n")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("issue_comment" in r for r in report.reasons))

    def test_schedule_is_informational_only(self):
        text = ("on:\n  schedule:\n    - cron: '0 1 * * *'\n"
                "jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: nightly\n")
        gh = TriggerGH()
        gh.contents[("main", ".github/workflows")] = [
            entry("nightly.yml", ".github/workflows/nightly.yml", 120)]
        gh.contents[("main", ".github/workflows/nightly.yml")] = blob(text)
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.SAFE)
        self.assertEqual(report.details["schedule_workflows"],
                         ["nightly.yml@main"])

    def test_lfs_in_gitattributes_on_head_is_flagged(self):
        gh = TriggerGH()
        gh.contents[("feature", ".gitattributes")] = blob(
            "*.bin filter=lfs diff=lfs merge=lfs\n", name=".gitattributes",
            path=".gitattributes")
        report = triggers.preflight(gh, REPO, head_ref="feature")
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any(r.startswith("lfs") for r in report.reasons))

    def test_missing_gitattributes_is_fine(self):
        gh = TriggerGH()
        report = triggers.preflight(gh, REPO, head_ref="feature")
        self.assertEqual(report.details["gitattributes"], "missing")

    def test_inaccessible_rulesets_yields_needs_review(self):
        gh = TriggerGH()
        gh.errors["rulesets"] = RuntimeError("HTTP 403: forbidden")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("rulesets" in r for r in report.reasons))
        self.assertIn("rulesets", report.details["inaccessible"])

    def test_owner_allowlisted_rulesets_record_unverified_but_safe(self):
        gh = TriggerGH()
        gh.errors["rulesets"] = RuntimeError(
            "HTTP 403: Upgrade to GitHub Pro or make this repository public")
        report = triggers.preflight(gh, REPO, allow_unknown_rulesets=True)
        self.assertEqual(report.verdict, triggers.SAFE)
        self.assertIn("unverified", str(report.details.get("rulesets", "")))
        # the other guard surfaces still hold when they fail
        gh.errors["repo"] = RuntimeError("HTTP 403: org sso required")
        report2 = triggers.preflight(gh, REPO, allow_unknown_rulesets=True)
        self.assertEqual(report2.verdict, triggers.REVIEW)

    def test_private_repo_plan_limit_is_unverified_not_held(self):
        gh = TriggerGH()
        gh.repo_payload = dict(gh.repo_payload, private=True)
        gh.errors["rulesets"] = RuntimeError(
            "HTTP 403: Upgrade to GitHub Pro or make this repository public")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.SAFE)
        self.assertIn("plan without ruleset access", report.details["rulesets"])

    def test_public_repo_plan_limit_still_holds(self):
        gh = TriggerGH()
        gh.repo_payload = dict(gh.repo_payload, private=False)
        gh.errors["rulesets"] = RuntimeError(
            "HTTP 403: Upgrade to GitHub Pro or make this repository public")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)

    def test_missing_rulesets_is_fine(self):
        gh = TriggerGH()
        gh.errors["rulesets"] = RuntimeError(NOT_FOUND)
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.details["rulesets"], "none")

    def test_copilot_config_in_rulesets_is_flagged(self):
        gh = TriggerGH()
        gh.rulesets = [{"id": 1, "name": "Copilot review policy", "rules": []}]
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("copilot" in r.lower() for r in report.reasons))

    def test_repo_metadata_failure_blocks_everything(self):
        gh = TriggerGH()
        gh.errors["repo"] = RuntimeError("HTTP 403: org sso required")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("metadata" in r for r in report.reasons))
        self.assertEqual(report.details["refs_inspected"], [])

    def test_oversized_workflow_flagged_without_reading_it(self):
        gh = TriggerGH()
        gh.contents[("main", ".github/workflows")] = [
            entry("big.yml", ".github/workflows/big.yml", 70_000)]
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("too large" in r for r in report.reasons))
        self.assertFalse(any(".github/workflows/big.yml?ref=" in p
                             for _, p in gh.calls))  # content was never fetched

    def test_head_ref_workflows_are_inspected_too(self):
        gh = TriggerGH()
        gh.contents[("feature", ".github/workflows")] = [
            entry("added.yml", ".github/workflows/added.yml", 40)]
        gh.contents[("feature", ".github/workflows/added.yml")] = blob(
            "on: [pull_request]\njobs:\n  b:\n    steps:\n      - run: echo\n")
        report = triggers.preflight(gh, REPO, head_ref="feature")
        self.assertEqual(report.verdict, triggers.REVIEW)
        listed = [p for _, p in gh.calls if ".github/workflows?ref=" in p]
        self.assertEqual(len(listed), 2)  # main AND feature listings
        self.assertTrue(any("added.yml@feature" in r for r in report.reasons))

    def test_workflow_listing_error_yields_needs_review(self):
        gh = TriggerGH()
        gh.errors[("main", ".github/workflows")] = RuntimeError("HTTP 500")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any(".github/workflows" in r for r in report.reasons))

    def test_publish_step_is_flagged(self):
        gh = TriggerGH()
        gh.contents[("main", ".github/workflows")] = [
            entry("rel.yml", ".github/workflows/rel.yml", 80)]
        gh.contents[("main", ".github/workflows/rel.yml")] = blob(
            "on: workflow_dispatch\njobs:\n  b:\n    steps:\n"
            "      - name: Publish to PyPI\n        run: twine upload dist/*\n")
        report = triggers.preflight(gh, REPO)
        self.assertEqual(report.verdict, triggers.REVIEW)
        self.assertTrue(any("publish" in r.lower() for r in report.reasons))

    def test_safe_report_line(self):
        line = triggers.verdict_line(triggers.TriggerReport())
        self.assertIn("SAFE_TO_WRITE", line)
        self.assertNotIn("\n", line)


class ScannerUnitTests(unittest.TestCase):
    def test_inline_and_block_forms(self):
        self.assertEqual(triggers._scan_triggers("on: [push, pull_request_target]\n"),
                         ["pull_request_target", "push"])
        self.assertEqual(triggers._scan_triggers(
            "on:\n  push:\n    branches: [main]\n  workflow_call:\njobs: {}\n"),
            ["push", "workflow_call"])
        self.assertEqual(triggers._scan_triggers("on: schedule\n"), ["schedule"])
        self.assertEqual(triggers._scan_triggers('"on":\n  - pull_request\n'),
                         ["pull_request"])
        self.assertEqual(triggers._scan_triggers("# on: push\njobs: {}\n"), [])
        self.assertEqual(triggers._scan_triggers(""), [])

    def test_step_and_runner_scanners(self):
        text = ("jobs:\n  b:\n    runs-on: [ubuntu-latest]\n    steps:\n"
                "      - run: npm publish\n      - uses: actions/checkout@v4\n"
                "  c:\n    runs-on: self-hosted\n")
        self.assertEqual(triggers._step_flags(text), ["publish"])
        self.assertEqual(triggers._hosted_runners(text), ["ubuntu-latest"])

    def test_lfs_detector(self):
        self.assertTrue(triggers._has_lfs_filter("*.psd filter=lfs\n"))
        self.assertFalse(triggers._has_lfs_filter("*.log text auto\n"))


if __name__ == "__main__":
    unittest.main()
