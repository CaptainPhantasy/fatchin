"""Job execution flow: RETRIEVING → WORKING → VERIFYING → RESPONDING → DONE/FAILED.

Sibling worker modules (ghagent.github_client, ghagent.queue,
ghagent.coding_agent, ghagent.worktree) are INJECTED, never imported at
module level. ghagent.worktree is imported lazily at call time when no
worktree_ops is supplied. See interfaces.py for the shared contracts.

Worktree-ops duck type expected by this module (all positional):

    add_worktree(path, base_ref) -> str            # worktree path
    fetch_pr_worktree(repo_full_name, pr_number, head_ref) -> str
    commit_all(worktree_path, message) -> str | None   # sha, None if clean
    is_clean(worktree_path) -> bool
    current_sha(worktree_path) -> str
    remove_worktree(worktree_path) -> None
    prune() -> None

Every state transition is persisted with queue.update() BEFORE and AFTER
remote writes. Remote writes are gated on the triggers.preflight verdict and
a remote-moved re-read; the worker never force-pushes, never closes issues,
never merges, and resolves review threads only with complete evidence.
"""
from __future__ import annotations

from .task_write_guard import GuardedGitHub, ensure_not_stopped

import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote

from . import responder, tasks, triggers
from .interfaces import Config, Job, JobKind, JobState

PUSH_REMOTE = "origin"
MAX_GRAPHQL_PAGES = 50
MAX_THREAD_COMMENT_PAGES = 10
MAX_RUN_VIEWS = 3
MAX_RESOLVE_GATES = 10

