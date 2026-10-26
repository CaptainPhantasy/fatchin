#!/usr/bin/env python3
"""End-to-end demonstration of gha-worker on this machine (2026-10-17).

Real components: JobExecutor + JobQueue + WorktreeOpsAdapter + ModelClient +
BrokerLeaseBearer + CodingAgent (live your-model via the secret-broker
proxy). Mocked: only the GitHub REST/GraphQL edge (FakeGh) and the git
"remote" (a local bare clone standing in for github.com), because this machine
has no GitHub.com credential yet.

The planted defect: total() sums range(1, len(nums)) — it skips the first
element. The review thread asks for a fix; the worker must correct the code,
make the test suite pass, push the branch, answer the thread, and resolve it.

Spend bound: cfg max_calls_per_job=15 small calls; run once, then discard.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # for tests.mock_github

from ghagent.executor import JobExecutor          # noqa: E402
from ghagent.github_client import GitHubClient    # noqa: E402
from ghagent.interfaces import Config, Job, JobKind, RepoConfig  # noqa: E402
from ghagent.queue import JobQueue                # noqa: E402
from ghagent.wiring import (                      # noqa: E402
    WorktreeOpsAdapter, make_coding_agent_factory, _safe_name)
from tests.mock_github import FakeGh              # noqa: E402

REPO = "local/demo"


def sh(argv, cwd=None):
    rc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if rc.returncode != 0:
        raise RuntimeError(f"{argv} failed rc={rc.returncode}: {rc.stderr[:400]}")
    return rc.stdout.strip()


def build_fixture(base: Path) -> tuple[Path, str, str]:
    """Seed repo with the planted bug; returns (bare_dir, pr_head_sha, base_sha)."""
    seed = base / "seed"
    seed.mkdir()
    sh(["git", "init", "-q", "-b", "main", str(seed)])
    sh(["git", "-C", str(seed), "config", "user.name", "fixture"])
    sh(["git", "-C", str(seed), "config", "user.email", "fixture@local"])
    (seed / "bug.py").write_text(
        "def total(nums):\n"
        "    \"\"\"Sum every number in nums.\"\"\"\n"
        "    s = 0\n"
        "    for i in range(1, len(nums)):\n"
        "        s += nums[i]\n"
        "    return s\n"
    )
    (seed / "tests").mkdir()
    (seed / "tests" / "test_bug.py").write_text(
        "import unittest\n"
        "from bug import total\n"
        "\n"
        "class TestTotal(unittest.TestCase):\n"
        "    def test_sums_all_elements(self):\n"
        "        self.assertEqual(total([1, 2, 3]), 6)\n"
        "\n"
        "    def test_empty(self):\n"
        "        self.assertEqual(total([]), 0)\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    )
    (seed / "tests" / "__init__.py").write_text("")
    sh(["git", "-C", str(seed), "add", "-A"])
    sh(["git", "-C", str(seed), "commit", "-q", "-m", "seed: total()"])
    base_sha = sh(["git", "-C", str(seed), "rev-parse", "HEAD"])

    bare = base / "origin.git"
    sh(["git", "clone", "-q", "--bare", str(seed), str(bare)])
    sh(["git", "-C", str(bare), "config", "user.name", "fixture"])
    sh(["git", "-C", str(bare), "config", "user.email", "fixture@local"])
    # (clone --bare already configured origin -> seed, so pushes from a
    # worktree land in the seed repo)

    # The PR branch: feature-x at the seed commit (bug present).
    sh(["git", "-C", str(bare), "update-ref", "refs/heads/feature-x", base_sha])
    sh(["git", "-C", str(bare), "update-ref", "refs/pull/1/head", base_sha])
    return bare, base_sha, base_sha


def make_fake_gh(head_sha: str) -> FakeGh:
    fake = FakeGh()
    pr = {
        "number": 1, "url": "https://github.invalid/local/demo/pull/1",
        "state": "OPEN", "title": "Add total()",
        "body": "Adds a total() helper.",
        "headRefOid": head_sha, "headRefName": "feature-x",
        "baseRefName": "main", "baseRefOid": head_sha,
        "headRepository": {"name": "demo"},
        "headRepositoryOwner": {"login": "someone"},
        "isCrossRepository": False, "maintainerCanModify": True,
    }
    rest_comment = {
        "id": 555, "node_id": "PRRC_fixture0001",
        "body": "total() skips the first element — range(1, len) drops index 0.",
        "user": {"id": 1001, "login": "reviewer"},
        "path": "bug.py", "line": 3, "in_reply_to_id": None,
    }
    graphql_body = {
        "data": {"repository": {"pullRequest": {
            "headRefOid": head_sha,
            "reviewThreads": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "id": "PRRT_fixturethread1",
                    "isResolved": False, "isOutdated": False,
                    "viewerCanResolve": True,
                    "path": "bug.py", "line": 3,
                    "comments": {"nodes": [{"id": "PRRC_fixture0001"}]},
                }],
            },
        }}}}

    def handler(argv, input=None):
        joined = " ".join(argv)
        if joined.startswith("pr view"):
            return 0, json.dumps(pr), ""
        if "--paginate" in argv and "issues/1/comments" in joined:
            return 0, json.dumps([[  # one page, conversation comments
                {"id": 500, "body": "Please take a look.",
                 "user": {"id": 1001, "login": "reviewer"}},
            ]]), ""
        if "--paginate" in argv and "pulls/1/reviews" in joined:
            return 0, json.dumps([[]]), ""
        if "--paginate" in argv and "pulls/1/comments" in joined:
            return 0, json.dumps([[rest_comment]]), ""
        if "graphql" in argv and "resolveReviewThread" in joined:
            return 0, json.dumps(
                {"data": {"resolveReviewThread": {
                    "thread": {"id": "PRRT_fixturethread1", "isResolved": True}}}}), ""
        if joined.startswith("api --method GET") and joined.endswith("repos/local/demo"):
            return 0, json.dumps({
                "full_name": "local/demo", "default_branch": "main",
                "private": True, "archived": False,
                "owner": {"login": "local", "type": "User"},
            }), ""
        if "repos/local/demo/rulesets" in joined:
            return 0, json.dumps([]), ""
        if "repos/local/demo" in joined and joined.rstrip("/").endswith("vulnerability-alerts"):
            return 0, json.dumps({"enabled": False}), ""
        if "graphql" in argv:
            return 0, json.dumps(graphql_body), ""
        if "contents/.github/workflows" in joined or "contents/.gitattributes" in joined:
            return 1, "", "HTTP 404: Not Found"
        if joined.startswith("pr checks"):
            return 0, json.dumps([]), ""  # no remote checks configured
        if joined.startswith("pr comment"):
            return 0, "https://github.invalid/local/demo/pull/1#issuecomment-9001", ""
        if "/comments/555/replies" in joined:
            return 0, json.dumps({"id": 9002}), ""
        return 1, "", f"e2e handler: unmatched gh call: {joined[:160]}"

    fake.handler = handler
    return fake


class LocalBareAdapter(WorktreeOpsAdapter):
    """WorktreeOpsAdapter whose 'origin' is the local bare fixture (stands in
    for github.com until the machine has a GitHub credential). The fixture
    carries the pull ref directly, so fetch is a local update-ref."""

    def __init__(self, cfg: Config, bare: Path, pr_head_sha: str):
        super().__init__(cfg)
        self._bare = bare
        self._pr_head_sha = pr_head_sha

    def fetch_pr_worktree(self, repo_full_name: str, pr_number: int,
                          head_ref: str = "") -> str:
        ref = f"refs/remotes/origin/pr-{pr_number}"
        sh(["git", "-C", str(self._bare), "update-ref", ref, self._pr_head_sha])
        dest = self._root / _safe_name(
            f"{repo_full_name}-pr{pr_number}-{head_ref or 'head'}")
        rc = self._git(["worktree", "add", "--detach", str(dest), ref],
                       cwd=str(self._bare))
        if rc.returncode != 0:
            raise RuntimeError(f"worktree add failed: {rc.stderr.strip()[:300]}")
        return str(dest)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gha-e2e-"))
    print(f"fixture root: {tmp}")
    bare, head_sha, _ = build_fixture(tmp)
    print(f"fixture bare repo: {bare}  (PR head {head_sha[:10]})")

    cfg = Config(
        github_host="github.invalid",
        state_dir=str(tmp / "state"),
        worktree_root=str(tmp / "worktrees"),
        repos=[RepoConfig(owner="local", name="demo", base_branch="main",
                          checks=["python3 -m unittest discover -s tests -t ."])],
    )
    cfg.model.max_calls_per_job = 15

    fake = make_fake_gh(head_sha)
    client = GitHubClient(host="github.invalid", runner=fake)

    queue = JobQueue(cfg.state_dir)
    (Path(cfg.state_dir)).mkdir(parents=True, exist_ok=True)

    # Live model through the broker proxy (your-model, short-lived lease).
    from ghagent.wiring import build_model_client
    import os
    env = {"MCP_SECRET_BROKER_ADMIN_TOKEN": os.environ.get("MCP_SECRET_BROKER_ADMIN_TOKEN", "")}
    model_client = build_model_client(cfg, env)
    print(f"model: {cfg.model.model_id} via {cfg.model.base_url}")

    executor = JobExecutor(
        client, queue, cfg,
        make_coding_agent_factory(cfg, model_client),
        worktree_ops=LocalBareAdapter(cfg, bare, head_sha),
    )

    job = Job(
        job_id="job_e2e_demo_0001", kind=JobKind.FIX_PR, repo_full_name=REPO,
        number=1, command_comment_id=500, requested_by=42,
        head_sha=head_sha, dedup_key=f"fix-pr:{REPO}:1:{head_sha}",
    )
    queue.add(job)

    done = executor.execute(job)
    print("=" * 60)
    print(f"final state      : {done.state.value}")
    print(f"error            : {done.error or '(none)'}")
    print(f"produced commits : {done.produced_commits}")
    print(f"posted comments  : {done.posted_comment_ids}")
    print(f"resolved threads : {done.resolved_thread_ids}")
    print(f"model calls used : {done.model_calls_used}")
    print(f"tested commit    : {done.notes.get('tested_commit', '')[:10]}")
    print(f"trigger verdict  : {done.notes.get('verdict', '')}")
    for check in (done.notes.get("checks") or []):
        print(f"  check: passed={check.get('passed')} :: {check.get('command')}")

    branch_head = ""
    try:
        branch_head = sh(["git", "-C", str(bare), "rev-parse", "refs/heads/feature-x"])
    except RuntimeError:
        pass
    pushed = branch_head == (done.produced_commits[0] if done.produced_commits else "")
    print(f"remote branch    : feature-x -> {branch_head[:10] or '(absent)'} "
          f"pushed={pushed}")

    ok = (
        done.state.value == "DONE"
        and bool(done.produced_commits) and pushed
        and 9001 in done.posted_comment_ids
        and done.resolved_thread_ids == ["PRRT_fixturethread1"]
        and done.model_calls_used > 0
    )
    print("=" * 60)
    print("E2E DEMO RESULT:", "PASS" if ok else "FAIL")
    if not ok:
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
