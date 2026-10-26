"""Executor flow tests: full fakes only — no network, no sibling modules."""
import base64
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from ghagent import executor, interfaces, responder
from ghagent.executor import JobExecutor
from ghagent.interfaces import (AgentResult, Config, Job, JobKind, JobState,
                                RepoConfig)

REPO = "o/r"
NUM = 12
WT_PATH = "/tmp/fake-wt/job1"
COMMIT = "deadbeefcafe1234"
START_SHA = "startsha123"
NOT_FOUND = "GET failed: 404 Not Found"


def make_job(**over):
    data = dict(job_id="job_test_1", kind=JobKind.FIX_PR, repo_full_name=REPO,
                number=NUM, command_comment_id=555, requested_by=42,
                head_sha=START_SHA, dedup_key="fix-pr:o:r:12:" + START_SHA)
    data.update(over)
    return Job(**data)


def default_pr(**over):
    pr = {"number": NUM, "state": "OPEN", "title": "Fix the thing",
          "url": f"https://github.com/{REPO}/pull/{NUM}",
          "headRefName": "feature-x", "headRefOid": START_SHA,
          "baseRefName": "main", "isCrossRepository": False,
          "maintainerCanModify": False, "author": {"login": "alice"}}
    pr.update(over)
    return pr


def thread(tid, path, line, first_gid, resolved=False, outdated=False,
           can_resolve=True):
    return {"id": tid, "isResolved": resolved, "isOutdated": outdated,
            "viewerCanResolve": can_resolve, "path": path, "line": line,
            "comments": {"nodes": [{"id": first_gid}]}}


def thread_page(has_next, cursor, nodes):
    return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": nodes}


def blob(text, name="wf.yml", path=".github/workflows/wf.yml"):
    data = text.encode("utf-8")
    return {"type": "file", "name": name, "path": path, "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"), "size": len(data)}


def wf_entry(name="wf.yml", path=".github/workflows/wf.yml", size=100):
    return {"type": "file", "name": name, "path": path, "size": size}


