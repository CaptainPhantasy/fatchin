"""JobQueue — durable JSON-file job store (implements interfaces.QueueProto).

Layout: one state file ``<state_dir>/jobs.json`` and one lock file
``<state_dir>/queue.lock``. Every operation takes an exclusive fcntl.flock on
the lock file, so concurrent worker processes serialize. Every persist is
atomic: write a temp file in the same directory, flush, fsync, os.replace onto
jobs.json, then fsync the directory — a crash mid-write can never leave a torn
jobs.json behind, only the old or the new complete file.

CRASH-DETECTABILITY CONTRACT
----------------------------
Callers MUST persist job state via update() BEFORE each remote write and
AFTER it. The gap between those two persists is the window in which a crash
leaves the job claiming work whose remote effect is unknown; anything that
reads the queue later (executor or reconcile_with_remote) detects that window
by comparing the recorded state against the remote, instead of trusting it.

Read errors are preserved: a corrupt or structurally wrong jobs.json raises
QueueError rather than being silently treated as an empty queue.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ghagent.github_client import GitHubError
from ghagent.interfaces import GitHubClientProto, Job, JobKind, JobState

TERMINAL_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.STOPPED})


class QueueError(RuntimeError):
    """The durable store is unreadable or structurally wrong."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _job_to_dict(job: Job) -> dict[str, Any]:
    data = {f: getattr(job, f) for f in Job.__dataclass_fields__}
    data["kind"] = job.kind.value
    data["state"] = job.state.value
    return data


def _job_from_dict(raw: dict[str, Any]) -> Job:
    return Job.from_json(json.dumps(raw))


