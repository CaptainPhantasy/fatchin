"""Worker daemon: poll GitHub for /agent commands, run jobs one at a time.

Ops component of gha-worker. Sibling modules (github_client, queue, executor,
model_client, commands, coding_agent, worktree) are imported lazily at daemon
runtime only — see cli._default_worker_factory. This module is testable with
constructor-injected fakes implementing the interfaces Protocols.

Guarantees:
- single instance per state_dir (flock, nonblocking),
- refuses to start without GitHub auth (exit 4 path) or model auth material
  (exit 5 path),
- incremental polling via per-repo `since=` watermarks,
- one job at a time = one writer per PR by construction,
- loop errors never crash the daemon; progressive backoff capped at 15 min,
- health.json written atomically each cycle.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import config_loader
from .control_store import ControlStore, WatchStore
from .interfaces import (
    COMMENT_MARKER,
    Config,
    Job,
    JobKind,
    JobState,
    RepoConfig,
)

POLL_PAGE_SIZE = 100
WATCHED_FILE = "watched.json"
AUTO_JOBS_PER_PR_PER_DAY = 6
AUTO_DEDUP_PREFIX = "auto-fix-pr:"
MAX_BACKOFF_SECONDS = 900  # progressive delay cap: 15 minutes
HEALTH_FILE = "health.json"
WATERMARKS_FILE = "watermarks.json"
LOCK_FILE = "worker.lock"
PID_FILE = "worker.pid"
TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.STOPPED}

_ACTION_KINDS: dict[str, JobKind] = {
    "fix": JobKind.FIX_PR,
    "implement": JobKind.IMPLEMENT_ISSUE,
    "status": JobKind.STATUS,
    "stop": JobKind.STOP,
}


class WorkerAlreadyRunning(Exception):
    """Another daemon holds the state_dir lock (CLI exit code 3)."""


class GitHubAuthUnavailable(Exception):
    """`gh auth status -h github.com` reports no usable auth (exit code 4)."""


class ProviderAuthMissing(Exception):
    """Model auth material absent from the environment (exit code 5)."""


def acquire_worker_lock(state_dir):
    """The daemon and one-shot runner share one writer lock."""
    state_dir = Path(state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_FILE
    fh = open(lock_path, 'a+')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise WorkerAlreadyRunning(f'another gha-worker already holds {lock_path}') from None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ts_newer(candidate: str, reference: str) -> bool:
    a, b = _parse_ts(candidate), _parse_ts(reference)
    if a is None:
        return False
    return b is None or a > b


def _stale(iso_ts: str, days: int = 7) -> bool:
    """True when a timestamp is older than `days` (unparseable = not stale)."""
    when = _parse_ts(iso_ts)
    if when is None:
        return False
    return (datetime.now(timezone.utc) - when).days >= days


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"job_{stamp}_{secrets.token_hex(4)}"


@dataclass
class ParsedCommand:
    """Result of parsing one comment body against the command prefix."""
    kind: JobKind
    number: int | None  # explicit target number in the command text, if any
    repo_full_name: str | None = None  # explicit owner/repo target, if any


def parse_command_text(body: str, prefix: str) -> ParsedCommand | None:
    """Built-in command parser. Used when the ghagent.commands sibling module
    is not importable; see Worker.parse_command for the lazy sibling hook."""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix):].strip().split()
        if not parts:
            continue
        kind = _ACTION_KINDS.get(parts[0].lower())
        if kind is None:
            continue
        number: int | None = None
        for token in parts[1:]:
            digits = token.lstrip("#")
            if digits.isdigit():
                number = int(digits)
                break
        return ParsedCommand(kind=kind, number=number)
    return None


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON with a same-directory temp file + os.replace (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def make_dedup_key(kind: JobKind, repo_full_name: str, number: int,
                   revision: str) -> str:
    """Dedup key in the interfaces.Job.dedup_key format. Prefers the
    ghagent.commands sibling helper when it is importable (lazy); falls back
    to the documented "kind:repo:number:revision" layout."""
    try:
        from ghagent.commands import dedup_key_for as sibling_dedup_key  # lazy sibling
        return sibling_dedup_key(kind, repo_full_name, number, revision)
    except ImportError:
        return f"{kind.value}:{repo_full_name}:{number}:{revision}"


class Worker:
    """Polling daemon. gh/queue/executor are injected (fakes in tests)."""

    def __init__(
        self,
        gh,
        queue,
        executor,
        cfg: Config,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        env: dict[str, str] | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.gh = gh
        self.queue = queue
        self.executor = executor
        self.cfg = cfg
        self.clock = clock
        self.sleeper = sleeper
        self.env = dict(env or {})
        self.stop_event = stop_event or threading.Event()
        self.state_dir = Path(cfg.state_dir).expanduser()
        self.own_logins: set[str] = set()
        self.own_user_id: int | None = None
        self.watermarks: dict[str, str] = {}
        self.watches: dict[str, dict[str, dict]] = {}
        self.last_poll_iso: str = ""
        self._lock_fh = None
        self._error_streak = 0
        self._last_error_sig = ""
        self._backoff_seconds = 0
        # The executor consults this flag between steps; duck-typed injection
        # so the sibling module needs no import here.
        try:
            self.executor.stop_event = self.stop_event
        except AttributeError:
            pass

    # -- logging ------------------------------------------------------------

    def _log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[gha-worker {stamp}] {message}", file=sys.stderr, flush=True)

    # -- single instance ----------------------------------------------------

    def acquire_single_instance_lock(self) -> None:
        self._lock_fh = acquire_worker_lock(self.state_dir)
        atomic_write_json(self.state_dir / PID_FILE, {"pid": os.getpid()})

    def close(self) -> None:
        """Release the single-instance lock (used by tests and shutdown)."""
        fleet_thread = getattr(self, '_fleet_thread', None)
        if fleet_thread is not None:
            self.stop_event.set()
            fleet_thread.join(timeout=15)
        if self._lock_fh is not None:
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    # -- startup ------------------------------------------------------------

    def startup(self) -> None:
        """Lock, verify GitHub auth, verify model auth material, reconcile."""
        self.acquire_single_instance_lock()
        try:
            login = self.gh.current_login()
            user_id = self.gh.current_user_id()
        except Exception as exc:
            raise GitHubAuthUnavailable(
                "GitHub authentication unavailable "
                f"(`gh auth status -h {self.cfg.github_host}` reports no auth: {exc}). "
                "Run: gh auth login --hostname github.com --git-protocol https --web"
            ) from None
        self.own_logins = {login}
        self.own_user_id = user_id
        problems = config_loader.model_auth_problems(self.cfg, self.env)
        if problems:
            from .fleet_providers import fleet_startup_available
            if not (getattr(self, 'fleet_paths', None) and fleet_startup_available(self.cfg, self.env)):
                raise ProviderAuthMissing("; ".join(problems))
            self._legacy_auth_missing = True
            self._log('The agent fleet is available. Ordinary GLM tasks are waiting for their configured connection.')
        self._load_watermarks()
        self._load_watches()
        try:
            self.queue.reconcile_with_remote(self.gh)
        except Exception as exc:
            # Reconcile best-effort: a transient failure must not kill startup.
            self._log(f"queue reconcile_with_remote failed (continuing): {exc}")
        self._write_health("running")
        self._log(
            f"started; polling {len(self.cfg.repos)} repo(s) as {login} (id {user_id})"
        )

    # -- watermarks ---------------------------------------------------------

    def _watermarks_path(self) -> Path:
        return self.state_dir / WATERMARKS_FILE

    def _load_watermarks(self) -> None:
        path = self._watermarks_path()
        if path.exists():
            try:
                self.watermarks = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # Preserve read errors instead of inventing empty state.
                self._log(f"watermarks unreadable, starting from baseline ({exc})")
                self.watermarks = {}
        else:
            self.watermarks = {}

    def _save_watermarks(self) -> None:
        atomic_write_json(self._watermarks_path(), self.watermarks)

    # -- polling ------------------------------------------------------------

    def poll_once(self) -> int:
        """One incremental poll over all configured repos. Returns jobs enqueued."""
        enqueued = 0
        paused = ControlStore(self.state_dir / 'gui-control.json').read().get('polling_paused', False)
        if not self.cfg.poll_repos or paused:
            self.last_poll_iso = _now_iso()
            return 0
        for repo in self.cfg.repos:
            enqueued += self._poll_repo(repo)
        enqueued += self._poll_watched()
        self.last_poll_iso = _now_iso()
        return enqueued

    def _poll_repo(self, repo: RepoConfig) -> int:
        watermark = self.watermarks.get(repo.full_name, "")
        baseline = not watermark
        try:
            comments = self.gh.rest(
                "GET",
                f"repos/{repo.full_name}/issues/comments",
                body={"since": watermark, "per_page": POLL_PAGE_SIZE},
                paginate=True,
            ) or []
        except Exception as exc:
            self._log(f"poll {repo.full_name} failed: {exc}")
            return 0
        enqueued = 0
        latest = watermark
        failed = False
        for comment in comments:
            ts = comment.get("updated_at") or comment.get("created_at") or ""
            if _ts_newer(ts, latest):
                latest = ts
            # Locally re-apply the watermark filter: never reprocess a comment
            # the remote already filtered out by `since=`.
            if not baseline and not _ts_newer(ts, watermark):
                continue
            try:
                if self._handle_comment(repo, comment, baseline):
                    enqueued += 1
            except Exception as exc:
                failed = True
                self._log(
                    f"comment {comment.get('id')} in {repo.full_name} "
                    f"failed to process: {exc}"
                )
        # A timestamp is shared by multiple comments. Preserve the entire
        # batch watermark on any handling failure so neither that comment nor
        # an equal-timestamp peer is silently skipped on retry. Durable job
        # deduplication makes replay of successful comments harmless.
        if not failed and _ts_newer(latest, watermark):
            self.watermarks[repo.full_name] = latest
            self._save_watermarks()
        return enqueued

    def _comment_number(self, comment: dict) -> int | None:
        """The issue/PR number a comment belongs to, from its issue_url."""
        for key in ("issue_url", "html_url"):
            url = comment.get(key) or ""
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                return int(tail)
        return None

    # -- PR watch: authorized PRs get their new review feedback handled
    # -- automatically (until the PR closes, /agent stop, or the daily cap).

    def _load_watches(self) -> None:
        self.watches = WatchStore(self.state_dir / WATCHED_FILE).read()

    def _record_watch(self, job: Job) -> None:
        """Authorize a PR for automatic re-fixing after a successful job."""
        if job.notes.get('auto_watch'):
            return  # Completion must not recreate a watch removed by a member.
        repo = job.repo_full_name
        if job.kind is JobKind.FIX_PR:
            number = job.number
        else:
            created = job.notes.get("created_pr") or {}
            number = int(created.get("number") or 0)
        if not number or repo not in {r.full_name for r in self.cfg.repos}:
            return
        comments = self.gh.rest('GET', f'repos/{repo}/pulls/{number}/comments', paginate=True) or []
        meta = {'last_review_comment_id': max((int(c.get('id', 0)) for c in comments), default=0),
                'authorized_at': _now_iso(), 'scope': 'selected'}
        for key in ['output_dir', 'member', 'member_home']:
            if job.notes.get(key):
                meta[key] = job.notes[key]
        problem = self._watch_destination_problem(meta)
        meta.update(watch_status='needs-folder' if problem else 'watching',
                    status_reason=problem or 'Waiting for new eligible inline review feedback.',
                    hold_reason=problem, last_checked_at=_now_iso())
        WatchStore(self.state_dir / WATCHED_FILE).put(repo, number, meta, only_new=True)
        self._load_watches()

    @staticmethod
    def _watch_destination_problem(meta):
        """A saved subscription never supplies an implicit results folder."""
        if not all(isinstance(meta.get(key), str) and meta[key].strip()
                   for key in ('output_dir', 'member', 'member_home')):
            return 'Choose your member account and results folder before automatic follow-up can start.'
        try:
            home = Path(meta['member_home']).resolve(strict=True)
            output = Path(meta['output_dir']).resolve(strict=True)
            if (not Path(meta['output_dir']).is_absolute() or output == home
                    or not output.is_relative_to(home) or not output.is_dir()
                    or not os.access(output, os.W_OK | os.X_OK)):
                raise ValueError('invalid destination')
        except (OSError, ValueError):
            return 'The chosen results folder is unavailable. Edit this watch and choose a writable folder in your account.'
        return ''

    def _watch_status(self, store, repo, number, status, reason, *, error='', **extra):
        """Merge health only, so a poll cannot replace an edited destination."""
        timestamp = _now_iso()
        patch = dict(watch_status=status, status_reason=reason,
                     hold_reason=reason if status != 'watching' else '',
                     last_checked_at=timestamp, last_error=error,
                     next_check_at=(datetime.now(timezone.utc) + timedelta(
                         seconds=self.cfg.poll_interval_seconds + self._backoff_seconds)).isoformat(timespec='seconds'),
                     **extra)
        if status not in {'lookup-failed', 'needs-folder', 'repository-removed', 'disabled'}:
            patch['last_success_at'] = timestamp
        store.put(repo, number, patch, only_existing=True)
        if (str(number) != '*' and status != 'closed'
                and self.watches.get(repo, {}).get(str(number), {}).get('scope') == 'all'):
            # The UI intentionally collapses derived PR subscriptions into the
            # all-PR row. Carry a child's hold to that visible row as well.
            store.put(repo, '*', {**patch, 'last_checked_pr': int(number)}, only_existing=True)
        current = self.watches.get(repo, {}).get(str(number))
        if current is not None:
            current.update(patch)
        return patch

    def _poll_watched(self) -> int:
        """Explain every watch hold while preserving its feedback cursor."""
        enqueued = 0
        today = datetime.now(timezone.utc).date()
        store = WatchStore(self.state_dir / WATCHED_FILE)
        if store.path.exists():
            self.watches = store.read()
        configured = {r.full_name for r in self.cfg.repos}
        scope_scan_holds = {}
        for repo_name, entries in list(self.watches.items()):
            for key, meta in list(entries.items()):
                if meta.get('enabled') is False:
                    self._watch_status(store, repo_name, key, 'disabled',
                                       'This watch is paused. Edit it to enable automatic follow-up again.')
                elif repo_name not in configured:
                    self._watch_status(store, repo_name, key, 'repository-removed',
                                       'This repository is no longer configured. Add it in Repositories or keep this watch paused.')
                elif self._watch_destination_problem(meta):
                    self._watch_status(store, repo_name, key, 'needs-folder', self._watch_destination_problem(meta))
            scope = entries.get('*')
            if (not scope or scope.get('enabled') is False or repo_name not in configured
                    or self._watch_destination_problem(scope)):
                continue
            try:
                prs = self.gh.rest('GET', f'repos/{repo_name}/pulls',
                                   body={'state': 'open', 'per_page': 100}, paginate=True) or []
                self._watch_status(store, repo_name, '*', 'watching',
                                   'The latest check succeeded. New eligible inline review comments can start follow-up work.')
                for pr in prs:
                    key = str(pr['number'])
                    if key in entries:
                        continue
                    comments = self.gh.rest('GET', f'repos/{repo_name}/pulls/{key}/comments', paginate=True) or []
                    # Check scope under the same lock that creates its child.
                    # Removing a watch during GitHub's reply cannot recreate it.
                    def add_child(value):
                        saved = value.get(repo_name, {})
                        current_scope = saved.get('*')
                        if (not current_scope or current_scope.get('enabled') is False
                                or self._watch_destination_problem(current_scope) or key in saved):
                            return None
                        child = {**current_scope, 'scope': 'all', 'title': pr.get('title', ''),
                                 'last_review_comment_id': max((int(c.get('id', 0)) for c in comments), default=0)}
                        saved[key] = child
                        return dict(child)
                    child = store.change(add_child)
                    if child is not None:
                        entries[key] = child
            except Exception as exc:
                scope_scan_holds[repo_name] = self._watch_status(store, repo_name, '*', 'lookup-failed',
                                   'GitHub could not be checked. Check this repository’s access or try again later.',
                                   error='The latest GitHub lookup did not complete.')
                self._log(f'watch repository lookup {repo_name} failed: {type(exc).__name__}')

        # Keep the strongest child hold on the visible all-PR row. A healthy
        # sibling must not hide an error or an exhausted daily allowance.
        scope_holds = dict(scope_scan_holds)
        rank = {'lookup-failed': 7, 'needs-folder': 6, 'daily-limit': 5,
                'author-not-allowed': 4, 'task-in-progress': 3, 'queued': 2, 'closed': 1}
        for repo_name, entries in sorted(self.watches.items()):
            if repo_name not in configured:
                continue
            for number_s, meta in sorted(((k, v) for k, v in entries.items() if k != '*'), key=lambda kv: int(kv[0])):
                if (meta.get('enabled') is False or meta.get('closed') or self._watch_destination_problem(meta)
                        or (meta.get('scope') == 'all' and (not entries.get('*')
                            or entries['*'].get('enabled') is False
                            or self._watch_destination_problem(entries['*'])))):
                    continue
                number = int(number_s)
                try:
                    pr = self.gh.pr_view(repo_name, number)
                    if not isinstance(pr, dict) or not pr:
                        raise ValueError('Pull request is unavailable')
                    if str(pr.get('state', '')).upper() in ('CLOSED', 'MERGED'):
                        patch = self._watch_status(store, repo_name, number, 'closed',
                                                  'This pull request is closed. Its watch history is kept; no new follow-up will start.',
                                                  closed=True, closed_at=_now_iso())
                        continue
                    comments = self.gh.rest('GET', f'repos/{repo_name}/pulls/{number}/comments',
                                            body={'since': str(meta.get('last_comment_at') or ''),
                                                  'per_page': POLL_PAGE_SIZE}, paginate=True) or []
                except Exception as exc:
                    patch = self._watch_status(store, repo_name, number, 'lookup-failed',
                                               'GitHub could not be checked. Check repository access or wait for the next check.',
                                               error='The latest pull request or feedback lookup did not complete.')
                    if meta.get('scope') == 'all':
                        scope_holds[repo_name] = patch
                    self._log(f'watch lookup {repo_name}#{number} failed: {type(exc).__name__}')
                    continue
                last_id = int(meta.get('last_review_comment_id') or 0)
                new_comments = [c for c in comments if COMMENT_MARKER not in (c.get('body') or '')
                                and int(c.get('id') or 0) > last_id]
                fresh = [c for c in new_comments
                         if (c.get('user') or {}).get('id') in self.cfg.allowed_command_users]
                latest = max(new_comments, key=lambda c: int(c.get('id') or 0), default=None)
                evidence = {}
                if latest:
                    evidence['latest_feedback'] = {
                        'id': latest.get('id'), 'author': (latest.get('user') or {}).get('login') or '',
                        'url': latest.get('html_url') or '', 'created_at': latest.get('created_at') or '',
                        'eligible': (latest.get('user') or {}).get('id') in self.cfg.allowed_command_users}
                if not meta.get('stale_notified'):
                    updated = str(pr.get('updatedAt') or pr.get('updated_at') or '')
                    if _stale(updated, days=7) and str(pr.get('state', '')).upper() == 'OPEN':
                        store.put(repo_name, number, {'stale_notified': _now_iso()}, only_existing=True)
                        self._write_digest_event('stale-pr', {
                            'repo': repo_name, 'number': number,
                            'text': f'Pull request #{number} in {repo_name} has waited a week for a decision. '
                                    'Merging or closing stays with you.'})
                auto_today = sum(1 for j in self.queue.all()
                                 if j.dedup_key.startswith(f'{AUTO_DEDUP_PREFIX}{repo_name}:{number}:')
                                 and (_parse_ts(j.created_at) or _parse_ts('1970-01-01')).date() == today)
                evidence['auto_jobs_today'] = auto_today
                evidence['cap_reset_at'] = ''
                if not fresh:
                    status = 'author-not-allowed' if new_comments else 'watching'
                    reason = ('New feedback came from a person or bot who is not allowed to start automatic work. '
                              'Review allowed GitHub users in Settings, or create a task yourself.' if new_comments else
                              'The latest check succeeded. Waiting for new eligible inline review comments.')
                elif auto_today >= AUTO_JOBS_PER_PR_PER_DAY:
                    status = 'daily-limit'
                    evidence['cap_reset_at'] = datetime.combine(today + timedelta(days=1),
                                                               datetime.min.time(), timezone.utc).isoformat()
                    reason = 'Six automatic follow-ups have already started for this pull request today. '
                    reason += 'The new feedback is saved for the next UTC day, or you can create a task yourself.'
                elif self.queue.find_open_by_repo_number(repo_name, number):
                    status = 'task-in-progress'
                    reason = 'A task for this pull request is already waiting or running. Open Tasks to follow it; this feedback is kept.'
                else:
                    eligible = max(fresh, key=lambda c: int(c.get('id') or 0))
                    # Bind the newest destination under the watch lock. Edits
                    # made while GitHub replied cannot be overwritten here.
                    with store.lock():
                        value = store._read()
                        saved = value.get(repo_name, {}).get(number_s)
                        if not saved or saved.get('enabled') is False or saved.get('closed'):
                            continue
                        if saved.get('scope') == 'all' and value.get(repo_name, {}).get('*', {}).get('enabled') is False:
                            continue
                        problem = self._watch_destination_problem(saved)
                        if problem:
                            continue
                        job = Job(job_id=new_job_id(), kind=JobKind.FIX_PR, repo_full_name=repo_name,
                                  number=number, command_comment_id=int(eligible.get('id') or 0),
                                  requested_by=int(eligible.get('user', {}).get('id') or 0),
                                  head_sha=self._pr_head_sha(pr),
                                  dedup_key=f"{AUTO_DEDUP_PREFIX}{repo_name}:{number}:{eligible['id']}",
                                  created_at=_now_iso(), updated_at=_now_iso(),
                                  notes={'auto_watch': True, **{k: saved[k] for k in ('output_dir', 'member', 'member_home')}})
                        if self.queue.add(job):
                            enqueued += 1
                        saved.update(last_review_comment_id=int(eligible['id']),
                                     last_comment_at=eligible.get('created_at') or saved.get('last_comment_at') or '')
                        atomic_write_json(store.path, value)
                    status = 'queued'
                    reason = 'New eligible feedback has queued a task. Open Tasks to follow its checks and results.'
                patch = self._watch_status(store, repo_name, number, status, reason, **evidence)
                if meta.get('scope') == 'all' and rank.get(status, 0) > rank.get(scope_holds.get(repo_name, {}).get('watch_status'), 0):
                    scope_holds[repo_name] = patch
        for repo_name, patch in scope_holds.items():
            store.put(repo_name, '*', patch, only_existing=True)
        if store.path.exists():
            self.watches = store.read()
        return enqueued

    def parse_command(self, body: str, target_kind: str | None = None,
                      target_number: int | None = None) -> ParsedCommand | None:
        """Prefer the ghagent.commands sibling parser (lazy import), which
        validates that the verb agrees with what the comment sits on; fall
        back to the built-in parser when the sibling is absent or the target
        kind is not yet known. A sibling that returns an unexpected shape is a
        loud error, never silently ignored."""
        if target_kind is None:
            return parse_command_text(body, self.cfg.command_prefix)
        try:
            from ghagent.commands import parse_command as sibling_parse  # lazy sibling
        except ImportError:
            return parse_command_text(body, self.cfg.command_prefix)
        parsed = sibling_parse(body, target_kind, target_number,
                               command_prefix=self.cfg.command_prefix)
        if parsed is None:
            return None
        kind = getattr(parsed, "kind", None)
        number = getattr(parsed, "number", None)
        if not isinstance(kind, JobKind):
            raise TypeError(
                "ghagent.commands.parse_command returned an object without a "
                f"JobKind .kind attribute: {type(parsed).__name__}"
            )
        repo_full_name = getattr(parsed, "repo_full_name", None)
        return ParsedCommand(kind=kind, number=int(number),
                             repo_full_name=repo_full_name or None)

    def _resolve_target(self, repo_full_name: str, number: int) -> tuple[bool, str]:
        """Is the number a PR? Returns (is_pr, head_sha). Falls back to issue."""
        try:
            pr = self.gh.pr_view(repo_full_name, number)
        except Exception:
            return False, ""
        return True, self._pr_head_sha(pr)

    @staticmethod
    def _pr_head_sha(pr: dict) -> str:
        for key in ("headRefOid", "head_sha", "headSha", "head"):
            value = pr.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("sha") or value.get("oid")
                if isinstance(nested, str) and nested:
                    return nested
        return ""

    def _handle_comment(self, repo: RepoConfig, comment: dict,
                        baseline: bool) -> bool:
        """Turn one comment into at most one queued job. Returns True if a job
        was enqueued (or fast-pathed)."""
        body = comment.get("body") or ""
        user = comment.get("user") or {}
        author_id = user.get("id")
        author_login = user.get("login", "")
        if COMMENT_MARKER in body:
            return False  # our own output (every worker comment carries it)
        # NOTE: no author-identity own-filter here. In a single-account setup
        # the owner commands from the same account the worker posts as; the
        # hidden marker is the only reliable own-output test. A separate bot
        # account is still covered, because its posts also carry the marker.
        if author_id not in set(self.cfg.allowed_command_users):
            return False  # unauthorized author
        # Cheap text-only pre-filter (no gh round-trip) so ordinary comments
        # cost nothing; the authoritative sibling parse happens once the
        # target kind (pr vs issue) is known.
        if parse_command_text(body, self.cfg.command_prefix) is None:
            return False
        number = self._comment_number(comment)
        if not number or number <= 0:
            self._log(f"command comment {comment.get('id')} has no target number")
            return False
        is_pr, head_sha = self._resolve_target(repo.full_name, number)
        parsed = self.parse_command(body, "pr" if is_pr else "issue", number)
        if parsed is None:
            return False  # an ordinary comment (or verb/target mismatch)
        if parsed.repo_full_name and parsed.repo_full_name != repo.full_name:
            self._log(
                f"command targets {parsed.repo_full_name} which is not the "
                f"commented repo {repo.full_name}; skipped"
            )
            return False
        kind = parsed.kind
        number = parsed.number or number
        if kind is JobKind.FIX_PR and not is_pr:
            self._log(
                f"#{number} in {repo.full_name} is not a pull request; "
                f"'{self.cfg.command_prefix} fix' skipped"
            )
            return False
        # PR jobs dedup on head SHA; issue jobs on the commanding comment id
        # (each new /agent implement comment is a fresh request).
        revision = head_sha if kind is JobKind.FIX_PR else str(comment.get("id") or 0)
        job = Job(
            job_id=new_job_id(),
            kind=kind,
            repo_full_name=repo.full_name,
            number=number,
            command_comment_id=int(comment.get("id") or 0),
            requested_by=int(author_id or 0),
            head_sha=head_sha,
            dedup_key=make_dedup_key(kind, repo.full_name, number, revision),
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        if baseline:
            # First-ever poll for this repo: record the watermark, do not
            # replay the whole comment history as commands.
            return False
        if not self.queue.add(job):
            return False  # duplicate dedup_key
        self._fast_path(job, repo.full_name, number)
        return True

    def _fast_path(self, job: Job, repo_full_name: str, number: int) -> None:
        """STATUS and STOP complete immediately instead of being executed."""
        if job.kind is JobKind.STATUS:
            # Report the saved state of this issue/PR's own jobs (newest
            # first), not the STATUS job itself and not other targets.
            relevant = [
                j for j in self.queue.all()
                if j.repo_full_name == repo_full_name
                and j.number == number
                and j.job_id != job.job_id
            ]
            relevant.sort(key=lambda j: j.created_at or "", reverse=True)
            relevant = relevant[:3]
            try:
                if relevant:
                    text = "\n\n".join(self.executor.status_text(j) for j in relevant)
                else:
                    # Nothing recorded for this target yet: render the STATUS
                    # job itself through the executor (its context names the
                    # repo/number and current queue state).
                    text = self.executor.status_text(job)
            except Exception as exc:
                text = f"status unavailable: {exc}"
            job.notes["status_posted"] = self._post_comment(
                repo_full_name, number, f"status:{job.job_id}", text
            )
            job.state = JobState.DONE
            job.updated_at = _now_iso()
            self.queue.update(job)
        elif job.kind is JobKind.STOP:
            # Target the oldest open job for this issue/PR other than this
            # STOP job itself (queue.find_open_by_repo_number cannot express
            # the exclusion and would match the STOP job we just enqueued).
            candidates = [
                j for j in self.queue.all()
                if j.repo_full_name == repo_full_name
                and j.number == number
                and j.state not in TERMINAL_STATES
                and j.job_id != job.job_id
                and j.kind is not JobKind.STOP
            ]
            target = max(candidates,
                         key=lambda j: (j.created_at or "", j.job_id),
                         default=None)
            stopped_id = None
            if target is not None:
                if self.queue.stop_job(target.job_id):
                    stopped_id = target.job_id
            job.notes["stopped_job"] = stopped_id
            job.state = JobState.DONE
            job.updated_at = _now_iso()
            self.queue.update(job)

    def _post_comment(self, repo_full_name: str, number: int, tag: str,
                      text: str) -> str:
        """Post a marked comment. When remote write fails, write locally so
        the answer is never silently lost. Returns where it landed."""
        payload = {"body": f"{COMMENT_MARKER}{tag}-->\n{text}"}
        try:
            self.gh.rest(
                "POST",
                f"repos/{repo_full_name}/issues/{number}/comments",
                body=payload,
            )
            return "remote"
        except Exception as exc:
            self._log(
                f"remote comment write to {repo_full_name}#{number} failed "
                f"({exc}); writing locally"
            )
            fallback_dir = self.state_dir / "comments-fallback"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            safe_repo = repo_full_name.replace("/", "_")
            path = fallback_dir / f"{safe_repo}_{number}_{int(time.time())}.md"
            path.write_text(payload["body"], encoding="utf-8")
            return f"local:{path}"

    # -- job drain ----------------------------------------------------------

    def _drain_jobs(self) -> int:
        """Run open jobs sequentially, oldest first: one at a time means one
        writer per PR by construction. Jobs waiting on a person (drafts) are
        not run — they resume only through an explicit user action.
        Returns the number of jobs attempted."""
        if getattr(self, '_legacy_auth_missing', False):
            return 0
        open_jobs = [
            j for j in self.queue.all()
            if j.state not in TERMINAL_STATES
            and j.state is not JobState.WAITING_ON_YOU
        ]
        open_jobs.sort(key=lambda j: (j.created_at or "", j.job_id))
        attempted = 0
        for job in open_jobs:
            if self.stop_event.is_set():
                break
            current = self.queue.get(job.job_id) or job
            if current.state in TERMINAL_STATES:
                continue
            if current.state is JobState.WAITING_ON_YOU:
                continue
            attempted += 1
            self._log(
                f"running job {current.job_id} "
                f"({current.kind.value} {current.repo_full_name}#{current.number})"
            )
            try:
                ControlStore(self.state_dir / 'execution.json').change(
                    lambda record: record.update(pid=os.getpid(), job_id=current.job_id,
                                                 member=current.notes.get('member', '')))
                self.executor.execute(current)
            except Exception as exc:
                current = self.queue.get(current.job_id) or current
                current.state = JobState.FAILED
                current.error = f"{type(exc).__name__}: {exc}"[:500]
                current.attempts += 1
                current.updated_at = _now_iso()
                self.queue.update(current)
                self._log(f"job {current.job_id} failed: {current.error}")
                self._write_digest('done', current,
                                   f"{current.kind.value} {current.repo_full_name}"
                                   f"#{current.number} failed: {current.error[:160]}")
            else:
                current = self.queue.get(current.job_id) or current
                if current.state is JobState.DONE:
                    try:
                        self._record_watch(current)
                    except Exception as exc:
                        self._log(f"watch record failed: {exc}")
                    self._write_digest('done', current,
                                       f"{current.kind.value} {current.repo_full_name}"
                                       f"#{current.number} finished")
                elif current.state is JobState.WAITING_ON_YOU:
                    self._write_digest('waiting-on-you', current,
                                       f"A draft for {current.repo_full_name}"
                                       f"#{current.number} is ready for your decision")
            finally:
                ControlStore(self.state_dir / 'execution.json').change(lambda record: record.clear())
        return attempted

    def _write_digest(self, kind: str, job: Job, text: str) -> None:
        """Append one plain-language doorbell event (best effort)."""
        self._write_digest_event(kind, {
            'job_id': job.job_id, 'repo': job.repo_full_name,
            'number': job.number, 'task': job.kind.value, 'text': text})

    def _write_digest_event(self, kind: str, fields: dict) -> None:
        try:
            with open(self.state_dir / 'digest.jsonl', 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'kind': kind, 'at': _now_iso(), **fields},
                                        ensure_ascii=False) + '\n')
        except OSError as exc:
            self._log(f"digest write failed: {exc}")

    # -- health -------------------------------------------------------------

    def _write_health(self, state: str = "running") -> None:
        jobs = self.queue.all()
        today = datetime.now(timezone.utc).date()
        open_count = 0
        done_today = 0
        model_calls = 0
        for job in jobs:
            updated = _parse_ts(job.updated_at)
            is_today = updated is not None and updated.date() == today
            if job.state not in TERMINAL_STATES:
                open_count += 1
            if is_today and job.state is JobState.DONE:
                done_today += 1
            if is_today:
                model_calls += job.model_calls_used
        atomic_write_json(
            self.state_dir / HEALTH_FILE,
            {
                "last_poll_iso": self.last_poll_iso,
                "jobs_open": open_count,
                "jobs_done_today": done_today,
                "model_calls_today": self._model_calls_today(model_calls),
                "pid": os.getpid(),
                "state": state,
            },
        )

    def _model_calls_today(self, fallback=0):
        path = self.state_dir / 'model-call-counts.json'
        if not path.exists():
            return fallback
        usage = ControlStore(path).read()
        return usage.get('count', 0) if usage.get('date') == datetime.now(timezone.utc).date().isoformat() else 0

    # -- main loop ----------------------------------------------------------

    def _note_error(self, exc: Exception) -> None:
        signature = f"{type(exc).__name__}: {exc}"
        if signature == self._last_error_sig:
            self._error_streak += 1
        else:
            self._error_streak = 1
            self._last_error_sig = signature
        base = max(self.cfg.poll_interval_seconds, 1)
        self._backoff_seconds = min(
            base * (2 ** min(self._error_streak - 1, 10)), MAX_BACKOFF_SECONDS
        )
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            self._backoff_seconds = min(int(retry_after), MAX_BACKOFF_SECONDS)
        self._log(
            f"loop error (streak {self._error_streak}, "
            f"next delay {self._backoff_seconds}s): {exc}"
        )

    def _note_success(self) -> None:
        self._error_streak = 0
        self._last_error_sig = ""
        self._backoff_seconds = 0

    def _interruptible_sleep(self, seconds: float) -> None:
        if seconds <= 0 or self.stop_event.is_set():
            return
        deadline = self.clock() + seconds
        while not self.stop_event.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            before = self.clock()
            self.sleeper(min(remaining, 1.0))
            if self.clock() == before:
                # Fake/non-advancing clock: treat the sleep as elapsed rather
                # than spin. Real time.monotonic always advances.
                return

    def run_forever(self) -> None:
        """Poll, drain jobs, sleep — until stop_event is set. Never raises on
        loop errors; every error is logged and backed off."""
        if getattr(self, 'fleet_paths', None):
            self._fleet_thread = threading.Thread(target=self._run_fleet,
                                                   name='fatchin-fleet', daemon=True)
            self._fleet_thread.start()
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._note_error(exc)
            else:
                self._note_success()
            try:
                self._drain_jobs()
            except Exception as exc:
                self._note_error(exc)
            self._write_health("running")
            self._interruptible_sleep(
                self.cfg.poll_interval_seconds + self._backoff_seconds
            )
        self._write_health("stopped")
        self._log("stopped cleanly")

    def _run_fleet(self):
        """Recover the coordinator itself without making people reset it."""
        from .fleet_store import FleetStore
        from .fleet_supervisor import FleetSupervisor
        config_path, env_path = self.fleet_paths
        store = FleetStore(self.state_dir / 'fleet-v1')
        while not self.stop_event.is_set():
            try:
                cfg = config_loader.load_config(config_path)
                env = {**os.environ, **config_loader.load_env_file(env_path)}
                FleetSupervisor(cfg, env, store, config_path=config_path,
                                env_path=env_path).run(self.stop_event)
            except Exception as exc:
                # Do not echo potentially credential-bearing provider errors.
                message = 'Fleet coordinator is recovering after ' + type(exc).__name__ + '.'
                self._log(message)
                try:
                    store.supervisor_status(status='recovering', error=message)
                except Exception:
                    pass  # Preserve an unreadable state file; next attempt retries it.
            if not self.stop_event.is_set():
                self.stop_event.wait(5)