class FakeGH:
    """interfaces.GitHubClientProto fake with canned paged payloads."""

    def __init__(self):
        self.repo_payload = {"default_branch": "main", "full_name": REPO}
        self.rulesets = []
        self.contents = {}            # (ref, path) -> entries | blob | "404"
        self.remote_branches = None   # None -> every branch 404s
        self.issue = {"number": 7, "title": "Add widget", "state": "open",
                      "body": "build a widget", "user": {"login": "dana"}}
        self.issue_comments = []
        self.reviews = []
        self.review_comments = []
        self.parent_comments = {}     # numeric id -> REST comment dict
        self.commit_dates = {COMMIT: "2026-10-17T10:00:00Z"}
        self.checks = []
        self.pr = default_pr()
        self.pr_sequence = []
        self.thread_pages = {}        # after-cursor -> page
        self.thread_comment_pages = {}  # thread id -> {after: page}
        self.thread_last_comment = {}   # thread id -> [createdAt]
        self.rest_calls = []
        self.graphql_calls = []
        self.resolved_calls = []
        self.run_view_calls = []
        self.pr_view_calls = 0

    # -- proto surface --------------------------------------------------------

    def rest(self, method, path, body=None, paginate=False):
        self.rest_calls.append((method, path, paginate))
        if path == f"repos/{REPO}":
            return self.repo_payload
        if path == f"repos/{REPO}/rulesets":
            return self.rulesets
        if path.startswith(f"repos/{REPO}/contents/"):
            return self._content(path)
        if path.startswith(f"repos/{REPO}/issues/"):
            return self.issue_comments
        if path.startswith(f"repos/{REPO}/pulls/{NUM}/reviews"):
            return self.reviews
        if path.startswith(f"repos/{REPO}/pulls/{NUM}/comments"):
            return self.review_comments
        if path.startswith(f"repos/{REPO}/pulls/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            return self.parent_comments[cid]
        if path.startswith(f"repos/{REPO}/branches/"):
            name = unquote(path.split("/branches/", 1)[1])
            if self.remote_branches and name in self.remote_branches:
                return {"name": name,
                        "commit": {"sha": self.remote_branches[name]}}
            raise RuntimeError(f"{NOT_FOUND}: branches/{name}")
        if path.startswith(f"repos/{REPO}/commits/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.commit_dates:
                return {"sha": sha, "commit": {"committer": {
                    "date": self.commit_dates[sha]}}}
            raise RuntimeError(f"{NOT_FOUND}: commits/{sha}")
        raise AssertionError(f"unexpected rest call: {method} {path}")

    def _content(self, path):
        base, _, qs = path.partition("?")
        ref = unquote(qs.split("ref=", 1)[1].split("&", 1)[0]) if "ref=" in qs else ""
        rel = unquote(base.split("/contents/", 1)[1])
        val = self.contents.get((ref, rel), "404")
        if val == "404":
            raise RuntimeError(f"{NOT_FOUND}: contents/{rel}?ref={ref}")
        if isinstance(val, Exception):
            raise val
        return val

    def graphql(self, query, variables=None, paginate_path=None):
        variables = variables or {}
        self.graphql_calls.append((query, dict(variables)))
        if "reviewThreads(first:" in query:
            page = self.thread_pages[variables.get("after")]
            return {"repository": {"pullRequest": {"reviewThreads": page}}}
        if "comments(last: 1)" in query:
            stamps = self.thread_last_comment.get(variables.get("id") or "", [])
            nodes = [{"createdAt": stamps[0]}] if stamps else []
            return {"node": {"comments": {"nodes": nodes}}}
        if "comments(first:" in query:
            pages = self.thread_comment_pages.get(variables.get("id") or "", {})
            page = pages.get(variables.get("after"))
            if page is None:
                raise AssertionError("no canned page for thread comments")
            return {"node": {"comments": page}}
        if "resolveReviewThread" in query:
            self.resolved_calls.append(variables["threadId"])
            return {"resolveReviewThread": {"thread": {
                "id": variables["threadId"], "isResolved": True}}}
        raise AssertionError(f"unexpected graphql query: {query[:60]}")

    def pr_view(self, repo, number):
        self.pr_view_calls += 1
        if self.pr_sequence:
            return self.pr_sequence.pop(0)
        return self.pr

    def issue_view(self, repo, number):
        return self.issue

    def pr_checks(self, repo, number):
        return self.checks

    def run_view(self, repo, run_id):
        self.run_view_calls.append(run_id)
        return {"conclusion": "FAILURE", "url": f"https://github.com/{REPO}/actions/runs/{run_id}"}

    def current_login(self): return "gha-worker-bot"
    def current_user_id(self): return 424242


class FakeQueue:
    def __init__(self, job=None):
        self.jobs = {}
        self.history = []  # (state.value, notes snapshot)
        self.auto_stop_after_state = None  # simulate a concurrent /agent stop
        if job is not None:
            self.add(job)

    def add(self, job):
        self.jobs[job.job_id] = job
        return True

    def get(self, job_id):
        return self.jobs.get(job_id)

    def update(self, job):
        self.history.append((job.state.value, dict(job.notes)))
        self.jobs[job.job_id] = job
        if self.auto_stop_after_state is not None \
                and job.state is self.auto_stop_after_state:
            self.jobs[job.job_id].state = JobState.STOPPED

    def find_open_by_repo_number(self, repo, number): return None
    def all(self): return list(self.jobs.values())
    def stop_job(self, job_id):
        if job_id in self.jobs:
            self.jobs[job_id].state = JobState.STOPPED
            return True
        return False
    def reconcile_with_remote(self, gh): return []


class FakeWT:
    def __init__(self, path=WT_PATH, sha=COMMIT):
        self.path, self.sha = path, sha
        self.calls = []

    def add_worktree(self, path, base_ref, repo_full_name=None):
        self.calls.append(("add_worktree", path, base_ref))
        return path

    def fetch_pr_worktree(self, repo, number, head_ref):
        self.calls.append(("fetch_pr_worktree", repo, number, head_ref))
        return self.path

    def commit_all(self, wt, message):
        self.calls.append(("commit_all", wt, message))
        return self.sha

    def is_clean(self, wt): return True

    def current_sha(self, wt):
        self.calls.append(("current_sha", wt))
        return self.sha

    def remove_worktree(self, wt): self.calls.append(("remove_worktree", wt))
    def prune(self): self.calls.append(("prune",))


class FakeAgent:
    """Callable factory returning itself; .run() records the instruction."""

    def __init__(self, result):
        self.result = result
        self.instructions = []
        self.instantiations = 0

    def __call__(self, worktree_path=None, checks=None):
        self.instantiations += 1
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        return self.result


def agent_result(checks=None, changed=None, error=""):
    return AgentResult(
        summary="Fixed the reported problems.",
        changed_files=changed if changed is not None
        else ["src/a.py", "src/c.py", "src/other.py"],
        worktree_path=WT_PATH, final_commit_sha=COMMIT,
        checks=checks if checks is not None
        else [{"command": "make test", "exit": 0, "passed": True,
               "output_tail": "all green"}],
        model_calls_used=3, error=error)


class RecordGit:
    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls = []

    def __call__(self, args, cwd, timeout=600.0):
        self.calls.append((list(args), cwd, timeout))
        return self.rc, self.out, self.err


class RecordGh:
    """Stands in for responder._run_gh (the gh CLI subprocess)."""

    def __init__(self):
        self.calls = []
        self.bodies = []
        self._cid = 9001
        self._rid = 9101
        self.pr_url = f"https://github.com/{REPO}/pull/21"

    def __call__(self, args, timeout=120.0):
        self.calls.append((list(args), timeout))
        if args[:2] == ["pr", "comment"]:
            self._read(args)
            return 0, f"https://github.com/{REPO}/pull/12#issuecomment-{self._cid}\n", ""
        if args[:2] == ["pr", "create"]:
            self._read(args)
            return 0, self.pr_url + "\n", ""
        if args[:1] == ["api"]:
            self._read(args)
            rid = self._rid
            self._rid += 1
            return 0, json.dumps({"id": rid}), ""
        return 1, "", f"unexpected gh argv: {args[:2]}"

    def _read(self, args):
        if "--body-file" in args:
            path = args[args.index("--body-file") + 1]
        elif "-F" in args:
            path = args[args.index("-F") + 1].split("@", 1)[1]
        else:
            return
        with open(path, encoding="utf-8") as fh:
            self.bodies.append(fh.read())

    def api_roots(self):
        roots = []
        for args, _ in self.calls:
            if args[:1] == ["api"]:
                m = re.search(r"comments/(\d+)/replies", " ".join(args))
                roots.append(int(m.group(1)))
        return roots

    def pr_comment_calls(self):
        return [args for args, _ in self.calls if args[:2] == ["pr", "comment"]]

    def pr_create_calls(self):
        return [args for args, _ in self.calls if args[:2] == ["pr", "create"]]


def happy_gh():
    gh = FakeGH()
    gh.reviews = [{"user": {"login": "alice"}, "state": "CHANGES_REQUESTED",
                   "body": "please fix"}]
    gh.review_comments = [
        {"id": 111, "node_id": "GC1", "in_reply_to_id": None,
         "user": {"login": "alice"}, "body": "a is wrong",
         "created_at": "2026-10-17T08:00:00Z"},
        {"id": 222, "node_id": "GC2", "in_reply_to_id": None,
         "user": {"login": "bob"}, "body": "b is wrong",
         "created_at": "2026-10-17T08:05:00Z"},
        {"id": 333, "node_id": "GC8", "in_reply_to_id": None,
         "user": {"login": "carol"}, "body": "c is wrong",
         "created_at": "2026-10-17T08:10:00Z"},
    ]
    gh.thread_pages = {
        None: thread_page(True, "CUR1", [
            thread("T1", "src/a.py", 10, "GC1"),
            thread("T2", "src/b.py", 5, "GC2", can_resolve=False),
        ]),
        "CUR1": thread_page(False, None, [thread("T3", "src/c.py", 7, "GC9")]),
    }
    gh.thread_comment_pages = {
        "T3": {None: thread_page(False, None,
                                 [{"id": "GC9"}, {"id": "GC8"}])},
    }
    gh.thread_last_comment = {
        "T1": ["2026-10-17T09:00:00Z"],
        "T2": ["2026-10-17T09:30:00Z"],
        "T3": ["2026-10-17T11:00:00Z"],  # after the fix commit
    }
    gh.checks = [{"name": "ci/test", "state": "FAILURE",
                  "link": f"https://github.com/{REPO}/actions/runs/999"}]
    return gh


def default_cfg(checks=("make test",)):
    return Config(state_dir="/tmp/state", worktree_root="/tmp/wt-root",
                  repos=[RepoConfig(owner="o", name="r", base_branch="main",
                                    checks=list(checks))])


def run_job(job, gh, cfg=None, wt=None, agent=None):
    wt = wt or FakeWT()
    agent = agent or FakeAgent(agent_result())
    queue = FakeQueue(job)
    exe = JobExecutor(gh, queue, cfg or default_cfg(), agent, worktree_ops=wt)
    git_rec, gh_rec = RecordGit(), RecordGh()
    with patch.object(executor, "_run_git", git_rec), \
            patch.object(responder, "_run_gh", gh_rec):
        returned = exe.execute(job)
    return returned, queue, wt, agent, git_rec, gh_rec


class FixPrHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.job = make_job()
        self.gh = happy_gh()
        (self.job, self.queue, self.wt, self.agent,
         self.git, self.gh_rec) = run_job(self.job, self.gh)

    def test_reaches_done_with_full_state_transitions(self):
        self.assertEqual(self.job.state, JobState.DONE, self.job.error)
        states = [s for s, _ in self.queue.history]
        self.assertEqual(states[0], "retrieving")
        for expected in ("working", "verifying", "responding", "done"):
            self.assertIn(expected, states)
        self.assertEqual(states[-1], "done")

    def test_retrieval_pulls_three_paged_collections_and_run_evidence(self):
        paginated = [p for m, p, pag in self.gh.rest_calls if pag]
        self.assertIn(f"repos/{REPO}/issues/{NUM}/comments", paginated)
        self.assertIn(f"repos/{REPO}/pulls/{NUM}/reviews", paginated)
        self.assertIn(f"repos/{REPO}/pulls/{NUM}/comments", paginated)
        self.assertEqual(self.gh.run_view_calls, [999])

    def test_review_threads_fetched_with_pagination(self):
        afters = [v.get("after") for q, v in self.gh.graphql_calls
                  if "reviewThreads" in q]
        self.assertEqual(afters, [None, "CUR1"])

    def test_push_argv_is_explicit_and_never_forced(self):
        self.assertEqual(len(self.git.calls), 1)
        args, cwd, _ = self.git.calls[0]
        self.assertEqual(args, ["push", "origin", f"HEAD:refs/heads/feature-x"])
        self.assertNotIn("force", " ".join(args).lower())
        self.assertEqual(cwd, WT_PATH)

    def test_worktree_used_for_pr_head_and_cleaned_after_success(self):
        self.assertIn(("fetch_pr_worktree", REPO, NUM, "feature-x"),
                      self.wt.calls)
        self.assertIn(("remove_worktree", WT_PATH), self.wt.calls)
        self.assertIn(("prune",), self.wt.calls)

    def test_summary_comment_posted_with_marker_and_job_id(self):
        calls = self.gh_rec.pr_comment_calls()
        self.assertEqual(len(calls), 1)
        summary = self.gh_rec.bodies[-1]
        self.assertTrue(summary.startswith(interfaces.COMMENT_MARKER))
        self.assertIn("job_id=job_test_1", summary.splitlines()[0])
        self.assertIn("Review threads resolved: 1", summary)
        self.assertIn(COMMIT, summary)

    def test_inline_replies_use_numeric_root_ids(self):
        self.assertEqual(sorted(self.gh_rec.api_roots()), [111, 222, 333])
        for body in self.gh_rec.bodies[:3]:
            self.assertTrue(body.startswith(interfaces.COMMENT_MARKER))

    def test_thread_resolved_only_with_complete_evidence(self):
        self.assertEqual(self.job.resolved_thread_ids, ["T1"])
        self.assertEqual(self.gh.resolved_calls, ["T1"])

    def test_job_records_are_persisted_before_return(self):
        self.assertEqual(self.job.produced_commits, [COMMIT])
        # replies are posted first, the summary comment last
        self.assertEqual(self.job.posted_comment_ids, [9101, 9102, 9103, 9001])
        self.assertEqual(self.job.model_calls_used, 3)
        self.assertEqual(self.job.notes["tested_base"], COMMIT)


class EvidenceGatingTests(unittest.TestCase):
    def test_failing_checks_fail_verification_before_any_write(self):
        agent = FakeAgent(agent_result(checks=[
            {"command": "make test", "exit": 1, "passed": False,
             "output_tail": "3 tests failed"}]))
        gh = happy_gh()
        job, queue, wt, agent, git, gh_rec = run_job(make_job(), gh,
                                                     agent=agent)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("verification failed", job.error)
        self.assertIn("make test", job.error)
        states = [s for s, _ in queue.history]
        self.assertNotIn("responding", states)
        self.assertEqual(git.calls, [])
        self.assertEqual(gh_rec.calls, [])
        self.assertEqual(gh.resolved_calls, [])

    def test_configured_checks_not_run_by_agent_fail_verification(self):
        agent = FakeAgent(agent_result(checks=[]))
        job, _, _, _, git, gh_rec = run_job(make_job(), happy_gh(), agent=agent)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("reported no check results", job.error)
        self.assertEqual(git.calls, [])

    def test_no_checks_configured_is_local_tests_only_and_never_resolves(self):
        agent = FakeAgent(agent_result(checks=[]))
        job, _, _, _, git, gh_rec = run_job(
            make_job(), happy_gh(), cfg=default_cfg(checks=()), agent=agent)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertIn("no repo checks configured", job.notes["verification"])
        self.assertEqual(job.resolved_thread_ids, [])  # never claimed as passing
        self.assertEqual(len(git.calls), 1)  # push still proceeds, labelled local-only

    def test_new_comment_after_fix_commit_blocks_resolution(self):
        agent = FakeAgent(agent_result(changed=["src/c.py"]))
        job, _, _, _, git, gh_rec = run_job(make_job(), happy_gh(), agent=agent)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertNotIn("T3", job.resolved_thread_ids)
        texts = gh_rec.bodies[:3]
        self.assertTrue(any("new comment arrived after the fix commit" in t
                            for t in texts))

    def test_link_threads_walks_reply_chains(self):
        threads = [thread("T9", "src/x.py", 3, "GC9")]
        comments = [
            {"id": 500, "node_id": "GC7", "in_reply_to_id": None},
            {"id": 501, "node_id": "GC9", "in_reply_to_id": 500},
        ]
        linked = executor.link_threads(threads, comments)
        self.assertEqual(linked[0]["_root_comment_id"], 500)

    def test_unlinked_thread_is_never_dropped(self):
        gh = happy_gh()
        gh.thread_comment_pages = {"T3": {None: thread_page(
            False, None, [{"id": "GC_unknown"}])}}
        job, _, _, _, git, gh_rec = run_job(make_job(), gh)
        self.assertEqual(job.state, JobState.DONE, job.error)
        roots = gh_rec.api_roots()
        self.assertNotIn(333, roots)  # no inline reply without a root id
        summary = gh_rec.bodies[-1]
        self.assertIn("could not be linked", summary)
        self.assertIn("src/c.py", summary)
        self.assertEqual(job.notes["threads_without_root_id"], 1)

    def test_collect_review_threads_pages_through_cursor(self):
        class PagedGH:
            def __init__(self):
                self.afters = []

            def graphql(self, query, variables=None, paginate_path=None):
                v = variables or {}
                self.afters.append(v.get("after"))
                if v.get("after") is None:
                    return {"repository": {"pullRequest": {"reviewThreads":
                            thread_page(True, "C2", [thread("A", "p", 1, "n")])}}}
                return {"repository": {"pullRequest": {"reviewThreads":
                        thread_page(False, None,
                                    [thread("B", "q", 2, "m"),
                                     thread("C", "r", 3, "k")])}}}

        gh = PagedGH()
        threads = executor.collect_review_threads(gh, REPO, NUM)
        self.assertEqual([t["id"] for t in threads], ["A", "B", "C"])
        self.assertEqual(gh.afters, [None, "C2"])


class WriteHoldTests(unittest.TestCase):
    def test_needs_review_verdict_holds_all_remote_writes(self):
        gh = happy_gh()
        gh.contents[("feature-x", ".github/workflows")] = [
            wf_entry("ci.yml", ".github/workflows/ci.yml", 100)]
        gh.contents[("feature-x", ".github/workflows/ci.yml")] = blob(
            "on: [push, pull_request_target]\n"
            "jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
            name="ci.yml", path=".github/workflows/ci.yml")
        job, queue, wt, agent, git, gh_rec = run_job(make_job(), gh)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("NEEDS_REVIEW", job.notes["verdict"])
        self.assertIn("pull_request_target", job.notes["verdict"])
        self.assertIn("pull_request", job.error)
        self.assertEqual(job.notes["trigger_verdict"]["verdict"], "NEEDS_REVIEW")
        self.assertEqual(git.calls, [], "no push may happen on NEEDS_REVIEW")
        self.assertEqual(gh_rec.calls, [], "no comments may happen on NEEDS_REVIEW")

    def test_allow_workflows_note_waives_push_trigger(self):
        gh = happy_gh()
        gh.contents[("feature-x", ".github/workflows")] = [
            wf_entry("ci.yml", ".github/workflows/ci.yml", 90)]
        gh.contents[("feature-x", ".github/workflows/ci.yml")] = blob(
            "on:\n  push:\n    branches:\n      - main\njobs:\n  b:\n    steps:\n      - run: echo hi\n",
            name="ci.yml", path=".github/workflows/ci.yml")
        job = make_job()
        job.notes["allow_workflows"] = True
        job, queue, wt, agent, git, gh_rec = run_job(job, gh)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.notes["trigger_verdict"]["verdict"], "SAFE_TO_WRITE")
        self.assertEqual(len(git.calls), 1)

    def test_remote_moved_aborts_merge_free(self):
        gh = happy_gh()
        gh.pr_sequence = [default_pr(), default_pr(headRefOid="movedsha456")]
        job, queue, wt, agent, git, gh_rec = run_job(make_job(), gh)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("remote moved; re-run to rebase", job.error)
        self.assertEqual(job.notes["remote_head_sha"], "movedsha456")
        self.assertEqual(git.calls, [])
        self.assertEqual(gh_rec.calls, [])
        self.assertEqual(gh.pr_view_calls, 2)

    def test_remote_branch_already_existing_aborts_issue_jobs(self):
        gh = happy_gh()
        job = make_job(kind=JobKind.IMPLEMENT_ISSUE, number=7, head_sha="")
        gh.remote_branches = {"ghagent/issue-7": "someothersha"}
        job, queue, wt, agent, git, gh_rec = run_job(job, gh)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("remote moved; re-run to rebase", job.error)
        self.assertIn("ghagent/issue-7", job.error)
        self.assertEqual(git.calls, [])


class StopTests(unittest.TestCase):
    def test_stop_during_retrieving_never_runs_the_agent(self):
        queue_target = FakeQueue()
        queue_target.auto_stop_after_state = JobState.RETRIEVING
        job = make_job()
        queue_target.add(job)
        gh = happy_gh()
        wt = FakeWT()
        agent = FakeAgent(agent_result())
        exe = JobExecutor(gh, queue_target, default_cfg(), agent,
                          worktree_ops=wt)
        git_rec, gh_rec = RecordGit(), RecordGh()
        with patch.object(executor, "_run_git", git_rec), \
                patch.object(responder, "_run_gh", gh_rec):
            out = exe.execute(job)
        self.assertEqual(out.state, JobState.STOPPED)
        self.assertEqual(agent.instantiations, 0)
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh_rec.calls, [])
        self.assertEqual(gh.resolved_calls, [])

    def test_stop_after_working_halts_before_verify_and_writes(self):
        queue_target = FakeQueue()
        queue_target.auto_stop_after_state = JobState.WORKING
        job = make_job()
        queue_target.add(job)
        agent = FakeAgent(agent_result())
        exe = JobExecutor(happy_gh(), queue_target, default_cfg(), agent,
                          worktree_ops=FakeWT())
        git_rec, gh_rec = RecordGit(), RecordGh()
        with patch.object(executor, "_run_git", git_rec), \
                patch.object(responder, "_run_gh", gh_rec):
            out = exe.execute(job)
        self.assertEqual(out.state, JobState.STOPPED)
        self.assertEqual(agent.instantiations, 1)
        states = [s for s, _ in queue_target.history]
        self.assertIn("working", states)
        self.assertNotIn("verifying", states)
        self.assertNotIn("responding", states)
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh_rec.calls, [])