class JobQueue:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.state_dir = Path(state_dir)
        self.jobs_path = self.state_dir / "jobs.json"
        self.lock_path = self.state_dir / "queue.lock"

    # -- locking / persistence ----------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load(self) -> dict[str, Any]:
        """Load the store. Caller holds the lock. A missing file is an empty
        queue; a corrupt file is an error we refuse to paper over."""
        if not self.jobs_path.exists():
            return {"jobs": {}}
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QueueError(
                f"cannot read {self.jobs_path}: {exc}; refusing to invent "
                f"empty state") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), dict):
            raise QueueError(
                f"{self.jobs_path} has unexpected structure; refusing to load")
        return raw

    def _save(self, store: dict[str, Any]) -> None:
        """Atomically persist. Caller holds the lock."""
        tmp_path = self.jobs_path.with_name(
            f"{self.jobs_path.name}.tmp.{os.getpid()}")
        payload = json.dumps(store, indent=2, sort_keys=True)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.jobs_path)
        dir_fd = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # -- QueueProto -----------------------------------------------------------

    def add(self, job: Job) -> bool:
        """Register a job. False when another job already holds the same
        dedup_key and that job is still open or finished successfully —
        identical work must not run twice, but a FAILED or STOPPED job may
        be retried by a fresh request. Stamps created_at/updated_at (UTC ISO)
        on the caller's object and persists."""
        with self._locked():
            store = self._load()
            jobs = store["jobs"]
            if job.dedup_key:
                retryable = {JobState.FAILED.value, JobState.STOPPED.value}
                for existing in jobs.values():
                    if existing.get("dedup_key") != job.dedup_key:
                        continue
                    if existing.get("state") in retryable:
                        continue
                    return False
            now = _iso(_utcnow())
            if not job.created_at:
                job.created_at = now
            job.updated_at = now
            jobs[job.job_id] = _job_to_dict(job)
            self._save(store)
            return True

    def get(self, job_id: str) -> Job | None:
        with self._locked():
            store = self._load()
            raw = store["jobs"].get(job_id)
        return _job_from_dict(raw) if raw is not None else None

    def update(self, job: Job) -> None:
        """Persist the caller's mutated Job and stamp updated_at. The job must
        have been added() first — updating an unknown id is a bug and raises
        KeyError rather than silently forking state."""
        with self._locked():
            store = self._load()
            jobs = store["jobs"]
            if job.job_id not in jobs:
                raise KeyError(f"unknown job_id {job.job_id!r}; add() it first")
            # A UI/CLI stop is authoritative even if an in-flight executor
            # still holds an older copy of this task in memory.
            if jobs[job.job_id].get('state') == JobState.STOPPED.value:
                job.state = JobState.STOPPED
            job.updated_at = _iso(_utcnow())
            jobs[job.job_id] = _job_to_dict(job)
            self._save(store)

    def all(self) -> list[Job]:
        with self._locked():
            store = self._load()
            jobs = [_job_from_dict(raw) for raw in store["jobs"].values()]
        jobs.sort(key=lambda j: (j.created_at, j.job_id))
        return jobs

    def find_open_by_repo_number(self, repo: str, number: int,
                                 kind: str | None = None) -> Job | None:
        """The newest open job for this repo+number. Open means state not in
        {DONE, FAILED, STOPPED}. With kind, only that kind counts, so one
        task type never blocks a different one on the same item."""
        candidates = [
            j for j in self.all()
            if j.repo_full_name == repo and j.number == number
            and j.state not in TERMINAL_STATES
            and (kind is None or j.kind.value == kind)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda j: (j.created_at, j.job_id))
        return candidates[-1]

    def find_open_by_repo(self, repo: str, kind: str) -> Job | None:
        """Newest open job of one kind for a whole repository (repo-level
        tasks carry number 0 and must not block item tasks)."""
        candidates = [
            j for j in self.all()
            if j.repo_full_name == repo and j.kind.value == kind
            and j.state not in TERMINAL_STATES
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda j: (j.created_at, j.job_id))
        return candidates[-1]

    def stop_job(self, job_id: str) -> bool:
        """Mark a job STOPPED (no further remote writes). Idempotent: True only
        when a live job was stopped by this call; False for unknown ids and
        already-terminal jobs."""
        with self._locked():
            store = self._load()
            raw = store["jobs"].get(job_id)
            if raw is None:
                return False
            job = _job_from_dict(raw)
            if job.state in TERMINAL_STATES:
                return False
            job.state = JobState.STOPPED
            job.updated_at = _iso(_utcnow())
            store["jobs"][job_id] = _job_to_dict(job)
            self._save(store)
            return True

    def reconcile_with_remote(self, gh: GitHubClientProto) -> list[str]:
        """Compare open FIX_PR jobs against the remote and return human-readable
        action strings for what happened.

        - PR merged/closed  -> job marked DONE with a dated note (the work's
          outcome is settled; no executor action left).
        - PR head SHA differs from job.head_sha -> note "remote head moved to
          <sha>"; the state is left to the executor, which owns retry/abort
          policy. Jobs recorded without a head_sha are skipped for this check.
        - Lookup failures are reported per job as an action string, never
          raised, so one broken PR cannot hide the rest."""
        actions: list[str] = []
        for job in self.all():
            if job.kind is not JobKind.FIX_PR or job.state in TERMINAL_STATES:
                continue
            try:
                view = gh.pr_view(job.repo_full_name, job.number)
            except GitHubError as exc:
                actions.append(
                    f"{job.job_id}: reconcile lookup failed for "
                    f"{job.repo_full_name}#{job.number}: {exc}")
                continue
            pr_state = str(view.get("state", "")).upper()
            remote_head = str(view.get("headRefOid", "") or "")
            stamp = _iso(_utcnow())
            if pr_state in {"MERGED", "CLOSED"}:
                job.state = JobState.DONE
                job.notes[f"reconcile:{stamp}"] = (
                    f"PR {job.repo_full_name}#{job.number} is "
                    f"{pr_state.lower()}; marked DONE")
                self.update(job)
                actions.append(
                    f"{job.job_id}: PR {job.repo_full_name}#{job.number} "
                    f"{pr_state.lower()} -> DONE")
            elif job.head_sha and remote_head and remote_head != job.head_sha:
                job.notes[f"reconcile:{stamp}"] = f"remote head moved to {remote_head}"
                self.update(job)
                actions.append(
                    f"{job.job_id}: remote head moved to {remote_head} "
                    f"(state left to executor)")
        return actions
