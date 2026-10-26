"""Unit tests for the worker daemon loop. Network-free: gh, queue, and
executor are constructor-injected fakes implementing interfaces.Protocols."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from ghagent import worker as worker_mod
from ghagent.interfaces import (
    COMMENT_MARKER,
    Config,
    Job,
    JobKind,
    JobState,
    ModelConfig,
    RepoConfig,
)
from ghagent.worker import (
    GitHubAuthUnavailable,
    ProviderAuthMissing,
    TERMINAL_STATES,
    Worker,
    WorkerAlreadyRunning,
)


class FakeGh:
    def __init__(self, comments=None, prs=None, auth_ok=True, post_fail=False):
        self.comments = comments or []
        self.prs = prs or {}
        self.auth_ok = auth_ok
        self.post_fail = post_fail
        self.rest_calls = []
        self.posted = []
        self.pr_view_calls = []

    def rest(self, method, path, body=None, paginate=False):
        self.rest_calls.append((method, path, body, paginate))
        if method == "GET" and path.endswith("/issues/comments"):
            return list(self.comments)
        if method == "GET" and "/pulls/" in path and path.endswith("/comments"):
            return list(getattr(self, "review_comments", []))
        if method == "POST" and path.endswith("/comments"):
            if self.post_fail:
                raise RuntimeError("remote write refused")
            self.posted.append((path, body))
            return {"id": 9999}
        raise AssertionError(f"unexpected rest call: {method} {path}")

    def graphql(self, query, variables=None, paginate_path=None):
        raise AssertionError("worker loop must not use graphql")

    def pr_view(self, repo, number):
        self.pr_view_calls.append((repo, number))
        if (repo, number) in self.prs:
            return self.prs[(repo, number)]
        raise RuntimeError(f"not a pull request: {repo}#{number}")

    def issue_view(self, repo, number):
        return {"number": number}

    def pr_checks(self, repo, number):
        return []

    def run_view(self, repo, run_id):
        return {}

    def current_login(self):
        if not self.auth_ok:
            raise RuntimeError("gh: not logged in to github.com")
        return "tester"

    def current_user_id(self):
        if not self.auth_ok:
            raise RuntimeError("gh: not logged in to github.com")
        return 42


class FakeQueue:
    def __init__(self):
        self._jobs = []
        self.stopped = []
        self.reconciled = False

    def add(self, job):
        if any(j.dedup_key == job.dedup_key for j in self._jobs):
            return False
        self._jobs.append(job)
        return True

    def get(self, job_id):
        return next((j for j in self._jobs if j.job_id == job_id), None)

    def update(self, job):
        for i, j in enumerate(self._jobs):
            if j.job_id == job.job_id:
                self._jobs[i] = job
                return

    def find_open_by_repo_number(self, repo, number):
        for j in reversed(self._jobs):
            if j.repo_full_name == repo and j.number == number \
                    and j.state not in TERMINAL_STATES:
                return j
        return None

    def all(self):
        return list(self._jobs)

    def stop_job(self, job_id):
        job = self.get(job_id)
        if job is not None and job.state not in TERMINAL_STATES:
            job.state = JobState.STOPPED
            self.stopped.append(job_id)
            return True
        return False

    def reconcile_with_remote(self, gh):
        self.reconciled = True
        return []


class FakeExecutor:
    def __init__(self):
        self.executed = []
        self.fail = False
        self.status_texts = 0

    def status_text(self, job=None):
        self.status_texts += 1
        return "queue: 0 open"

    def execute(self, job):
        if self.fail:
            raise RuntimeError("boom")
        job.state = JobState.DONE
        job.updated_at = worker_mod._now_iso()
        self.executed.append(job.job_id)


def make_cfg(tmp, **overrides):
    values = dict(
        state_dir=str(Path(tmp) / "state"),
        worktree_root=str(Path(tmp) / "wt"),
        allowed_command_users=[7],
        repos=[RepoConfig(owner="o", name="r")],
        poll_interval_seconds=5,
    )
    values.update(overrides)
    return Config(**values)


def make_worker(tmp, gh=None, queue=None, executor=None, cfg=None, env=None,
                stop_event=None):
    return Worker(
        gh or FakeGh(),
        queue or FakeQueue(),
        executor or FakeExecutor(),
        cfg or make_cfg(tmp),
        env=env if env is not None else {"MCP_SECRET_BROKER_ADMIN_TOKEN": "t"},
        stop_event=stop_event,
    )


def comment(cid, body, uid=7, login="alice", ts="2026-01-02T03:04:05Z",
            number=5):
    return {
        "id": cid,
        "body": body,
        "user": {"id": uid, "login": login},
        "created_at": ts,
        "updated_at": ts,
        "issue_url": f"https://api.github.com/repos/o/r/issues/{number}",
    }


class WorkerLoopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make(self, **kwargs):
        w = make_worker(self.tmp, **kwargs)
        self.addCleanup(w.close)  # release the flock promptly, no warnings
        return w

    def seed(self, w, iso="2026-01-01T00:00:00Z"):
        w.watermarks["o/r"] = iso

    # -- poll_once ----------------------------------------------------------

    def test_new_comment_enqueues_fix_job(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(11, "/agent fix")]
        w.gh.prs[("o/r", 5)] = {"headRefOid": "abc123"}
        self.assertEqual(w.poll_once(), 1)
        jobs = w.queue.all()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.kind, JobKind.FIX_PR)
        self.assertEqual(job.repo_full_name, "o/r")
        self.assertEqual(job.number, 5)
        self.assertEqual(job.head_sha, "abc123")
        self.assertEqual(job.dedup_key, "fix-pr:o/r:5:abc123")
        self.assertEqual(job.command_comment_id, 11)
        self.assertEqual(job.requested_by, 7)
        self.assertTrue(w.queue.reconciled)  # startup reconciled with remote

    def test_own_comment_ignored(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [
            # The worker's own output carries the hidden marker regardless of
            # which account posted it (single-account setups: the owner and
            # the worker share an identity, and commands must still work).
            comment(12, f"{COMMENT_MARKER}job:x--> text", uid=7),
        ]
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(w.queue.all(), [])

    def test_marker_free_command_from_worker_identity_is_a_command(self):
        w = self.make()
        # Single-account setup: the daemon's own identity (tester/42) is also
        # the authorized commander. A marker-free command must enqueue.
        w.cfg.allowed_command_users = [42]
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(14, "/agent status", uid=42, login="tester")]
        self.assertEqual(w.poll_once(), 1)
        self.assertEqual(w.queue.all()[0].kind, JobKind.STATUS)

    def test_unauthorized_author_ignored(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(14, "/agent fix", uid=99)]
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(w.queue.all(), [])

    def test_plain_comment_ignored(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(15, "this bug also breaks the /agentless path")]
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(w.queue.all(), [])

    def test_fix_on_issue_is_skipped(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(16, "/agent fix", number=6)]  # no PR entry
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(w.queue.all(), [])

    def test_status_fast_path(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(17, "/agent status")]
        self.assertEqual(w.poll_once(), 1)
        jobs = w.queue.all()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.kind, JobKind.STATUS)
        self.assertEqual(job.state, JobState.DONE)
        self.assertEqual(job.notes.get("status_posted"), "remote")
        self.assertEqual(w.executor.status_texts, 1)
        self.assertEqual(len(w.gh.posted), 1)
        path, body = w.gh.posted[0]
        self.assertIn("/issues/5/comments", path)
        self.assertIn(COMMENT_MARKER, body["body"])
        self.assertIn("queue: 0 open", body["body"])

    def test_status_post_falls_back_to_local_file(self):
        w = self.make()
        w.gh.post_fail = True
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(18, "/agent status")]
        self.assertEqual(w.poll_once(), 1)
        job = w.queue.all()[0]
        self.assertEqual(job.state, JobState.DONE)
        posted = job.notes.get("status_posted", "")
        self.assertTrue(posted.startswith("local:"))
        fallback = Path(str(posted).split("local:", 1)[1])
        self.assertTrue(fallback.exists())
        self.assertIn(COMMENT_MARKER, fallback.read_text())

    def test_stop_fast_path(self):
        w = self.make()
        w.startup()
        self.seed(w)
        target = Job(job_id="job_target", kind=JobKind.FIX_PR,
                     repo_full_name="o/r", number=5, command_comment_id=1,
                     requested_by=7, dedup_key="fix-pr:o/r:5:zzz",
                     state=JobState.WORKING)
        w.queue.add(target)
        w.gh.comments = [comment(19, "/agent stop")]
        self.assertEqual(w.poll_once(), 1)
        self.assertEqual(w.queue.stopped, ["job_target"])
        stop_jobs = [j for j in w.queue.all() if j.kind is JobKind.STOP]
        self.assertEqual(len(stop_jobs), 1)
        self.assertEqual(stop_jobs[0].state, JobState.DONE)
        self.assertEqual(stop_jobs[0].notes.get("stopped_job"), "job_target")

    def test_watermark_advances_and_since_is_sent(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(20, "/agent status")]
        w.poll_once()
        self.assertEqual(w.watermarks["o/r"], "2026-01-02T03:04:05Z")
        saved = json.loads(
            (Path(w.state_dir) / "watermarks.json").read_text())
        self.assertEqual(saved, {"o/r": "2026-01-02T03:04:05Z"})
        w.gh.comments = [comment(21, "hello", ts="2026-01-03T00:00:00Z")]
        w.poll_once()
        last_get = [c for c in w.gh.rest_calls if c[0] == "GET"][-1]
        self.assertEqual(last_get[2]["since"], "2026-01-02T03:04:05Z")

    def test_duplicate_dedup_on_reprocess(self):
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(22, "/agent fix")]
        w.gh.prs[("o/r", 5)] = {"headRefOid": "abc123"}
        self.assertEqual(w.poll_once(), 1)
        # Simulate the watermark being lost: the same comments come again.
        w.watermarks["o/r"] = "2026-01-01T00:00:00Z"
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(len(w.queue.all()), 1)

    def test_baseline_first_poll_records_position_only(self):
        w = self.make()
        w.startup()
        w.gh.comments = [comment(23, "/agent fix")]  # no stored watermark
        w.gh.prs[("o/r", 5)] = {"headRefOid": "abc123"}
        self.assertEqual(w.poll_once(), 0)  # history is not replayed
        self.assertEqual(w.queue.all(), [])
        self.assertEqual(w.watermarks["o/r"], "2026-01-02T03:04:05Z")
        w.gh.comments = [comment(24, "/agent fix", ts="2026-01-04T00:00:00Z")]
        self.assertEqual(w.poll_once(), 1)  # genuinely new comment -> job

    def test_sibling_parse_command_preferred(self):
        fake = types.ModuleType("ghagent.commands")
        seen = {}

        def parse_command(body, target_kind, target_number, *,
                          command_prefix="/agent"):
            seen.update(body=body, target_kind=target_kind,
                        target_number=target_number, prefix=command_prefix)
            return types.SimpleNamespace(kind=JobKind.STATUS, number=77,
                                         repo_full_name=None, raw=body)

        fake.parse_command = parse_command
        sys.modules["ghagent.commands"] = fake
        self.addCleanup(sys.modules.pop, "ghagent.commands", None)
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(25, "/agent status")]
        self.assertEqual(w.poll_once(), 1)
        self.assertEqual(seen["prefix"], "/agent")
        self.assertEqual(seen["target_kind"], "issue")
        self.assertEqual(seen["target_number"], 5)
        job = w.queue.all()[0]
        self.assertEqual(job.kind, JobKind.STATUS)
        self.assertEqual(job.number, 77)

    def test_sibling_bad_shape_is_loud_not_fatal(self):
        fake = types.ModuleType("ghagent.commands")

        def bad_parse(body, target_kind, target_number, *, command_prefix="/agent"):
            return object()

        fake.parse_command = bad_parse
        sys.modules["ghagent.commands"] = fake
        self.addCleanup(sys.modules.pop, "ghagent.commands", None)
        w = self.make()
        w.startup()
        self.seed(w)
        w.gh.comments = [comment(26, "/agent fix")]
        self.assertEqual(w.poll_once(), 0)  # logged, skipped, no crash
        self.assertEqual(w.queue.all(), [])

    # -- run_forever ----------------------------------------------------------

    def test_run_forever_two_cycles_then_stop(self):
        stop = threading.Event()

        class FakeClock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds):
            clock.t += seconds
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 10:  # 2 cycles x 5 chunks of 1s
                stop.set()

        w = self.make(stop_event=stop)
        w.clock = clock
        w.sleeper = sleeper
        w.startup()
        target = Job(job_id="job_drain", kind=JobKind.IMPLEMENT_ISSUE,
                     repo_full_name="o/r", number=5, command_comment_id=2,
                     requested_by=7, dedup_key="implement-issue:o/r:5:rev",
                     state=JobState.RECEIVED,
                     created_at="2026-01-01T00:00:00+00:00")
        w.queue.add(target)
        w.run_forever()
        gets = [c for c in w.gh.rest_calls if c[0] == "GET"]
        self.assertEqual(len(gets), 2)          # exactly two poll cycles
        self.assertEqual(w.executor.executed, ["job_drain"])
        self.assertEqual(len(sleep_calls), 10)
        health = json.loads((Path(w.state_dir) / "health.json").read_text())
        self.assertEqual(health["state"], "stopped")  # final write on exit

    # -- startup guards -------------------------------------------------------

    def test_single_instance_lock(self):
        w1 = self.make()
        w1.startup()
        w2 = self.make()
        with self.assertRaises(WorkerAlreadyRunning):
            w2.startup()

    def test_no_github_auth_refuses_start(self):
        w = self.make(gh=FakeGh(auth_ok=False))
        with self.assertRaises(GitHubAuthUnavailable) as ctx:
            w.startup()
        self.assertIn("GitHub authentication unavailable", str(ctx.exception))
        self.assertIn("gh auth login", str(ctx.exception))

    def test_missing_provider_material_refuses_start(self):
        w = self.make(env={})
        with self.assertRaises(ProviderAuthMissing) as ctx:
            w.startup()
        self.assertIn("MCP_SECRET_BROKER_ADMIN_TOKEN", str(ctx.exception))

    # -- health ----------------------------------------------------------------

    def test_health_file_written_atomically(self):
        w = self.make()
        w.startup()
        path = Path(w.state_dir) / "health.json"
        health = json.loads(path.read_text())
        self.assertEqual(health["state"], "running")
        self.assertEqual(health["pid"], os.getpid())
        self.assertIn("jobs_open", health)
        self.assertIn("jobs_done_today", health)
        self.assertIn("model_calls_today", health)
        self.assertIn("last_poll_iso", health)
        leftovers = list(Path(w.state_dir).glob("*.tmp"))
        self.assertEqual(leftovers, [], "atomic replace left temp files behind")

    # -- drain and backoff -------------------------------------------------------

    def test_executor_failure_marks_job_failed(self):
        w = self.make()
        executor = w.executor
        executor.fail = True
        job = Job(job_id="job_bad", kind=JobKind.IMPLEMENT_ISSUE,
                  repo_full_name="o/r", number=5, command_comment_id=3,
                  requested_by=7, dedup_key="implement-issue:o/r:5:x",
                  state=JobState.RECEIVED)
        w.queue.add(job)
        w._drain_jobs()
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("boom", job.error)
        self.assertEqual(job.attempts, 1)

    def test_drain_respects_created_order_and_stop_flag(self):
        w = self.make()
        for i, created in enumerate(["2026-01-02T00:00:00+00:00",
                                     "2026-01-01T00:00:00+00:00"]):
            w.queue.add(Job(job_id=f"job_{i}", kind=JobKind.IMPLEMENT_ISSUE,
                            repo_full_name="o/r", number=i + 1,
                            command_comment_id=i, requested_by=7,
                            dedup_key=f"k:{i}", state=JobState.RECEIVED,
                            created_at=created))
        w.stop_event.set()  # shutdown requested: no new job may start
        self.assertEqual(w._drain_jobs(), 0)
        self.assertEqual(w.executor.executed, [])

    def test_backoff_progresses_capped_and_resets(self):
        w = self.make()
        w.cfg.poll_interval_seconds = 120
        for _ in range(6):
            w._note_error(RuntimeError("same failure"))
        self.assertEqual(w._backoff_seconds, worker_mod.MAX_BACKOFF_SECONDS)
        self.assertEqual(w._backoff_seconds, 900)
        w._note_success()
        self.assertEqual(w._backoff_seconds, 0)

    def test_rate_limited_honors_retry_after(self):
        class RateLimited(Exception):
            retry_after = 30

        w = self.make()
        w._note_error(RateLimited("slow down"))
        self.assertEqual(w._backoff_seconds, 30)

    def test_poll_failure_does_not_crash_loop(self):
        w = self.make()

        def broken_rest(*a, **k):
            raise RuntimeError("network gone")

        w.gh.rest = broken_rest
        w.startup()
        self.assertEqual(w.poll_once(), 0)  # logged, watermarks untouched
        w._note_error(RuntimeError("network gone"))
        self.assertGreater(w._backoff_seconds, 0)


if __name__ == "__main__":
    unittest.main()


class WatchTests(unittest.TestCase):
    """Authorized PRs keep getting new review feedback handled automatically
    (until closed, /agent stop, or the daily auto-job cap)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _worker(self, gh=None, cfg=None):
        w = make_worker(self.tmp, gh=gh, cfg=cfg)
        self.addCleanup(w.close)
        w.startup()
        return w

    def _done_fix_job(self, w, job_id="job_w1", number=5):
        job = Job(job_id=job_id, kind=JobKind.FIX_PR, repo_full_name="o/r",
                  number=number, command_comment_id=1, requested_by=7,
                  head_sha="abc123", dedup_key=f"fix-pr:o/r:{number}:abc123")
        w.queue.add(job)
        w._drain_jobs()          # FakeExecutor marks it DONE
        return job

    def test_done_fix_job_authorizes_pr_watch(self):
        w = self._worker()
        self._done_fix_job(w)
        self.assertIn("5", w.watches.get("o/r", {}))
        stored = json.loads((Path(w.state_dir) / "watched.json").read_text())
        self.assertIn("5", stored["o/r"])

    def test_new_review_comment_enqueues_auto_fix_job(self):
        # This selected review bot has the same explicit dispatch permission
        # as a human /agent commenter; watching alone grants no such power.
        cfg = make_cfg(self.tmp, allowed_command_users=[7, 9])
        gh = FakeGh()
        gh.prs[("o/r", 5)] = {"state": "OPEN", "headRefOid": "head456"}
        gh.review_comments = [
            {"id": 101, "body": "This can race with the retry loop.",
             "created_at": "2026-01-02T03:04:05Z",
             "user": {"id": 9, "login": "copilot-bot"}},
        ]
        w = self._worker(gh=gh, cfg=cfg)
        feedback = gh.review_comments
        gh.review_comments = []
        self._done_fix_job(w)     # authorize PR 5; historical feedback is baselined
        gh.review_comments = feedback  # a genuinely new comment arrives afterward
        enqueued = w.poll_once()
        self.assertEqual(enqueued, 1)
        jobs = [j for j in w.queue.all() if j.dedup_key.startswith("auto-fix-pr:")]
        self.assertEqual(len(jobs), 1)
        auto = jobs[0]
        self.assertEqual((auto.kind, auto.number), (JobKind.FIX_PR, 5))
        self.assertEqual(auto.head_sha, "head456")
        self.assertEqual(auto.notes.get("auto_watch"), True)
        # watermark advanced: a second poll does not duplicate
        self.assertEqual(w.poll_once(), 0)

    def test_worker_marker_comment_does_not_trigger(self):
        gh = FakeGh()
        gh.prs[("o/r", 5)] = {"state": "OPEN", "headRefOid": "head456"}
        gh.review_comments = [
            {"id": 102, "body": f"{COMMENT_MARKER}job:x--> done",
             "created_at": "2026-01-02T03:04:05Z",
             "user": {"id": 42, "login": "tester"}},
        ]
        w = self._worker(gh=gh)
        feedback = gh.review_comments
        gh.review_comments = []
        self._done_fix_job(w)
        gh.review_comments = feedback
        self.assertEqual(w.poll_once(), 0)
        self.assertFalse([j for j in w.queue.all()
                          if j.dedup_key.startswith("auto-fix-pr:")])

    def test_auto_job_cap_per_pr_per_day(self):
        gh = FakeGh()
        gh.prs[("o/r", 5)] = {"state": "OPEN", "headRefOid": "head456"}
        gh.review_comments = [
            {"id": 200, "body": "another finding",
             "created_at": "2026-01-02T03:04:05Z",
             "user": {"id": 9, "login": "copilot-bot"}},
        ]
        w = self._worker(gh=gh)
        feedback = gh.review_comments
        gh.review_comments = []
        self._done_fix_job(w)
        gh.review_comments = feedback
        for i in range(worker_mod.AUTO_JOBS_PER_PR_PER_DAY):
            filler = Job(job_id=f"auto_{i}", kind=JobKind.FIX_PR,
                         repo_full_name="o/r", number=5,
                         command_comment_id=i, requested_by=9,
                         head_sha="x",
                         state=JobState.DONE,
                         dedup_key=f"auto-fix-pr:o/r:5:{i}",
                         created_at=worker_mod._now_iso())
            w.queue.add(filler)
        self.assertEqual(w.poll_once(), 0)   # cap reached; feedback waits

    def test_closed_pr_watch_is_dropped(self):
        gh = FakeGh()
        gh.prs[("o/r", 5)] = {"state": "CLOSED", "headRefOid": "head456"}
        gh.review_comments = [
            {"id": 300, "body": "late comment",
             "created_at": "2026-01-02T03:04:05Z",
             "user": {"id": 9, "login": "copilot-bot"}},
        ]
        w = self._worker(gh=gh)
        self._done_fix_job(w)
        self.assertEqual(w.poll_once(), 0)
        self.assertNotIn("5", w.watches.get("o/r", {}))

    def test_all_scope_discovers_current_and_future_prs_without_replaying_old_comments(self):
        from ghagent.control_store import WatchStore
        class ScopeGh(FakeGh):
            open_prs = [{'number': 5, 'title': 'First PR'}]
            def rest(inner, method, path, body=None, paginate=False):
                if path == 'repos/o/r/pulls':
                    return inner.open_prs
                return super(ScopeGh, inner).rest(method, path, body, paginate)
        gh = ScopeGh(prs={('o/r', 5): {'state': 'OPEN', 'headRefOid': 'abc'},
                         ('o/r', 6): {'state': 'OPEN', 'headRefOid': 'def'}})
        gh.review_comments = [{'id': 100, 'body': 'Historical review', 'user': {'id': 7}}]
        w = self._worker(gh=gh)
        store = WatchStore(w.state_dir / 'watched.json')
        store.put('o/r', '*', {'scope': 'all', 'output_dir': '/home/test/project', 'member': 'test'})
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(store.read()['o/r']['5']['last_review_comment_id'], 100)
        gh.open_prs = [*gh.open_prs, {'number': 6, 'title': 'New PR'}]
        self.assertEqual(w.poll_once(), 0)
        self.assertIn('6', store.read()['o/r'])
        gh.review_comments.append({'id': 101, 'body': 'New review', 'user': {'id': 7}})
        self.assertEqual(w.poll_once(), 2)
        self.assertTrue(all(paginate for method, path, body, paginate in gh.rest_calls
                            if '/pulls/' in path and path.endswith('/comments')))
        self.assertEqual({j.number for j in w.queue.all()}, {5, 6})
        self.assertTrue(all(j.notes['output_dir'] == '/home/test/project' for j in w.queue.all()))
        store.remove('o/r', '*')
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(store.read(), {})

    def test_daily_cap_preserves_feedback_cursor(self):
        from ghagent.control_store import WatchStore
        gh = FakeGh(prs={('o/r', 5): {'state': 'OPEN', 'headRefOid': 'abc'}})
        w = self._worker(gh=gh)
        self._done_fix_job(w)
        for i in range(worker_mod.AUTO_JOBS_PER_PR_PER_DAY):
            w.queue.add(Job(f'cap_{i}', JobKind.FIX_PR, 'o/r', 5, i, 7, state=JobState.DONE,
                            dedup_key=f'auto-fix-pr:o/r:5:{i}', created_at=worker_mod._now_iso()))
        gh.review_comments = [{'id': 100, 'body': 'New review', 'user': {'id': 7}}]
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(WatchStore(w.state_dir / 'watched.json').read()['o/r']['5']['last_review_comment_id'], 0)