class ImplementIssueTests(unittest.TestCase):
    def issue_gh(self):
        gh = FakeGH()
        gh.issue_comments = [{"user": {"login": "bob"},
                              "body": "use the existing framework",
                              "created_at": "2026-10-17T09:00:00Z"}]
        gh.checks = []
        return gh

    def test_issue_flow_pushes_branch_creates_pr_and_comments(self):
        agent = FakeAgent(AgentResult(
            summary="Widget added.", changed_files=["src/widget.py"],
            worktree_path=WT_PATH, final_commit_sha=COMMIT,
            checks=[{"command": "make test", "exit": 0, "passed": True,
                     "output_tail": "ok"}], model_calls_used=2))
        job = make_job(kind=JobKind.IMPLEMENT_ISSUE, number=7, head_sha="")
        wt = FakeWT()
        job, queue, wt, agent, git, gh_rec = run_job(job, self.issue_gh(),
                                                     wt=wt, agent=agent)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.notes["created_pr"]["number"], 21)
        create = gh_rec.pr_create_calls()
        self.assertEqual(len(create), 1)
        args = create[0]
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("--head") + 1], "ghagent/issue-7")
        self.assertEqual(args[args.index("--repo") + 1], REPO)
        self.assertIn("--body-file", args)
        self.assertEqual(len(git.calls), 1)
        git_args = git.calls[0][0]
        self.assertEqual(git_args, ["push", "origin",
                                    "HEAD:refs/heads/ghagent/issue-7"])
        self.assertNotIn("force", " ".join(git_args).lower())
        comments = gh_rec.pr_comment_calls()
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][comments[0].index("comment") + 1], "21")
        self.assertTrue(gh_rec.bodies[-1].startswith(interfaces.COMMENT_MARKER))
        self.assertIn(("prune",), wt.calls)

    def test_issue_number_that_is_a_pr_fails_without_working(self):
        gh = self.issue_gh()
        gh.issue = {"number": 12, "title": "x", "state": "open",
                    "pull_request": {"url": "https://github.com/o/r/pull/12"}}
        job, queue, wt, agent, git, gh_rec = run_job(
            make_job(kind=JobKind.IMPLEMENT_ISSUE), gh)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("pull request", job.error)
        self.assertEqual(wt.calls, [])
        self.assertEqual(git.calls, [])
        self.assertEqual(gh_rec.calls, [])

    def test_issue_view_shape_guard_is_pull_request_aware(self):
        self.assertTrue(executor._looks_like_pr({"pull_request": {}}))
        self.assertTrue(executor._looks_like_pr({"isPullRequest": True}))
        self.assertFalse(executor._looks_like_pr({"title": "plain issue"}))
        self.assertTrue(executor._looks_like_pr(None))


