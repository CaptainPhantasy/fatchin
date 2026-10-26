"""Shared contracts for gha-worker. Every module codes against these.

Stdlib only (Python 3.14). No third-party imports anywhere in this package.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


# Hidden marker embedded in every comment the worker posts, so the worker can
# recognize and ignore its own output when scanning conversations.
COMMENT_MARKER = "<!-- gha-worker:v1:"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    """One explicitly selected repository."""
    owner: str                      # e.g. "octocat"
    name: str                       # e.g. "hello-world"
    base_branch: str = "main"       # authorized base for issue implementations
    checks: list[str] = field(default_factory=list)  # shell commands run in the worktree
    # Rulesets API is plan-limited (HTTP 403) on private free-plan repos. When
    # the owner sets this, an unreadable ruleset is recorded as unverified
    # instead of holding every write. Everything else still blocks.
    allow_unknown_rulesets: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class ModelConfig:
    base_url: str = "http://127.0.0.1:8080/v1"  # broker proxy; no trailing slash
    model_id: str = "your-model"
    disable_reasoning: bool = False  # send thinking={"type":"disabled"} to Z.ai GLM
    # How the bearer token is obtained. "broker-lease" = mint/renew/revoke a
    # short-lived lease via the broker admin API (requires MCP_SECRET_BROKER_ADMIN_TOKEN
    # in the worker env file). "env" = read GHA_MODEL_API_KEY directly.
    auth_mode: str = "broker-lease"
    broker_base_url: str = "http://127.0.0.1:8080"
    broker_consumer_id: str = "fatchin"
    broker_server_id: str = "your-provider"
    lease_ttl_seconds: int = 900
    request_timeout_seconds: int = 180
    max_tokens: int = 8192          # your-model is a reasoning model; keep generous
    max_calls_per_job: int = 60     # spend ceiling: model calls per job
    max_calls_per_day: int = 600    # spend ceiling: model calls per calendar day (UTC)


@dataclass
class Config:
    github_host: str = "github.com"
    rest_api_version: str = "2022-11-28"
    poll_interval_seconds: int = 120
    command_prefix: str = "/agent"
    allowed_command_users: list[int] = field(default_factory=list)  # GitHub numeric user IDs
    repos: list[RepoConfig] = field(default_factory=list)
    model: ModelConfig = field(default_factory=ModelConfig)
    state_dir: str = ""             # MUST be inside the member's own space; required
    worktree_root: str = ""         # where per-job git worktrees are created; required
    max_tool_iterations: int = 40
    run_command_timeout_seconds: int = 600
    run_command_output_cap: int = 100_000
    poll_repos: bool = True         # poll issues/PRs for commands
    fleet_policy: dict = field(default_factory=dict)  # org opt-in / personal opt-out
    max_fleet_publications_per_repo_per_day: int = 12
    max_fleet_calls_total: int = 0  # 0 = no per-run ceiling

    @staticmethod
    def load(path: str | Path) -> "Config":
        p = Path(path)
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
        repos = [RepoConfig(**r) for r in raw.pop("repos", [])]
        model_raw = raw.pop("model", {})
        model = ModelConfig(**model_raw)
        cfg = Config(repos=repos, model=model, **raw)
        if not cfg.state_dir or not cfg.worktree_root:
            raise ValueError("config must set state_dir and worktree_root")
        return cfg


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------

class JobKind(str, Enum):
    FIX_PR = "fix-pr"              # "/agent fix" on a PR
    IMPLEMENT_ISSUE = "implement-issue"  # "/agent implement" on an issue
    # FatChin task types (registry-driven; see ghagent/tasks/types.py)
    EXPLAIN_REPO = "explain-repo"
    WRITE_DOCS = "write-docs"
    TRIAGE_ISSUES = "triage-issues"
    NEW_PROJECT = "new-project"
    REVIEW_PR = "review-pr"
    HYGIENE_SWEEP = "hygiene-sweep"
    CLEANUP_MERGED = "cleanup-merged"
    SYNC_BRANCH = "sync-branch"
    BACKUP_REPO = "backup-repo"
    ORGANIZE_WORK = "organize-work"
    STATUS = "status"
    STOP = "stop"


class JobState(str, Enum):
    RECEIVED = "received"
    RETRIEVING = "retrieving"      # fetching complete request/review history
    WORKING = "working"            # coding agent in its worktree
    VERIFYING = "verifying"        # repo checks on the recorded commit
    RESPONDING = "responding"      # pushing / posting remote writes
    WAITING_ON_YOU = "waiting-on-you"  # draft ready; nothing moves until you act
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"            # via /agent stop; no further remote writes


@dataclass
class Job:
    job_id: str                     # stable id, e.g. "job_<utcstamp>_<rand>"
    kind: JobKind
    repo_full_name: str             # validated "OWNER/REPO"
    number: int                     # issue or PR number (positive int)
    command_comment_id: int         # REST id of the commanding comment (0 for local submit)
    requested_by: int               # GitHub numeric user id of the authorizer
    head_sha: str = ""              # starting commit for PR jobs
    base_sha: str = ""              # base commit recorded at creation
    dedup_key: str = ""             # kind:repo:number:head_sha or issue revision
    state: JobState = JobState.RECEIVED
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    started_commit: str = ""
    produced_commits: list[str] = field(default_factory=list)
    posted_comment_ids: list[int] = field(default_factory=list)
    resolved_thread_ids: list[str] = field(default_factory=list)
    model_calls_used: int = 0
    error: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(text: str) -> "Job":
        d = json.loads(text)
        d["kind"] = JobKind(d["kind"])
        d["state"] = JobState(d["state"])
        return Job(**d)


@dataclass
class AgentResult:
    """Structured result returned by the coding agent."""
    summary: str
    changed_files: list[str]
    worktree_path: str
    final_commit_sha: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)  # {command, exit, passed, output_tail}
    model_calls_used: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# Protocols (interfaces the concrete modules implement)
# ---------------------------------------------------------------------------

class GitHubClientProto(Protocol):
    """Thin wrapper around the gh CLI. Never logs or returns token material."""
    def rest(self, method: str, path: str, body: dict | None = None,
             paginate: bool = False) -> Any: ...
    def graphql(self, query: str, variables: dict | None = None,
                paginate_path: str | None = None) -> Any: ...
    def pr_view(self, repo: str, number: int) -> dict: ...
    def issue_view(self, repo: str, number: int) -> dict: ...
    def pr_checks(self, repo: str, number: int) -> list[dict]: ...
    def run_view(self, repo: str, run_id: int) -> dict: ...
    def current_login(self) -> str: ...
    def current_user_id(self) -> int: ...


class ModelClientProto(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One chat completion. Returns the assistant message dict
        ({"content": str|None, "tool_calls": [...]|None}). Honors Retry-After;
        raises ModelError on auth/validation errors (never retries those)."""
    @property
    def calls_used(self) -> int: ...


class QueueProto(Protocol):
    def add(self, job: Job) -> bool: ...           # False on duplicate dedup_key
    def get(self, job_id: str) -> Job | None: ...
    def update(self, job: Job) -> None: ...
    def find_open_by_repo_number(self, repo: str, number: int) -> Job | None: ...
    def all(self) -> list[Job]: ...
    def stop_job(self, job_id: str) -> bool: ...
    def reconcile_with_remote(self, gh: GitHubClientProto) -> list[str]: ...
