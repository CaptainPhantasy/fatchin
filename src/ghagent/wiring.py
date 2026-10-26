"""Runtime wiring: builds the real component graph from a loaded Config.

Only the daemon/CLI entry points import this module. Every constructor here
matches the real sibling signatures; if a constructor changes, this file is
the one place to adjust.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .interfaces import Config


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:80] or "repo"


class BrokerLeaseBearer:
    """Bearer-token provider backed by a short-lived broker lease.

    Minted lazily on first use and renewed by the manager's own thread; the
    raw model key never enters this process — only the lease token does.
    """

    def __init__(self, cfg: Config, env: dict[str, str]):
        self._cfg = cfg
        self._env = env
        self._manager = None

    def _ensure(self):
        if self._manager is None:
            from .broker_lease import BrokerLeaseManager  # lazy sibling

            admin = self._env.get("MCP_SECRET_BROKER_ADMIN_TOKEN", "")
            if not admin:
                raise RuntimeError(
                    "MCP_SECRET_BROKER_ADMIN_TOKEN is not set; cannot mint "
                    "a model lease (set it in the worker env file)")
            self._manager = BrokerLeaseManager(
                broker_base_url=self._cfg.model.broker_base_url,
                consumer_id=self._cfg.model.broker_consumer_id,
                server_id=self._cfg.model.broker_server_id,
                ttl_seconds=self._cfg.model.lease_ttl_seconds,
                admin_token_provider=lambda: admin,
            )
            self._manager.start()
        return self._manager

    def __call__(self) -> str:
        return self._ensure().bearer()

    def close(self) -> None:
        if self._manager is not None:
            self._manager.stop()
            self._manager = None


class EnvKeyBearer:
    """Bearer-token provider reading a static key from the env dict."""

    def __init__(self, env: dict[str, str]):
        self._env = env

    def __call__(self) -> str:
        key = self._env.get("GHA_MODEL_API_KEY", "")
        if not key:
            raise RuntimeError(
                "GHA_MODEL_API_KEY is not set; no model credential available")
        return key


def make_bearer(cfg: Config, env: dict[str, str]):
    mode = (cfg.model.auth_mode or "broker-lease").strip().lower()
    if mode == "broker-lease":
        return BrokerLeaseBearer(cfg, env)
    if mode == "env":
        return EnvKeyBearer(env)
    raise RuntimeError(
        f"unknown model auth_mode {cfg.model.auth_mode!r} "
        "(expected 'broker-lease' or 'env')")


class WorktreeOpsAdapter:
    """Worktree operations on the executor's positional duck-type.

    Keeps a bare clone cache under <worktree_root>/_repos so worktrees are
    created per job (race-free) while the GitHub remote stays `origin` — a
    push from inside a worktree goes straight to GitHub, never to the cache.
    """

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._root = Path(cfg.worktree_root)
        self._cache_root = self._root / "_repos"

    # -- internals ----------------------------------------------------------

    def _git(self, argv, cwd=None, timeout=300):
        return subprocess.run(
            ["git", *argv], cwd=cwd, capture_output=True, text=True,
            timeout=timeout,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                 "GIT_TERMINAL_PROMPT": "0", "HOME": str(Path.home())},
        )

    def _clone_dir(self, repo_full_name: str) -> Path:
        return self._cache_root / (_safe_name(repo_full_name) + ".git")

    def _ensure_clone(self, repo_full_name: str) -> Path:
        url = f"https://{self._cfg.github_host}/{repo_full_name}.git"
        dest = self._clone_dir(repo_full_name)
        if not (dest / "HEAD").exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            rc = self._git(["clone", "--bare", url, str(dest)])
            if rc.returncode != 0:
                raise RuntimeError(f"clone {repo_full_name} failed: {rc.stderr.strip()[:300]}")
        else:
            # Bare clones have no default remote fetch refspec. Refresh the
            # actual branch refs used for detached task checkouts explicitly.
            refreshed = self._git(["fetch", "origin", "--prune",
                                   "+refs/heads/*:refs/heads/*"], cwd=str(dest))
            if refreshed.returncode != 0:
                raise RuntimeError(f"fetch {repo_full_name} failed: "
                                   f"{refreshed.stderr.strip()[:300]}")
        return dest

    # -- executor-facing surface (positional) --------------------------------

    def fetch_pr_worktree(self, repo_full_name: str, pr_number: int,
                          head_ref: str = "") -> str:
        cache = self._ensure_clone(repo_full_name)
        ref = f"refs/remotes/origin/pr-{pr_number}"
        rc = self._git(["fetch", "origin",
                        f"refs/pull/{pr_number}/head:{ref}"], cwd=str(cache))
        if rc.returncode != 0:
            raise RuntimeError(f"fetch PR #{pr_number} failed: {rc.stderr.strip()[:300]}")
        dest = self._root / _safe_name(
            f"{repo_full_name}-pr{pr_number}-{head_ref or 'head'}")
        rc = self._git(["worktree", "add", "--detach", str(dest), ref],
                       cwd=str(cache))
        if rc.returncode != 0:
            raise RuntimeError(f"worktree add failed: {rc.stderr.strip()[:300]}")
        return str(dest)

    def add_worktree(self, dest_path: str, base_ref: str,
                     repo_full_name: str | None = None) -> str:
        if not repo_full_name:
            raise RuntimeError(
                "add_worktree needs the repository (owner/name) to clone")
        cache = self._ensure_clone(repo_full_name)
        # Detached: a job worktree must never hold the shared base branch
        # (a second job checking out the same branch would collide).
        dest = Path(dest_path)
        if dest.exists():
            self.remove_worktree(str(dest))
        rc = self._git(["worktree", "add", "--detach", str(dest_path), base_ref],
                       cwd=str(cache))
        if rc.returncode != 0:
            raise RuntimeError(
                f"worktree add ({base_ref}) failed: {rc.stderr.strip()[:300]}")
        return str(dest_path)

    # Build outputs that must never enter a worker commit, even when the
    # target repo has no .gitignore of its own.
    ARTIFACT_PATHSPECS = (
        ":(glob)**/__pycache__", ":(glob)**/*.pyc", ":(glob)**/*.pyo",
        ":(glob).pytest_cache/**", ":(glob).mypy_cache/**",
        ":(glob).ruff_cache/**", ":(glob)**/.DS_Store",
    )

    def commit_all(self, wt: str, message: str) -> str:
        add = self._git(["add", "-A"], cwd=wt)
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {add.stderr.strip()[:300]}")
        self._git(["rm", "-r", "--cached", "--quiet", "--ignore-unmatch",
                   "--", *self.ARTIFACT_PATHSPECS], cwd=wt)
        commit = self._git(
            ["-c", "user.name=gha-worker",
             "-c", "user.email=gha-worker@inference",
             "commit", "-m", message],
            cwd=wt)
        if commit.returncode != 0:
            combined = (commit.stderr or "").strip() or (commit.stdout or "").strip()
            both = commit.stdout + commit.stderr
            if ("nothing to commit" in both
                    or "nothing added to commit" in both
                    or "no changes added to the commit" in both):
                return ""
            raise RuntimeError(
                f"git commit failed (rc={commit.returncode}): {combined[:300]}")
        sha = self.current_sha(wt)
        return sha or ""

    def is_clean(self, wt: str) -> bool:
        rc = self._git(["status", "--porcelain"], cwd=wt)
        return rc.returncode == 0 and not rc.stdout.strip()

    def current_sha(self, wt: str) -> str:
        rc = self._git(["rev-parse", "HEAD"], cwd=wt)
        return rc.stdout.strip() if rc.returncode == 0 else ""

    def remove_worktree(self, wt: str) -> None:
        rc = self._git(["worktree", "remove", str(wt)], cwd=wt)
        if rc.returncode != 0:
            rc = self._git(["worktree", "remove", "--force", str(wt)], cwd=wt)
            if rc.returncode != 0:
                raise RuntimeError('Scratch checkout could not be removed; its recovery path has been retained.')

    def prune(self) -> None:
        for cache in self._cache_root.glob("*.git"):
            self._git(["worktree", "prune"], cwd=str(cache))


class CodingAgentRunner:
    """Adapts the executor's single-string run(instruction) to the real
    CodingAgent task-dict contract, binding one worktree + check list."""

    def __init__(self, model_client, cfg: Config, worktree_path: str,
                 checks: list[str]):
        from .coding_agent import CodingAgent  # lazy sibling

        self._agent = CodingAgent(model_client, cfg)
        self._model = model_client
        self._job_id = ''
        self._task = {
            "instruction": "",
            "worktree_path": worktree_path,
            "repo_context": "",
            "checks": list(checks or []),
        }

    def set_stop_check(self, callback):
        self._task["should_stop"] = callback

    def set_job_context(self, job_id):
        self._job_id = job_id

    @property
    def total_model_calls_used(self):
        return getattr(self._model, 'calls_used', None)

    def run(self, instruction: str):
        if hasattr(self._model, 'begin_job'):
            import uuid
            self._model.begin_job(self._job_id or 'local-' + uuid.uuid4().hex)
        self._task["instruction"] = instruction
        result = self._agent.run(self._task)
        # Reservations include failed requests that reached the provider.
        if hasattr(self._model, 'calls_used'):
            result.model_calls_used = max(result.model_calls_used, self._model.calls_used)
        return result


def make_coding_agent_factory(cfg: Config, model_client):
    return lambda wt, checks: CodingAgentRunner(model_client, cfg, wt, checks)


def build_model_client(cfg: Config, env: dict[str, str], *, check_auth=True):
    from .model_client import ModelClient  # lazy sibling

    bearer = make_bearer(cfg, env)
    # Touch the provider once so a missing credential fails here, at startup,
    # instead of mid-job. (bearer() raises when auth material is absent.)
    if check_auth:
        bearer()
    return ModelClient(cfg.model, bearer, day_counter_path=str(Path(cfg.state_dir) / 'model-call-counts.json'))


def default_components(cfg: Config, env: dict[str, str], *, check_model_auth=True):
    """Returns (gh, queue, executor, model_client, bearer_close)."""
    from .executor import JobExecutor  # lazy siblings
    from .github_client import GitHubClient
    from .queue import JobQueue

    gh = GitHubClient(host=cfg.github_host, api_version=cfg.rest_api_version)
    queue = JobQueue(cfg.state_dir)
    model_client = build_model_client(cfg, env) if check_model_auth else build_model_client(cfg, env, check_auth=False)
    executor = JobExecutor(
        gh, queue, cfg,
        make_coding_agent_factory(cfg, model_client),
        worktree_ops=WorktreeOpsAdapter(cfg),
    )
    return gh, queue, executor, model_client