class ForkedHeadTests(unittest.TestCase):
    def test_forked_pr_head_gets_companion_branch_never_original(self):
        gh = happy_gh()
        gh.pr = default_pr(isCrossRepository=True, maintainerCanModify=False)
        job, queue, wt, agent, git, gh_rec = run_job(make_job(), gh)
        self.assertEqual(job.state, JobState.DONE, job.error)
        args = git.calls[0][0]
        self.assertEqual(args[2], f"HEAD:refs/heads/gha/fix-{NUM}-deadbeef")
        self.assertEqual(job.notes["companion_branch"], f"gha/fix-{NUM}-deadbeef")
        self.assertIn("fork", job.notes["pr_head_refusal"])
        summary = gh_rec.bodies[-1]
        self.assertIn("companion", summary)
        self.assertIn("was not fixed", summary)


class StatusTextTests(unittest.TestCase):
    def test_status_after_happy_path(self):
        job, _, _, _, _, _ = run_job(make_job(), happy_gh())
        text = JobExecutor(happy_gh(), FakeQueue(), default_cfg(),
                           FakeAgent(agent_result())).status_text(job)
        self.assertIn("done", text)
        self.assertIn(COMMIT, text)
        self.assertIn("9001", text)
        self.assertIn("model calls used: 3", text)

    def test_status_on_fresh_job(self):
        job = make_job()
        text = executor.format_status(job)
        self.assertIn("received", text)
        self.assertIn("model calls used: 0", text)