_REVIEW_THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          viewerCanResolve
          path
          line
          comments(first: 1) { nodes { id } }
        }
      }
    }
  }
}"""

_THREAD_COMMENTS_QUERY = """query($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id }
      }
    }
  }
}"""

_THREAD_LAST_COMMENT_QUERY = """query($id: ID!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(last: 1) { nodes { createdAt } }
    }
  }
}"""


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _clip(text: object, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short(exc: BaseException, limit: int = 200) -> str:
    return _clip(f"{type(exc).__name__}: {exc}", limit)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _is_not_found(exc: BaseException) -> bool:
    text = str(exc)
    return "404" in text or "Not Found" in text


def _norm_ts(value: object) -> str:
    return str(value or "").replace(" ", "T")


def _run_git(args: list[str], cwd: str,
             timeout: float = 600.0) -> tuple[int, str, str]:
    """Run `git -C <cwd> <args>`; rc 124 marks a timeout (uncertain outcome).

    Tests patch this function; nothing else in this module spawns git.
    """
    argv = ["git", "-C", cwd, *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out, err = exc.stdout or "", exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err


# ---------------------------------------------------------------------------
# pure helpers: review threads, linking, work lists, evidence gating
# ---------------------------------------------------------------------------

def collect_review_threads(gh, repo_full_name: str, number: int,
                           max_pages: int = MAX_GRAPHQL_PAGES) -> list[dict]:
    """Page through reviewThreads (first:100, after:endCursor)."""
    owner, name = repo_full_name.split("/", 1)
    threads: list[dict] = []
    after: str | None = None
    pages = 0
    while pages < max_pages:
        data = gh.graphql(_REVIEW_THREADS_QUERY, {
            "owner": owner, "name": name, "number": int(number),
            "first": 100, "after": after,
        }) or {}
        conn = ((data.get("repository") or {}).get("pullRequest") or {}) \
            .get("reviewThreads") or {}
        threads.extend(n for n in (conn.get("nodes") or []) if isinstance(n, dict))
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
        pages += 1
    return threads


def fetch_thread_comment_ids(gh, thread_id: str,
                             max_pages: int = MAX_THREAD_COMMENT_PAGES) -> list[str]:
    """GraphQL comment ids of one thread via its own paginated connection."""
    ids: list[str] = []
    after: str | None = None
    pages = 0
    while pages < max_pages:
        data = gh.graphql(_THREAD_COMMENTS_QUERY, {
            "id": thread_id, "first": 100, "after": after,
        }) or {}
        conn = ((data.get("node") or {}).get("comments")) or {}
        ids.extend(n.get("id") for n in (conn.get("nodes") or [])
                   if isinstance(n, dict) and n.get("id"))
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
        pages += 1
    return ids


def _root_of(comment: dict, by_id: dict) -> int | None:
    """Walk in_reply_to_id within the REST review comments to the root."""
    current = comment
    seen: set = set()
    while isinstance(current, dict) and current.get("in_reply_to_id"):
        if current.get("id") in seen:
            break
        seen.add(current.get("id"))
        current = by_id.get(current.get("in_reply_to_id"))
    cid = (current or {}).get("id") if isinstance(current, dict) else None
    return int(cid) if isinstance(cid, int) else None


def link_threads(threads: list[dict], review_comments: list[dict]) -> list[dict]:
    """Annotate each thread with the NUMERIC REST id of its root comment.

    GraphQL global comment ids equal REST node_ids. Threads whose first
    comment has no REST match keep _root_comment_id None — callers must fetch
    that thread's own comment connection (see relink_thread), never drop it.
    """
    by_node = {c.get("node_id"): c for c in review_comments or []
               if isinstance(c, dict) and c.get("node_id")}
    by_id = {c.get("id"): c for c in review_comments or []
             if isinstance(c, dict) and c.get("id")}
    linked = []
    for t in threads or []:
        t = dict(t)
        nodes = ((t.get("comments") or {}).get("nodes")) or []
        first = nodes[0] if nodes and isinstance(nodes[0], dict) else {}
        rest = by_node.get(first.get("id"))
        root = _root_of(rest, by_id) if rest is not None else None
        t["_root_comment_id"] = root
        t["_link_source"] = "first-comment" if root is not None else None
        linked.append(t)
    return linked


def relink_thread(thread: dict, review_comments: list[dict], gh) -> bool:
    """Second chance for an unlinked thread: fetch its full comment ids and
    match any of them against the REST review comments."""
    by_node = {c.get("node_id"): c for c in review_comments or []
               if isinstance(c, dict) and c.get("node_id")}
    by_id = {c.get("id"): c for c in review_comments or []
             if isinstance(c, dict) and c.get("id")}
    for gid in fetch_thread_comment_ids(gh, thread["id"]):
        rest = by_node.get(gid)
        if rest is None:
            continue
        root = _root_of(rest, by_id)
        if root is not None:
            thread["_root_comment_id"] = root
            thread["_link_source"] = "thread-comments"
            return True
    return False


def _check_state(check: dict) -> str:
    return str(check.get("conclusion") or check.get("state")
               or check.get("status") or "").upper()


def _failed_states() -> set[str]:
    return {"FAILURE", "FAILED", "TIMED_OUT", "STARTUP_FAILURE"}


def _pending_states() -> set[str]:
    return {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "REQUESTED"}


def collect_run_evidence(gh, repo_full_name: str, checks: list[dict],
                         max_runs: int = MAX_RUN_VIEWS) -> dict:
    """run_view details for failed checks that carry a run id (bounded)."""
    evidence: dict = {}
    for c in checks or []:
        if not isinstance(c, dict) or _check_state(c) not in _failed_states():
            continue
        run_id = c.get("runId")
        if not run_id:
            m = re.search(r"/actions/runs/(\d+)",
                          str(c.get("link") or c.get("detailsUrl") or ""))
            run_id = int(m.group(1)) if m else None
        if not run_id or run_id in evidence:
            continue
        if len(evidence) >= max_runs:
            break
        try:
            info = gh.run_view(repo_full_name, int(run_id))
        except Exception as exc:
            info = {"error": _short(exc)}
        info = info if isinstance(info, dict) else {}
        evidence[str(c.get("name") or run_id)] = {
            "run_id": int(run_id),
            "conclusion": info.get("conclusion"),
            "url": info.get("url") or info.get("html_url"),
        }
    return evidence


def build_work_items_fix(pr: dict, threads: list[dict], checks: list[dict],
                         run_evidence: dict) -> list[dict]:
    """Work list = unresolved threads (outdated kept as context) + check evidence."""
    items: list[dict] = []
    for t in threads or []:
        if t.get("isResolved"):
            continue
        items.append({
            "kind": "review_thread",
            "thread_id": t.get("id"),
            "path": t.get("path"),
            "line": t.get("line"),
            "outdated": bool(t.get("isOutdated")),
            "root_comment_id": t.get("_root_comment_id"),
            "linked": t.get("_root_comment_id") is not None,
        })
    for c in checks or []:
        if not isinstance(c, dict):
            continue
        state = _check_state(c)
        if state in _failed_states() or state in _pending_states():
            items.append({
                "kind": "check" if state in _failed_states() else "check_pending",
                "name": c.get("name"),
                "state": state,
                "evidence": (run_evidence or {}).get(str(c.get("name"))),
            })
    return items


def build_work_items_issue(issue: dict, comments: list[dict]) -> list[dict]:
    items = [{
        "kind": "issue_body",
        "title": (issue or {}).get("title"),
        "text": _clip((issue or {}).get("body"), 2000),
    }]
    for c in (comments or [])[-20:]:
        if isinstance(c, dict):
            items.append({
                "kind": "issue_comment",
                "author": ((c.get("user") or {}) if isinstance(c.get("user"), dict)
                           else {}).get("login"),
                "text": _clip(c.get("body"), 500),
            })
    return items


def build_instruction(job: Job, subject: dict, work_items: list[dict],
                      check_commands: list[str]) -> str:
    title = _clip((subject or {}).get("title"), 200)
    lines = [f"Task: {job.kind.value} for {job.repo_full_name} #{job.number} — {title}"]
    reviews = (subject or {}).get("review_digest")
    if reviews:
        lines.append(f"Review state: {reviews}")
    lines.append("Work items (address every one you can; report what you cannot):")
    for i, item in enumerate(work_items or [], 1):
        kind = item.get("kind")
        if kind == "review_thread":
            note = " (outdated; context only)" if item.get("outdated") else ""
            link = (f" [root comment {item['root_comment_id']}]"
                    if item.get("root_comment_id") else " [no linked root comment]")
            lines.append(f"  {i}. review thread on {item.get('path')}"
                         f" line {item.get('line')}{note}{link}")
        elif kind in ("check", "check_pending"):
            ev = f" run evidence: {_clip(item.get('evidence'), 200)}" if item.get("evidence") else ""
            lines.append(f"  {i}. check {item.get('name')!r} state {item.get('state')}{ev}")
        elif kind == "issue_body":
            lines.append(f"  {i}. issue body: {item.get('text')}")
        else:
            lines.append(f"  {i}. comment by {item.get('author')}: {item.get('text')}")
    lines.append("Constraints: change only what the work items need; never force-push; "
                 "never close, merge, or resolve anything yourself.")
    if check_commands:
        lines.append("Run these checks before finishing and report each result: "
                     + "; ".join(check_commands))
    else:
        lines.append("No repo checks are configured; run whatever local tests exist "
                     "and say so plainly in the summary.")
    return "\n".join(lines)


def evaluate_verification(cfg_checks: list[str],
                          result) -> tuple[bool | None, str]:
    """(True|False|None, note). None means no checks exist — local tests only."""
    checks = list(getattr(result, "checks", None) or [])
    failed = [str(c.get("command") or c.get("name") or "?")
              for c in checks if isinstance(c, dict) and c.get("passed") is not True]
    if checks and not failed:
        return True, f"all {len(checks)} reported checks passed"
    if checks and failed:
        return False, "checks failed: " + ", ".join(_clip(f, 60) for f in failed[:5])
    if cfg_checks:
        return False, "repo checks are configured but the agent reported no check results"
    return None, "no repo checks configured; verification is local-tests-only " \
                 "(never claimed as passing checks)"


def cheap_resolution_gate(thread: dict, checks_pass: bool | None,
                          changed_files: set[str]) -> tuple[bool, str]:
    """Evidence gate, part 1 (pure): resolved state, permission, checks, path."""
    if thread.get("isResolved"):
        return False, "thread is already resolved"
    if not responder.can_resolve(thread):
        return False, "viewer does not have permission to resolve"
    if checks_pass is not True:
        return False, "checks did not pass (or none were reported)"
    if thread.get("path") not in changed_files:
        return False, "the fix does not touch this thread's file"
    return True, "evidence complete"


def new_comment_gate(last_comment_at: object, fix_commit_at: object) -> tuple[bool, str]:
    """Evidence gate, part 2 (pure): no comment newer than the fix commit."""
    if not last_comment_at:
        return True, "no comments on the thread"
    if not fix_commit_at:
        return False, "fix commit timestamp unknown"
    if _norm_ts(last_comment_at) > _norm_ts(fix_commit_at):
        return False, "new comment arrived after the fix commit"
    return True, "no newer comments"


def _thread_reply_text(thread: dict, produced_sha: str,
                       gate_ok: bool, why: str) -> str:
    path = thread.get("path") or "?"
    line = thread.get("line")
    fix_note = (f"Fix pushed as {produced_sha[:8]}." if produced_sha
                else "No commit was produced.")
    if gate_ok:
        tail = ("The fix covers this thread's file and checks pass, so the "
                "thread is being resolved.")
    else:
        tail = f"Leaving this thread unresolved: {why}."
    loc = f"`{path}`" + (f" line {line}" if line else "")
    return f"Worked on {loc}. {fix_note} {tail}"


def format_status(job: Job) -> str:
    """Plain-language saved-state report for /agent status."""
    notes = job.notes or {}
    lines = [f"job {job.job_id}: {job.kind.value} {job.repo_full_name}#{job.number}"
             f" — state {job.state.value}"]
    if notes.get("phase"):
        lines.append(f"phase: {notes['phase']}")
    if job.error:
        lines.append(f"error: {job.error}")
    if notes.get("verdict"):
        lines.append(f"triggers: {notes['verdict']}")
    if notes.get("verification"):
        lines.append(f"verification: {notes['verification']}")
    if notes.get("tested_base"):
        lines.append(f"verification base commit: {notes['tested_base']}")
    if job.produced_commits:
        lines.append("commits: " + ", ".join(job.produced_commits))
    if notes.get("created_pr"):
        created = notes["created_pr"]
        lines.append(f"opened PR: {created.get('url') or created.get('number')}")
    if job.posted_comment_ids:
        lines.append("comments posted: "
                     + ", ".join(str(i) for i in job.posted_comment_ids))
    if job.resolved_thread_ids:
        lines.append("threads resolved: " + ", ".join(job.resolved_thread_ids))
    lines.append(f"model calls used: {job.model_calls_used}")
    if notes.get("agent_summary"):
        lines.append(f"summary: {_clip(notes['agent_summary'], 300)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------

class JobExecutor:
    """Runs one Job through its full state machine and posts the responses."""

    def __init__(self, gh, queue, cfg: Config, coding_agent_factory,
                 worktree_ops=None):
        self.gh = gh
        self.queue = queue
        self.cfg = cfg
        self.coding_agent_factory = coding_agent_factory
        self._worktree_ops = worktree_ops

    # -- injected dependencies ------------------------------------------------

    def _ops(self):
        if self._worktree_ops is not None:
            return self._worktree_ops
        from . import worktree  # lazy: sibling module, built in parallel
        return worktree

    def _repo_cfg(self, job: Job):
        for rc in getattr(self.cfg, "repos", None) or []:
            if rc.full_name == job.repo_full_name:
                return rc
        return None

    def _repo_checks(self, job: Job) -> list[str]:
        rc = self._repo_cfg(job)
        return list(getattr(rc, "checks", None) or []) if rc else []

    def _base_branch(self, job: Job) -> str:
        rc = self._repo_cfg(job)
        return (getattr(rc, "base_branch", None) or "main") if rc else "main"

    def _run_agent(self, job: Job, wt: str, checks: list[str], instruction: str):
        """Bind every task to its saved identity, stop signal, and call total."""
        agent = self.coding_agent_factory(wt, checks)
        if hasattr(agent, 'set_job_context'):
            agent.set_job_context(job.job_id)
        if hasattr(agent, 'set_stop_check'):
            agent.set_stop_check(lambda: self._stopped(job.job_id))
        result = None
        try:
            result = agent.run(instruction)
            return result
        finally:
            # The real runner exposes the durable per-job total, including
            # failed requests and reservations from earlier worker runs.
            # Adding that total again would count resumed calls twice.
            total_calls = getattr(agent, 'total_model_calls_used', None)
            if type(total_calls) is int:
                job.model_calls_used = max(job.model_calls_used, total_calls)
            elif result is not None:
                job.model_calls_used += int(getattr(result, 'model_calls_used', 0) or 0)

    # -- persistence ----------------------------------------------------------

    def _persist(self, job: Job, state: JobState | None = None, error: str = "") -> None:
        if state is not None:
            job.state = state
        if error:
            job.error = _clip(error, 500)
        job.updated_at = _utcnow()
        self.queue.update(job)

    def _stopped(self, job_id: str) -> bool:
        stop_event = getattr(self, 'stop_event', None)
        if stop_event is not None and stop_event.is_set():
            return True
        current = self.queue.get(job_id)
        return current is not None and current.state is JobState.STOPPED

    def _fail(self, job: Job, error: str) -> Job:
        job.error = _clip(error, 500)
        self._persist(job, JobState.FAILED)
        return job

    def _halt_stopped(self, job: Job) -> Job:
        job.notes["phase"] = "stopped-before-completion"
        self._persist(job, JobState.STOPPED)
        return job

    # -- entry point ----------------------------------------------------------

    def execute(self, job: Job) -> Job:
        from .fleet_locks import FleetBusy, repo_lock
        try:
            with repo_lock(self.cfg.state_dir, job.repo_full_name, exclusive=True, blocking=False):
                return self._execute_with_repository_lock(job)
        except FleetBusy:
            # Leave the task queued instead of turning normal contention into
            # a failure. The daemon picks it up after the fleet task finishes.
            job.notes['phase'] = 'waiting-for-repository'
            self._persist(job)
            return job

    def _execute_with_repository_lock(self, job: Job) -> Job:
        try:
            return self._execute(job)
        except Exception as exc:
            job.error = f"executor error: {_short(exc)}"
            try:
                self._persist(job, JobState.FAILED)
            except Exception:
                pass
            return job
        finally:
            wt = job.notes.get("worktree_path")
            if wt and job.state is JobState.WAITING_ON_YOU:
                # Drafts still need their checkout to Post from: export the
                # patch now, keep the scratch until the person decides.
                try:
                    self._export_worktree(job, wt)
                except Exception as exc:
                    job.notes["result_export_error"] = _short(exc)
            elif wt:
                self._cleanup_worktree(job, wt)
            try:
                from .results import write_report
                write_report(job)
                self._persist(job)
            except Exception as exc:
                job.notes["result_export_error"] = _short(exc)
                self._persist(job)

    def _execute(self, job: Job) -> Job:
        if job.state is JobState.STOPPED:
            return job
        if job.state is JobState.WAITING_ON_YOU:
            return job  # drafts resume only through an explicit user action
        task = tasks.get(job.kind.value)
        if task is not None and task.profile == "read-only":
            return self._execute_report(job, task)
        if task is not None and task.profile in {"draft", "write", "high-stakes"}:
            return self._execute_drafts(job, task)
        if job.kind not in (JobKind.FIX_PR, JobKind.IMPLEMENT_ISSUE):
            if task is not None:
                return self._fail(
                    job, f"“{task.label}” is registered, but its execution "
                         "arrives with a later build phase. Nothing was changed; "
                         "watch the task list for that task type to appear.")
            return self._fail(job, f"unsupported job kind: {job.kind}")

        job.notes["phase"] = "retrieving"
        self._persist(job, JobState.RETRIEVING)
        if job.kind is JobKind.FIX_PR:
            context = self._retrieve_fix(job)
        else:
            context = self._retrieve_issue(job)
        if context is None:
            return job  # already FAILED with the reason recorded
        self._persist(job)
        if self._stopped(job.job_id):
            return self._halt_stopped(job)

        job.notes["phase"] = "working"
        self._persist(job, JobState.WORKING)
        working = self._working(job, context)
        if working is None:
            return job
        result, wt = working
        job.notes["worktree_path"] = wt  # scratch path; cleaned up on exit
        self._persist(job)
        if self._stopped(job.job_id):
            return self._halt_stopped(job)

        job.notes["phase"] = "verifying"
        self._persist(job, JobState.VERIFYING)
        verified = self._verifying(job, result, wt)
        if verified is None:
            self._cleanup_worktree(job, wt)
            return job
        checks_pass, verification_note, tested = verified
        self._persist(job)
        if self._stopped(job.job_id):
            self._cleanup_worktree(job, wt)
            return self._halt_stopped(job)

        job.notes["phase"] = "responding"
        self._persist(job, JobState.RESPONDING)
        done = self._responding(job, context, result, wt, checks_pass,
                                verification_note, tested)
        # Scratch cleanup: keep the worktree only when the fix landed
        # (produced commits reference it); otherwise remove it.
        if not done.produced_commits or done.state is not JobState.DONE:
            self._cleanup_worktree(job, wt)
        return done

    def _execute_report(self, job: Job, task) -> Job:
        """Read-only task path: inspect, produce a report, write nothing remote.

        Uses the same worktree isolation and agent loop as coding tasks, but no
        checks gate, no push, no comments. The report lands in the member's
        result folder via the existing export in execute()'s finally.
        """
        repo_cfg = self._repo_cfg(job)
        base = self._base_branch(job) if repo_cfg else "main"
        job.notes["phase"] = "retrieving"
        self._persist(job, JobState.RETRIEVING)
        ops = self._ops()
        path = os.path.join(self.cfg.worktree_root,
                            _safe_name(f"{task.id}-{job.job_id}"))
        if task.takes == "none":
            # Creating something new: there is no repository to check out.
            try:
                os.makedirs(path, exist_ok=True)
                _run_git(["init", "-q", "-b", base], cwd=path)
                _run_git(["-c", "user.name=gha-worker",
                          "-c", "user.email=gha-worker@inference",
                          "commit", "--allow-empty", "-q", "-m", "start"], cwd=path)
                wt = path
            except Exception as exc:
                return self._fail(job, f"scratch folder setup failed: {_short(exc)}")
        else:
            try:
                wt = ops.add_worktree(path, base, job.repo_full_name)
            except Exception as exc:
                return self._fail(job, f"worktree setup failed: {_short(exc)}")
        job.notes["worktree_path"] = wt
        self._persist(job)
        if self._stopped(job.job_id):
            return self._halt_stopped(job)

        job.notes["phase"] = "working"
        self._persist(job, JobState.WORKING)
        context = {"repo": job.repo_full_name, "worktree": wt,
                   "base_branch": base, "values": job.notes.get("values") or {},
                   "title": job.notes.get("title", "")}
        instruction = (task.build_instruction(context) if callable(task.build_instruction)
                       else self._default_report_instruction(task, job, context))
        job.notes["instruction"] = _clip(instruction, 4000)
        try:
            result = self._run_agent(job, wt, [], instruction)
        except Exception as exc:
            return self._fail(job, f"coding agent failed: {_short(exc)}")
        job.notes["agent_summary"] = _clip(getattr(result, "summary", ""), 4000)
        job.notes["changed_files"] = list(getattr(result, "changed_files", None) or [])
        error = str(getattr(result, "error", "") or "")
        if self._stopped(job.job_id):
            return self._halt_stopped(job)
        if error:
            return self._fail(job, f"coding agent reported error: {_clip(error, 300)}")
        job.notes["phase"] = "reporting"
        job.notes["verification"] = "Not applicable: this task reads and reports; it changes nothing on GitHub."
        self._persist(job, JobState.DONE)
        return job

    DRAFT_DIR = ".gha-drafts"

    def _execute_drafts(self, job: Job, task) -> Job:
        """Draft-producing task path: the agent writes candidate texts into
        .gha-drafts/ in the worktree; the job stops at WAITING_ON_YOU. Nothing
        is posted or pushed until the person chooses Post."""
        repo_cfg = self._repo_cfg(job)
        base = self._base_branch(job) if repo_cfg else "main"
        job.notes["phase"] = "retrieving"
        self._persist(job, JobState.RETRIEVING)
        ops = self._ops()
        path = os.path.join(self.cfg.worktree_root,
                            _safe_name(f"{task.id}-{job.job_id}"))
        if task.takes == "none":
            # Creating something new: there is no repository to check out.
            try:
                os.makedirs(path, exist_ok=True)
                _run_git(["init", "-q", "-b", base], cwd=path)
                _run_git(["-c", "user.name=gha-worker",
                          "-c", "user.email=gha-worker@inference",
                          "commit", "--allow-empty", "-q", "-m", "start"], cwd=path)
                wt = path
            except Exception as exc:
                return self._fail(job, f"scratch folder setup failed: {_short(exc)}")
        else:
            try:
                wt = ops.add_worktree(path, base, job.repo_full_name)
            except Exception as exc:
                return self._fail(job, f"worktree setup failed: {_short(exc)}")
        job.notes["worktree_path"] = wt
        self._persist(job)
        if self._stopped(job.job_id):
            return self._halt_stopped(job)

        job.notes["phase"] = "working"
        self._persist(job, JobState.WORKING)
        context = {"repo": job.repo_full_name, "worktree": wt,
                   "base_branch": base, "values": job.notes.get("values") or {},
                   "title": job.notes.get("title", ""),
                   "number": job.number, "job_id": job.job_id}
        instruction = (task.build_instruction(context) if callable(task.build_instruction)
                       else self._default_report_instruction(task, job, context))
        instruction += ("\n\nWrite every candidate text you want the person to "
                        f"review as its own Markdown file inside {self.DRAFT_DIR}/ "
                        "in the worktree: first line is the title, the rest is "
                        "the exact words that would be posted. Do not post, push, "
                        "or comment anything.")
        job.notes["instruction"] = _clip(instruction, 4000)
        try:
            result = self._run_agent(job, wt, [], instruction)
        except Exception as exc:
            return self._fail(job, f"coding agent failed: {_short(exc)}")
        job.notes["agent_summary"] = _clip(getattr(result, "summary", ""), 4000)
        job.notes["changed_files"] = list(getattr(result, "changed_files", None) or [])
        if self._stopped(job.job_id):
            return self._halt_stopped(job)
        error = str(getattr(result, "error", "") or "")
        if error:
            return self._fail(job, f"coding agent reported error: {_clip(error, 300)}")
        drafts = self._collect_drafts(Path(wt))
        job.notes["drafts"] = drafts
        if not drafts:
            return self._fail(job, "The drafting run produced no candidate texts. "
                                   "Nothing was changed; retry with clearer words.")
        try:
            from .control_store import AuthorityStore
            auto = AuthorityStore(Path(self.cfg.state_dir) / 'authority.json'
                                  ).mode(job.repo_full_name)
        except Exception:
            auto = 'ask'  # unreadable authority state never auto-posts
        if job.notes.get('learning_session_id'):
            # A guided task always stops at its draft, even in repositories
            # whose ordinary tasks have permission to post automatically.
            auto = 'ask'
        if auto == 'auto' and callable(task.apply) and not task.post_confirm_required:
            posted = []
            try:
                for draft in drafts:
                    if self._stopped(job.job_id):
                        return self._halt_stopped(job)
                    job.notes['posting'] = draft['name']
                    self._persist(job)
                    if self._stopped(job.job_id):
                        job.notes.pop('posting', None)
                        return self._halt_stopped(job)
                    context = {'repo': job.repo_full_name,
                                         'number': job.number,
                                         'draft': dict(draft),
                                         'values': job.notes.get('values') or {},
                                         'worktree': wt,
                                         'config': self.cfg,
                                         'stop_check': lambda: self._stopped(job.job_id)
                                         }
                    ensure_not_stopped(context)
                    result = task.apply(context, GuardedGitHub(self.gh, context), job) or {}
                    posted.append({'name': draft['name'], 'at': _utcnow(),
                                   'result': str(result.get('result', ''))[:500],
                                   'auto': True})
                    job.notes['posted'] = list(posted)
                    job.notes.pop('posting', None)
                    self._persist(job)
            except Exception as exc:
                job.notes['posted'] = posted
                # An apply failure can follow an external mutation. Keep its
                # claim until that outcome is reconciled; only the trusted
                # guard can prove a refusal happened before any write.
                try:
                    from .task_write_guard import PreWriteRefusal
                except ImportError:
                    pass  # Missing guard support must not release a claim.
                else:
                    if isinstance(exc, PreWriteRefusal):
                        job.notes.pop('posting', None)
                job.notes['verification'] = ('Automatic posting stopped part-way: '
                                             + _short(exc))
                job.notes['phase'] = 'waiting-on-you'
                self._persist(job, JobState.WAITING_ON_YOU)
                return job
            job.notes['posted'] = posted
            job.notes.pop('posting', None)
            job.notes['verification'] = ('Posted automatically under this '
                                         'repository’s “post directly” setting.')
            job.notes['phase'] = 'posted'
            self._persist(job, JobState.DONE)
            return job
        job.notes["phase"] = "waiting-on-you"
        job.notes["verification"] = ("Not applicable yet: nothing reaches GitHub "
                                     "until you choose Post.")
        self._persist(job, JobState.WAITING_ON_YOU)
        return job

    @classmethod
    def _collect_drafts(cls, worktree: Path) -> list[dict]:
        folder = worktree / cls.DRAFT_DIR
        if folder.is_symlink() or not folder.is_dir():
            return []
        drafts = []
        for path in sorted(folder.glob("*.md"))[:10]:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")[:20000]
            except OSError:
                continue
            first, _, rest = text.partition("\n")
            drafts.append({"name": path.stem,
                           "title": _clip(first.strip() or path.stem, 200),
                           "body": rest.strip() or text.strip()})
        return drafts

    def _default_report_instruction(self, task, job: Job, context: dict) -> str:
        values = context.get("values") or {}
        detail = "\n".join(f"- {k}: {v}" for k, v in values.items() if v)
        return (
            f"Task: {task.label} for {job.repo_full_name}.\n"
            f"{task.help}\n"
            + (f"Your inputs:\n{detail}\n" if detail else "")
            + "Work only inside this checkout. Investigate thoroughly and answer "
              "in plain language a first-time GitHub user can follow. Cite files "
              "and line numbers for every claim. Do not change repository files "
              "unless the task itself asks for it; this task makes no changes on "
              "GitHub. Finish with a complete answer in the summary field."
        )

    def _cleanup_worktree(self, job: Job, wt: str) -> None:
        if not self._export_worktree(job, wt):
            job.notes['worktree_cleanup'] = 'Scratch retained because result export failed. Correct the destination access before recovery.'
            return
        try:
            self._ops().remove_worktree(wt)
            self._ops().prune()
            job.notes.pop("worktree_path", None)
        except Exception:
            job.notes["worktree_cleanup"] = f"failed for {wt}"

    def _export_worktree(self, job: Job, wt: str) -> bool:
        if not job.notes.get("output_dir"):
            return True
        try:
            from .results import export_worktree
            export_worktree(job, wt)
            job.notes.pop('result_export_error', None)
            return True
        except Exception as exc:
            job.notes["result_export_error"] = _short(exc)
            return False

    # -- RETRIEVING -----------------------------------------------------------

    def _retrieve_fix(self, job: Job) -> dict | None:
        repo, number = job.repo_full_name, job.number
        pr = self.gh.pr_view(repo, number)
        if not isinstance(pr, dict) or not pr:
            self._fail(job, f"PR #{number} could not be read")
            return None
        if not job.head_sha:
            job.head_sha = str(pr.get("headRefOid") or "")
        job.started_commit = job.head_sha
        issue_comments = self.gh.rest(
            "GET", f"repos/{repo}/issues/{number}/comments", paginate=True) or []
        reviews = self.gh.rest(
            "GET", f"repos/{repo}/pulls/{number}/reviews", paginate=True) or []
        review_comments = self.gh.rest(
            "GET", f"repos/{repo}/pulls/{number}/comments", paginate=True) or []
        threads = collect_review_threads(self.gh, repo, number)
        threads = link_threads(threads, review_comments)
        unlinked = 0
        for t in threads:
            if t.get("_root_comment_id") is None:
                if relink_thread(t, review_comments, self.gh):
                    continue
                t["_link_note"] = ("root comment id unknown; the thread will be "
                                   "summarized on the PR instead of answered inline")
                unlinked += 1
        checks = self.gh.pr_checks(repo, number) or []
        run_evidence = collect_run_evidence(self.gh, repo, checks)
        work_items = build_work_items_fix(pr, threads, checks, run_evidence)
        pr = dict(pr)
        pr["review_digest"] = _clip(", ".join(
            f"{(r.get('user') or {}).get('login')}: {r.get('state')}"
            for r in reviews[:5] if isinstance(r, dict)), 200)
        job.notes.update({
            "pr_head_ref": pr.get("headRefName"),
            "pr_state": pr.get("state"),
            "threads_total": len(threads),
            "threads_unresolved": sum(1 for t in threads if not t.get("isResolved")),
            "threads_without_root_id": unlinked,
            "failed_checks": sum(1 for c in checks if _check_state(c) in _failed_states()),
            "review_count": len(reviews),
        })
        return {"pr": pr, "threads": threads, "checks": checks,
                "run_evidence": run_evidence, "work_items": work_items,
                "issue_comments": issue_comments}

    def _retrieve_issue(self, job: Job) -> dict | None:
        repo, number = job.repo_full_name, job.number
        issue = self.gh.issue_view(repo, number)
        if _looks_like_pr(issue):
            self._fail(job, f"#{number} is a pull request, not an issue; use /agent fix")
            return None
        comments = self.gh.rest(
            "GET", f"repos/{repo}/issues/{number}/comments", paginate=True) or []
        job.notes["title"] = _clip((issue or {}).get("title"), 200)
        job.notes["issue_state"] = (issue or {}).get("state")
        job.notes["issue_comments_count"] = len(comments)
        work_items = build_work_items_issue(issue, comments)
        return {"issue": issue, "comments": comments, "work_items": work_items}

    # -- WORKING --------------------------------------------------------------

    def _working(self, job: Job, context: dict):
        ops = self._ops()
        try:
            if job.kind is JobKind.FIX_PR:
                head_ref = (context.get("pr") or {}).get("headRefName") or ""
                wt = ops.fetch_pr_worktree(job.repo_full_name, job.number, head_ref)
            else:
                path = os.path.join(self.cfg.worktree_root,
                                    _safe_name(f"issue-{job.number}-{job.job_id}"))
                wt = ops.add_worktree(path, self._base_branch(job),
                                      getattr(job, "repo_full_name", None))
        except Exception as exc:
            self._fail(job, f"worktree setup failed: {_short(exc)}")
            return None
        instruction = build_instruction(
            job, context.get("pr") or context.get("issue"),
            context["work_items"], self._repo_checks(job))
        job.notes["instruction"] = _clip(instruction, 4000)
        job.notes["worktree_path"] = wt
        if not job.started_commit:
            job.started_commit = str(ops.current_sha(wt) or '')
        try:
            result = self._run_agent(job, wt, self._repo_checks(job), instruction)
        except Exception as exc:
            self._fail(job, f"coding agent failed: {_short(exc)}")
            return None
        job.notes["agent_summary"] = _clip(getattr(result, "summary", ""), 2000)
        job.notes["changed_files"] = list(getattr(result, "changed_files", None) or [])
        job.notes["checks"] = list(getattr(result, "checks", None) or [])
        if self._stopped(job.job_id):
            self._halt_stopped(job)
            return None
        error = str(getattr(result, "error", "") or "")
        if error:
            self._fail(job, f"coding agent reported error: {_clip(error, 300)}")
            return None
        return result, wt

    # -- VERIFYING ------------------------------------------------------------

    def _verifying(self, job: Job, result, wt: str):
        checks_pass, note = evaluate_verification(self._repo_checks(job), result)
        tested = ""
        try:
            tested = str(self._ops().current_sha(wt) or "")
        except Exception:
            tested = ""
        if not tested:
            tested = str(getattr(result, "final_commit_sha", "") or "")
        job.notes["tested_base"] = tested
        job.notes["verification"] = note
        if checks_pass is False:
            self._fail(job, f"verification failed: {note}")
            return None
        return checks_pass, note, tested

    # -- RESPONDING -----------------------------------------------------------

    def _responding(self, job: Job, context: dict, result, wt: str,
                    checks_pass: bool | None, verification_note: str,
                    tested: str) -> Job:
        repo = job.repo_full_name
        pr = context.get("pr") or {}
        is_fix = job.kind is JobKind.FIX_PR

        # (a) no-GitHub-charge guard: verdict NEEDS_REVIEW holds ALL writes.
        report = triggers.preflight(
            self.gh, repo,
            head_ref=pr.get("headRefName") if is_fix else None,
            allow_workflows=bool(job.notes.get("allow_workflows", False)),
            allow_unknown_rulesets=bool(getattr(
                self._repo_cfg(job), "allow_unknown_rulesets", False)))
        job.notes["trigger_verdict"] = {"verdict": report.verdict,
                                        "reasons": list(report.reasons)}
        job.notes["verdict"] = triggers.verdict_line(report)
        self._persist(job)
        if report.verdict != triggers.SAFE:
            first = report.reasons[0] if report.reasons else "unverified areas remain"
            return self._fail(job, f"trigger preflight held all writes: {first}")
        if self._stopped(job.job_id):
            return self._halt_stopped(job)

        # (b) re-read the remote: another writer may have moved it.
        if is_fix:
            pr_now = self.gh.pr_view(repo, job.number) or {}
            state = str(pr_now.get("state") or "").upper()
            if state in {"CLOSED", "MERGED"}:
                return self._fail(job, f"PR #{job.number} is {state}; refusing to push")
            if not job.head_sha:
                return self._fail(job, "starting head sha unknown; refusing to push")
            remote_head = str(pr_now.get("headRefOid") or "")
            if remote_head != job.head_sha:
                job.notes["remote_head_sha"] = remote_head
                return self._fail(job, "remote moved; re-run to rebase")
        else:
            branch = _issue_branch(job)
            exists, known, _ = _branch_info(self.gh, repo, branch)
            if not known:
                return self._fail(job, "remote branch state unknown; refusing to push")
            if exists:
                job.notes["existing_branch"] = branch
                return self._fail(
                    job, f"remote moved; re-run to rebase (branch {branch} already exists)")

        # (c) commit + explicit, never-forced push.
        ops = self._ops()
        message = f"gha-worker {job.kind.value} #{job.number} ({job.job_id})"
        produced = ""
        try:
            produced = str(ops.commit_all(wt, message) or "")
        except Exception as exc:
            return self._fail(job, f"commit failed: {_short(exc)}")
        self._persist(job)  # persist BEFORE the remote write
        if self._stopped(job.job_id):
            return self._halt_stopped(job)
        if not produced:
            job.notes["no_changes"] = True
        target_branch, refusal = _push_target(job, pr, produced or tested)
        if produced and not target_branch:
            return self._fail(job, "no push target branch could be determined")
        if produced:
            rc, out, err = _run_git(
                ["push", PUSH_REMOTE, f"HEAD:refs/heads/{target_branch}"],
                cwd=wt, timeout=self.cfg.run_command_timeout_seconds)
            if rc != 0:
                # Uncertain or failed write: verify by re-reading before any retry.
                _, known, remote_sha = _branch_info(self.gh, repo, target_branch)
                if not (known and remote_sha == produced):
                    return self._fail(job,
                                      f"git push failed (rc={rc}): {_clip(err or out, 300)}")
            job.produced_commits.append(produced)
            self._persist(job)  # persist AFTER the remote write
            # Sweep immediately: the review run starts the moment the push
            # lands, and every further push invites another one.
            self._cancel_copilot_runs(job)
        if refusal:
            job.notes["pr_head_refusal"] = refusal
            job.notes["companion_branch"] = target_branch
        self._persist(job)
        if self._stopped(job.job_id):
            return self._halt_stopped(job)

        # (c2) issue jobs get a PR with explicit base and a body file.
        created: dict = {}
        if not is_fix and produced:
            created = self._create_pr(job, target_branch, tested)
            if job.state in {JobState.FAILED, JobState.STOPPED}:
                return job
        comment_number = job.number if is_fix else created.get("number")

        # (d) per-thread inline replies + evidence-gated resolution.
        unlinked_threads: list[dict] = []
        if is_fix:
            self._respond_threads(job, context, result, produced,
                                  checks_pass, unlinked_threads)
            if job.state is JobState.FAILED:
                return job
            if self._stopped(job.job_id):
                return self._halt_stopped(job)

        # (d2) one summary comment per job.
        if comment_number:
            body = responder.build_comment_body(job.job_id, self._summary_text(
                job, result, tested, verification_note, refusal,
                unlinked_threads, target_branch))
            try:
                if self._stopped(job.job_id):
                    return self._halt_stopped(job)
                cid = responder.post_pr_comment(self.gh, repo, int(comment_number), body)
            except responder.ResponderError as exc:
                return self._fail(job, f"summary comment failed: {_short(exc)}")
            job.notes["summary_comment_id"] = cid
            job.posted_comment_ids.append(cid)
            self._persist(job)

        # (e) cancel Copilot review runs the account's auto-review started on
        # OUR push — GitHub charges per use and repo-visible settings cannot
        # opt out, so the worker stops them itself. Only runs on our own
        # commits are touched.
        self._cancel_copilot_runs(job)
        self._persist(job)

        # (f) final persist, then done; scratch worktree cleaned up on success.
        self._persist(job, JobState.DONE)
        if produced and job.state is JobState.DONE:
            self._cleanup_worktree(job, wt)
        return job

    def _cancel_copilot_runs(self, job: Job) -> None:
        """Stop Copilot code-review runs that fired on OUR commits.

        The account-level auto-review setting is not exposed to any API this
        worker can reach (verified 2026-10-17: repo/user copilot endpoints
        404), and its runs bill the account. Scope guard: only runs whose
        head sha is one of THIS job's produced commits, and whose workflow
        name contains 'copilot' (case-insensitive). Bounded: MAX_RUN_VIEWS
        candidates, one cancel attempt each, failures recorded not raised.
        """
        shas = {str(s) for s in job.produced_commits}
        if not shas:
            return
        already = {int(x) for x in job.notes.get('cancelled_run_ids', [])}
        cancelled: list[dict] = []
        try:
            runs = self.gh.rest("GET", f"repos/{job.repo_full_name}/actions/runs",
                                body={"per_page": 20}) or []
        except Exception as exc:
            job.notes["copilot_cancel_error"] = _clip(str(exc), 200)
            return
        runs_list = runs.get("workflow_runs") if isinstance(runs, dict) else runs
        for run in runs_list or []:
            if len(cancelled) >= MAX_RUN_VIEWS:
                break
            if not isinstance(run, dict):
                continue
            name = str(run.get("name") or run.get("workflowName") or "")
            if "copilot" not in name.lower():
                continue
            if str(run.get("head_sha") or "") not in shas:
                continue
            if str(run.get("status")) not in ("queued", "in_progress", "waiting",
                                              "pending"):
                continue
            run_id = run.get("id")
            if int(run_id or 0) in already:
                continue  # already cancelled by an earlier sweep in this job
            try:
                self.gh.rest("POST",
                             f"repos/{job.repo_full_name}/actions/runs/{run_id}/cancel")
                cancelled.append({"id": run_id, "name": name})
                already.add(int(run_id or 0))
            except Exception as exc:
                job.notes["copilot_cancel_error"] = (
                    f"run {run_id}: {_clip(str(exc), 160)}")
        if cancelled:
            job.notes["cancelled_copilot_runs"] = (
                list(job.notes.get("cancelled_copilot_runs") or []) + cancelled)
            job.notes["cancelled_run_ids"] = sorted(already)

    def _respond_threads(self, job: Job, context: dict, result, produced: str,
                         checks_pass: bool | None,
                         unlinked_threads: list[dict]) -> None:
        """Inline replies via numeric root ids; resolve only with full evidence."""
        repo = job.repo_full_name
        threads = [t for t in (context.get("threads") or []) if not t.get("isResolved")]
        changed = set(getattr(result, "changed_files", None) or [])
        fix_at = ""
        gates_used = 0
        for t in threads:
            if self._stopped(job.job_id):
                self._halt_stopped(job)
                return
            root = t.get("_root_comment_id")
            if root is None:
                unlinked_threads.append(t)
                continue
            gate_ok, why = cheap_resolution_gate(t, checks_pass, changed)
            if gate_ok:
                if gates_used >= MAX_RESOLVE_GATES:
                    gate_ok, why = False, ("resolution evidence check budget "
                                           "exhausted for this job")
                else:
                    gates_used += 1
                    if not fix_at and produced:
                        fix_at = self._commit_date(repo, produced)
                    last_at = self._thread_last_comment_at(t.get("id"))
                    gate_ok, why = new_comment_gate(last_at,
                                                    fix_at if produced else "")
            if int(root) not in job.posted_comment_ids:  # duplicate suppression
                body = responder.build_comment_body(
                    job.job_id, _thread_reply_text(t, produced, gate_ok, why))
                try:
                    if self._stopped(job.job_id):
                        self._halt_stopped(job)
                        return
                    rid = responder.reply_inline(self.gh, repo, job.number,
                                                 int(root), body)
                except responder.ResponderError as exc:
                    self._fail(job, f"inline reply failed on thread "
                                    f"{t.get('id')}: {_short(exc)}")
                    return
                job.posted_comment_ids.append(rid)
                self._persist(job)
            if gate_ok:
                try:
                    if self._stopped(job.job_id):
                        self._halt_stopped(job)
                        return
                    responder.resolve_thread(self.gh, t["id"])
                except responder.ResponderError as exc:
                    self._fail(job, f"thread resolution failed ({t.get('id')}): "
                                    f"{_short(exc)}")
                    return
                job.resolved_thread_ids.append(t["id"])
                self._persist(job)
        return None

    def _thread_last_comment_at(self, thread_id) -> str:
        try:
            data = self.gh.graphql(_THREAD_LAST_COMMENT_QUERY,
                                   {"id": thread_id}) or {}
            nodes = ((data.get("node") or {}).get("comments") or {}).get("nodes") or []
            return str(nodes[0].get("createdAt") or "") if nodes else ""
        except Exception:
            return ""

    def _commit_date(self, repo: str, sha: str) -> str:
        try:
            data = self.gh.rest("GET", f"repos/{repo}/commits/{quote(sha, safe='')}")
            return str(((data or {}).get("commit") or {})
                       .get("committer", {}).get("date") or "")
        except Exception:
            return ""

    def _create_pr(self, job: Job, branch: str, tested: str) -> dict:
        if self._stopped(job.job_id):
            self._halt_stopped(job)
            return {}
        repo = job.repo_full_name
        base = self._base_branch(job)
        title = _clip(f"gha-worker: implement #{job.number} — "
                      f"{job.notes.get('title') or ''}", 200)
        body = (f"Created by gha-worker job {job.job_id} for issue #{job.number}.\n\n"
                f"{_clip(job.notes.get('agent_summary'), 2000)}\n\n"
                f"Verification: {job.notes.get('verification', '')}\n"
                f"Tested commit: {tested}\n")
        path = responder._write_temp(body)
        try:
            rc, out, err = responder._run_gh(
                ["pr", "create", "--repo", repo, "--base", base, "--head", branch,
                 "--title", title, "--body-file", path],
                timeout=self.cfg.run_command_timeout_seconds)
        finally:
            responder._cleanup_temp(path)
        if rc != 0:
            self._fail(job, f"gh pr create failed (rc={rc}): {_clip(err or out, 300)}")
            return {}
        m = re.search(r"/pull/(\d+)", out or "")
        if not m:
            self._fail(job, f"PR created but its number could not be parsed "
                            f"from: {_clip(out, 120)}; summary comment skipped")
            return {}
        info = {"number": int(m.group(1)),
                "url": _clip(out.strip().splitlines()[-1] if out.strip() else "", 200)}
        job.notes["created_pr"] = info
        self._persist(job)
        return info

    def _summary_text(self, job: Job, result, tested: str, verification: str,
                      refusal: str, unlinked_threads: list[dict],
                      target_branch: str) -> str:
        notes = job.notes
        lines = [f"gha-worker run for {job.kind.value} #{job.number} "
                 f"(job {job.job_id})"]
        if notes.get("no_changes"):
            lines.append("No file changes were produced, so nothing was pushed.")
        else:
            lines.append(f"Pushed commit(s): {', '.join(job.produced_commits) or 'none'} "
                         f"to `{target_branch}`.")
        changed = notes.get("changed_files") or []
        if changed:
            lines.append(f"Changed files ({len(changed)}): "
                         + ", ".join(f"`{f}`" for f in changed[:30])
                         + (" …" if len(changed) > 30 else ""))
        lines.append(f"Verification: {verification}")
        if tested:
            lines.append(f"Tested commit: {tested}")
        if refusal:
            lines.append(f"Note: {refusal} — the original PR head was not fixed.")
        if job.kind is JobKind.FIX_PR:
            resolved = len(job.resolved_thread_ids)
            lines.append(f"Review threads resolved: {resolved}. Threads left "
                         "unresolved carry a reply explaining why.")
        for t in unlinked_threads:
            lines.append(f"Note: thread on `{t.get('path')}` could not be linked to a "
                         "root comment id, so it was not answered inline "
                         f"({t.get('_link_note', 'no REST match')}).")
        if notes.get("agent_summary"):
            lines.append(f"Agent summary: {_clip(notes.get('agent_summary'), 1000)}")
        return "\n".join(lines)

    # -- status ---------------------------------------------------------------

    def status_text(self, job: Job) -> str:
        return format_status(job)


# ---------------------------------------------------------------------------
# module-level helpers used by JobExecutor
# ---------------------------------------------------------------------------

def _looks_like_pr(issue: object) -> bool:
    if not isinstance(issue, dict):
        return True  # unknown shape: refuse rather than assume
    if issue.get("pull_request") is not None:      # REST issue payload
        return True
    if issue.get("isPullRequest") is True:         # gh issue view --json
        return True
    return False


def _issue_branch(job: Job) -> str:
    return f"ghagent/issue-{job.number}"


def _push_target(job: Job, pr: dict, sha_for_suffix: str) -> tuple[str, str | None]:
    """(branch, refusal). Forked PR heads always get a companion branch here:
    pushing the fork branch name via origin would create or clobber a branch
    of that name in the BASE repository, which this worker must never do."""
    if job.kind is JobKind.FIX_PR:
        head = str((pr or {}).get("headRefName") or "")
        if (pr or {}).get("isCrossRepository"):
            name = f"gha/fix-{job.number}-{str(sha_for_suffix)[:8]}"
            extra = "" if (pr or {}).get("maintainerCanModify") \
                else " (maintainer edits are not enabled on the fork)"
            return name, f"PR head lives in a fork{extra}; pushed companion branch"
        if not head:
            return "", "PR head branch name unknown"
        return head, None
    return _issue_branch(job), None


def _branch_info(gh, repo_full_name: str, branch: str) -> tuple[bool, bool, str]:
    """(exists, known, sha) — known False means the check itself failed."""
    try:
        data = gh.rest("GET", f"repos/{repo_full_name}/branches/{quote(branch, safe='')}")
        sha = ""
        if isinstance(data, dict) and isinstance(data.get("commit"), dict):
            sha = str(data["commit"].get("sha") or "")
        return True, True, sha
    except Exception as exc:
        if _is_not_found(exc):
            return False, True, ""
        return False, False, ""