if __name__ == "__main__":
    unittest.main()


class CopilotCancelTests(unittest.TestCase):
    """The worker cancels Copilot review runs that fired on its OWN commits
    (account-level auto-review is not API-visible)."""

    def _gh(self, runs):
        inner = happy_gh()

        class ActionsRecordingGh:
            def __init__(self):
                self.cancelled: list = []

            def rest(self, method, path, body=None, paginate=False):
                if path.endswith("/actions/runs"):
                    return {"workflow_runs": runs}
                if path.endswith("/cancel"):
                    self.cancelled.append(path.rsplit("/", 2)[-2])
                    return {}
                return inner.rest(method, path, body=body, paginate=paginate)

            def __getattr__(self, name):
                return getattr(inner, name)

        return ActionsRecordingGh()

    def test_cancels_only_copilot_runs_on_produced_commits(self):
        runs = [
            {"id": 1, "name": "Copilot", "status": "in_progress",
             "head_sha": COMMIT},                       # ours, copilot → cancel
            {"id": 2, "name": "Copilot", "status": "completed",
             "head_sha": COMMIT},                       # already done → skip
            {"id": 3, "name": "CI", "status": "in_progress",
             "head_sha": COMMIT},                       # not copilot → skip
            {"id": 4, "name": "Running Copilot Code Review",
             "status": "queued", "head_sha": "someone-elses-sha"},  # not ours
        ]
        gh = self._gh(runs)
        job, *_ = run_job(make_job(), gh)
        self.assertEqual(self._cancelled(gh), ["1"])
        self.assertEqual(job.notes["cancelled_copilot_runs"],
                         [{"id": 1, "name": "Copilot"}])

    def _cancelled(self, gh):
        return gh.cancelled

    def test_no_produced_commits_means_no_run_queries(self):
        gh = self._gh([])
        agent = FakeAgent(agent_result(checks=[{"command": "make test",
                                                "exit": 1, "passed": False,
                                                "output_tail": "fail"}]))
        job, *_ = run_job(make_job(), gh, agent=agent)
        self.assertEqual(self._cancelled(gh), [])   # failed before push
