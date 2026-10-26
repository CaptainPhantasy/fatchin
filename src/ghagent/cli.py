"""gha-worker command line interface.

Subcommands: run, once, status, stop, provider-check, init-config.
Entry point: python3 -m ghagent.cli (src/ on PYTHONPATH).

Exit codes: 0 ok, 2 missing destination (company output contract, exact
message), 3 already running, 4 no GitHub auth, 5 no model auth, 1 other.

provider-check is the diagnostic command that sends one model request;
run and once also use the model while executing coding tasks.
Sibling modules are imported lazily inside the *_default_* factories so the
CLI stays importable (and unit-testable with injected fakes) before merge.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config_loader, worker as worker_mod
from .config_loader import ConfigError
from .interfaces import Config, Job, JobKind, JobState
from .worker import (
    GitHubAuthUnavailable,
    ProviderAuthMissing,
    Worker,
    WorkerAlreadyRunning,
    make_dedup_key,
    new_job_id,
)

DEFAULT_CONFIG_PATH = "~/.config/gha-worker/config.toml"
DEFAULT_ENV_PATH = "~/.config/gha-worker/env"

# Company output contract: printed verbatim, exit code 2.
DESTINATION_MESSAGE = (
    "Error: No destination folder specified. "
    "Supply an output path within your user space."
)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STOP_WAIT_SECONDS = 5.0


@dataclass
class _Factories:
    """Injection points for unit tests; all default to lazy real builders."""
    worker: Callable | None = None
    component: Callable | None = None
    model_client: Callable | None = None


# ---------------------------------------------------------------------------
# lazy sibling builders (daemon runtime only — never imported at module load)
# ---------------------------------------------------------------------------

def _default_components(cfg: Config, env: dict[str, str], *, allow_fleet=False):
    from ghagent.wiring import default_components

    problems = config_loader.model_auth_problems(cfg, env)
    from .fleet_providers import fleet_startup_available
    if problems and not (allow_fleet and fleet_startup_available(cfg, env)):
        raise ProviderAuthMissing('; '.join(problems))
    components = default_components(cfg, env, check_model_auth=False) if problems else default_components(cfg, env)
    gh, queue, executor, _model_client = components
    return gh, queue, executor


def _default_worker(cfg: Config, env: dict[str, str]) -> Worker:
    gh, queue, executor = _default_components(cfg, env, allow_fleet=True)
    return Worker(gh, queue, executor, cfg, env=env)


def _default_model_client(model_cfg, env: dict[str, str], *, day_counter_path=None):
    from ghagent.model_client import ModelClient  # lazy sibling
    from ghagent.wiring import make_bearer

    bearer = make_bearer(_cfg_with_model(model_cfg), env)
    import uuid
    return ModelClient(model_cfg, bearer, max_attempts=1, day_counter_path=day_counter_path,
                       job_scope='provider-check-' + uuid.uuid4().hex)


def _cfg_with_model(model_cfg):
    """Wrap a bare ModelConfig in a Config shell so make_bearer can see the
    broker settings it needs (auth_mode defaults preserved)."""
    from ghagent.interfaces import Config, ModelConfig

    if isinstance(model_cfg, Config):
        return model_cfg
    shell = Config()
    shell.model = model_cfg if isinstance(model_cfg, ModelConfig) else ModelConfig(**model_cfg)
    return shell


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _load_env(path: str | Path) -> dict[str, str]:
    """Env file + process environment (process wins on conflict). File values
    are also exported into os.environ so child `gh` processes inherit them."""
    file_values = config_loader.load_env_file(path)
    for key, val in file_values.items():
        os.environ.setdefault(key, val)
    return {**file_values, **os.environ}


def _load_config_for_run(path: str) -> Config:
    return config_loader.load_config(path)


def _load_lenient(path: str) -> Config:
    """Load config tolerating missing state paths (status/stop/provider-check
    do not write artifacts, so the destination contract does not apply)."""
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(
            f"config file not found: {p} "
            "(create one with: python3 -m ghagent.cli init-config)"
        )
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    repos_raw = data.pop("repos", [])
    model_raw = data.pop("model", {})
    try:
        from .interfaces import ModelConfig, RepoConfig
        return Config(repos=[RepoConfig(**r) for r in repos_raw],
                      model=ModelConfig(**model_raw), **data)
    except TypeError as exc:
        raise ConfigError(
            f"config file has a field the worker does not recognize ({exc})"
        ) from None


def _require_destination(cfg: Config) -> None:
    if not str(cfg.state_dir).strip() or not str(cfg.worktree_root).strip():
        raise ConfigError("missing destination", missing_destination=True)
    _warn_outside_home(cfg)


def _warn_outside_home(cfg: Config) -> None:
    home = Path.home()
    for label, raw in (("state_dir", cfg.state_dir),
                       ("worktree_root", cfg.worktree_root)):
        try:
            resolved = Path(str(raw)).expanduser().resolve()
        except OSError:
            continue
        if resolved != home and home not in resolved.parents:
            print(
                f"warning: {label} {resolved} is outside your home directory; "
                "company law wants tool output inside your own account space",
                file=sys.stderr,
            )


def _err(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


def _sibling_missing(exc: ImportError) -> str:
    name = getattr(exc, "name", None) or exc
    return (f"required module not available: {name}; the ghagent sibling "
            "modules (github_client, queue, executor, model_client, commands) "
            "are not present in this installation")


def _install_signal_handlers(stop_event) -> dict:
    def handler(signum, frame):
        stop_event.set()

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            original = signal.getsignal(sig)
            signal.signal(sig, handler)
            previous[sig] = original
        except ValueError:
            # Not the main thread (e.g. embedded use): signal handling skips.
            pass
    return previous


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def _cmd_run(args, factories: _Factories) -> int:
    cfg = _load_config_for_run(args.config)
    _require_destination(cfg)
    env = _load_env(args.env_file)
    try:
        worker = (factories.worker or _default_worker)(cfg, env)
    except ImportError as exc:
        _err(_sibling_missing(exc))
        return 1
    if factories.worker is None:
        # Keep the fleet in this existing service's lifecycle. Child agents
        # are ordinary one-task Python processes, independently restartable.
        worker.fleet_paths = (str(Path(args.config).expanduser().resolve()),
                              str(Path(args.env_file).expanduser().resolve()))
    _install_signal_handlers(worker.stop_event)
    try:
        return _run_worker(worker)
    finally:
        close = getattr(worker, 'close', None)
        if close:
            close()


def _run_worker(worker):
    try:
        worker.startup()
    except WorkerAlreadyRunning as exc:
        _err(str(exc))
        return 3
    except GitHubAuthUnavailable as exc:
        _err(str(exc))
        return 4
    except ProviderAuthMissing as exc:
        _err(str(exc))
        return 5
    try:
        worker.run_forever()
    except Exception as exc:
        _err(f"daemon stopped on an unexpected error: {exc}")
        return 1
    return 0


def _cmd_once(args, factories: _Factories) -> int:
    cfg = _load_config_for_run(args.config)
    _require_destination(cfg)
    if not _REPO_RE.fullmatch(args.repo):
        _err("--repo must be OWNER/REPO")
        return 1
    if args.number <= 0:
        _err("--number must be a positive integer")
        return 1
    kinds = {
        "fix-pr": JobKind.FIX_PR,
        "implement-issue": JobKind.IMPLEMENT_ISSUE,
    }
    kind = kinds[args.kind]
    try:
        lock = worker_mod.acquire_worker_lock(cfg.state_dir)
    except WorkerAlreadyRunning as exc:
        _err(str(exc))
        return 3
    try:
        return _execute_once(args, factories, cfg, kind)
    finally:
        lock.close()


def _execute_once(args, factories, cfg, kind):
    env = _load_env(args.env_file)
    try:
        gh, queue, executor = (factories.component or _default_components)(cfg, env)
    except ImportError as exc:
        _err(_sibling_missing(exc))
        return 1
    try:
        requested_by = gh.current_user_id()
    except Exception as exc:
        raise GitHubAuthUnavailable(f'GitHub authentication unavailable: {exc}') from None
    head_sha = ""
    if kind is JobKind.FIX_PR:
        try:
            head_sha = Worker._pr_head_sha(gh.pr_view(args.repo, args.number))
        except Exception:
            _err(f"{args.repo}#{args.number} is not a pull request; "
                 "--kind fix-pr requires a PR")
            return 1
    revision = head_sha or f"local-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    job = Job(
        job_id=new_job_id(),
        kind=kind,
        repo_full_name=args.repo,
        number=args.number,
        command_comment_id=args.comment_id or 0,
        requested_by=int(requested_by or 0),
        head_sha=head_sha,
        dedup_key=make_dedup_key(kind, args.repo, args.number, revision),
        created_at=worker_mod._now_iso(),
        updated_at=worker_mod._now_iso(),
    )
    if not queue.add(job):
        print(f"an identical job already exists (dedup_key {job.dedup_key}); "
              "nothing to do")
        return 0
    executor.stop_event = threading.Event()
    previous_handlers = _install_signal_handlers(executor.stop_event)
    try:
        executor.execute(job)
    except Exception as exc:
        job = queue.get(job.job_id) or job
        job.state = JobState.FAILED
        job.error = f"{type(exc).__name__}: {exc}"[:500]
        job.updated_at = worker_mod._now_iso()
        queue.update(job)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    final = queue.get(job.job_id) or job
    print(f"job {final.job_id}: {final.kind.value} {final.repo_full_name}#"
          f"{final.number} -> {final.state.value}")
    if final.produced_commits:
        print(f"commits: {', '.join(final.produced_commits)}")
    if final.error:
        print(f"error: {final.error}")
    return 0 if final.state in (JobState.DONE, JobState.STOPPED) else 1


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        return {"_read_error": str(exc)}


def _cmd_status(args, factories: _Factories) -> int:
    try:
        cfg = _load_lenient(args.config)
    except ConfigError as exc:
        _err(str(exc))
        return 1
    if not str(cfg.state_dir).strip():
        _err("config does not set state_dir; no saved state to show")
        return 1
    state_dir = Path(cfg.state_dir).expanduser()
    if not state_dir.exists():
        print(f"no state yet: {state_dir} does not exist")
        return 0
    health = _read_json(state_dir / "health.json")
    print("== health ==")
    if health is None:
        print("(health.json not written yet)")
    else:
        print(json.dumps(health, indent=2, sort_keys=True))
    watermarks = _read_json(state_dir / "watermarks.json")
    print("== watermarks (per-repo poll position) ==")
    if isinstance(watermarks, dict) and watermarks:
        for repo in sorted(watermarks):
            print(f"  {repo}: {watermarks[repo]}")
    else:
        print("(none)")
    print("== jobs ==")
    jobs_raw = _read_json(state_dir / "jobs.json")
    if jobs_raw is None:
        print("(jobs.json not written yet)")
    elif isinstance(jobs_raw, dict) and "_read_error" in jobs_raw:
        print(f"(jobs.json unreadable, preserved: {jobs_raw['_read_error']})")
    else:
        counts: dict[str, int] = {}
        try:
            entries = jobs_raw.get('jobs', {}) if isinstance(jobs_raw, dict) else jobs_raw
            if isinstance(entries, dict):
                entries = entries.values()
            for entry in entries:
                job = Job.from_json(json.dumps(entry))
                counts[job.state.value] = counts.get(job.state.value, 0) + 1
                print(f"  {job.job_id}  {job.kind.value:17s} "
                      f"{job.repo_full_name}#{job.number:<6} {job.state.value}"
                      + (f"  error: {job.error}" if job.error else ""))
        except Exception as exc:
            print(f"(jobs.json present but could not be interpreted: {exc})")
        if counts:
            print("  states: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def _process_argv(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.decode('utf-8', errors='replace').rstrip('\x00').split('\x00')


def _worker_process_matches(pid: int, config_path: str) -> bool:
    """Identify the actual CLI daemon and its selected configuration."""
    argv = _process_argv(pid)
    if not argv or not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(argv[0]).name):
        return False
    try:
        module_index = argv.index('-m')
    except ValueError:
        return False
    allowed_flags = {'-u', '-B', '-I', '-E', '-s', '-S', '-O', '-OO', '-P'}
    if any(flag not in allowed_flags for flag in argv[1:module_index]):
        return False
    if argv[module_index + 1:module_index + 3] != ['ghagent.cli', 'run']:
        return False
    selected = DEFAULT_CONFIG_PATH
    arguments = argv[module_index + 3:]
    for index, argument in enumerate(arguments):
        if argument in ('-c', '--config'):
            if index + 1 >= len(arguments):
                return False
            selected = arguments[index + 1]
        elif argument.startswith('--config='):
            selected = argument.partition('=')[2]
        elif argument.startswith('-c') and len(argument) > 2:
            selected = argument[2:]
    try:
        if Path(f'/proc/{pid}').stat().st_uid != os.getuid():
            return False
        process_path = Path(selected).expanduser()
        if not process_path.is_absolute():
            process_path = Path(f'/proc/{pid}/cwd').resolve() / process_path
        return process_path.resolve() == Path(config_path).expanduser().resolve()
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed-but-not-yet-reaped zombie still answers kill(0); read its state.
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
        return fields[0] != "Z"
    except (OSError, IndexError):
        return True


def _cmd_stop(args, factories: _Factories) -> int:
    try:
        cfg = _load_lenient(args.config)
    except ConfigError as exc:
        _err(str(exc))
        return 1
    if not str(cfg.state_dir).strip():
        _err("config does not set state_dir; cannot locate the daemon")
        return 1
    state_dir = Path(cfg.state_dir).expanduser()
    health = _read_json(state_dir / "health.json")
    pid = health.get("pid") if isinstance(health, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        print("no running gha-worker found (health.json has no pid)")
        return 1
    if not _pid_alive(pid):
        print(f"no running gha-worker found (pid {pid} is gone)")
        return 1
    if not _worker_process_matches(pid, args.config):
        _err(f'pid {pid} is not the worker for this configuration; refusing to signal it')
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        _err(f"failed to signal pid {pid}: {exc}")
        return 1
    deadline = time.monotonic() + _STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            print(f"stopped gha-worker (pid {pid}) with SIGTERM")
            return 0
        time.sleep(0.1)
    _err(f"pid {pid} did not exit within {_STOP_WAIT_SECONDS:.0f}s; "
         "check journalctl --user -u gha-worker")
    return 1



def _cmd_provider_switch(args, factories: _Factories) -> int:
    """Point the worker at a different model provider, prove it, receipt it.

    Order of operations: overrides -> auth material check -> backup ->
    write -> one live call -> receipt. If the live call fails the previous
    settings are restored before returning, so a bad switch never leaves
    the worker pointing at a broken endpoint.
    """
    config_path = Path(args.config).expanduser()
    raw = tomllib.loads(config_path.read_text())
    model = raw.setdefault("model", {})
    if args.base_url:
        model["base_url"] = args.base_url
    if args.model_id:
        model["model_id"] = args.model_id
    if args.auth_mode:
        model["auth_mode"] = args.auth_mode
    from ghagent.web_control import toml_document
    text = toml_document(raw)
    tomllib.loads(text)  # refuse to write anything unreadable
    env = _load_env(args.env_file)
    switch_cfg = _load_lenient(str(config_path))
    switch_cfg.model = replace(switch_cfg.model,
                               base_url=model.get("base_url", switch_cfg.model.base_url),
                               model_id=model.get("model_id", switch_cfg.model.model_id),
                               auth_mode=model.get("auth_mode", switch_cfg.model.auth_mode))
    problems = config_loader.model_auth_problems(switch_cfg, env)
    if problems:
        for problem in problems:
            _err(problem)
        print("provider-switch: auth material missing; nothing was changed",
              file=sys.stderr)
        return 5
    previous = config_path.read_bytes()
    backup = config_path.with_name(
        config_path.name + ".bak-switch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    shutil.copy2(config_path, backup)
    config_path.write_text(text)
    cfg = _load_lenient(str(config_path))
    try:
        if factories.model_client:
            client = factories.model_client(replace(cfg.model, max_tokens=512), env)
        else:
            counter = str(Path(cfg.state_dir) / "model-call-counts.json") if cfg.state_dir else None
            client = _default_model_client(replace(cfg.model, max_tokens=512), env,
                                           day_counter_path=counter)
        started = time.monotonic()
        reply = client.chat([{"role": "user", "content": "Reply with OK"}])
        latency_ms = (time.monotonic() - started) * 1000
    except Exception as exc:
        config_path.write_bytes(previous)
        _err(f"the new provider failed its test call ({exc}); the previous settings were restored")
        return 1
    ok = isinstance(reply, dict)
    receipt = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "base_url": cfg.model.base_url, "model_id": cfg.model.model_id,
               "auth_mode": cfg.model.auth_mode, "latency_ms": round(latency_ms),
               "result": "PASS" if ok else "FAIL", "backup": str(backup)}
    state = Path(cfg.state_dir) if cfg.state_dir else None
    if state:
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "provider-receipts.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    print(f"switched to {cfg.model.model_id} at {cfg.model.base_url}")
    print(f"live check: {receipt['result']} in {receipt['latency_ms']} ms")
    print(f"previous settings saved beside the config: {backup.name}")
    return 0 if ok else 1


def _cmd_provider_check(args, factories: _Factories) -> int:
    cfg = _load_lenient(args.config)
    env = _load_env(args.env_file)
    problems = config_loader.model_auth_problems(cfg, env)
    if problems:
        for problem in problems:
            _err(problem)
        print("provider-check: auth material missing; no live call made",
              file=sys.stderr)
        return 5
    try:
        if factories.model_client:
            client = factories.model_client(replace(cfg.model, max_tokens=512), env)
        else:
            counter = str(Path(cfg.state_dir) / 'model-call-counts.json') if cfg.state_dir else None
            client = _default_model_client(replace(cfg.model, max_tokens=512), env, day_counter_path=counter)
    except ImportError as exc:
        _err(_sibling_missing(exc))
        return 1
    started = time.monotonic()
    try:
        reply = client.chat([{"role": "user", "content": "Reply with OK"}])
    except Exception as exc:
        _err(f"live model call failed: {exc}")
        return 1
    latency_ms = (time.monotonic() - started) * 1000
    print(f"endpoint: {cfg.model.base_url}")
    print(f"model: {cfg.model.model_id}")
    print(f"latency: {latency_ms:.0f} ms")
    content = reply.get("content") if isinstance(reply, dict) else None
    if content:
        print(f"reply: {content}")
    print("provider-check OK (one tiny live call; token never printed)")
    return 0


def _cmd_init_config(args, factories: _Factories) -> int:
    dest = args.path or DEFAULT_CONFIG_PATH
    try:
        path = config_loader.write_example_config(dest)
    except ConfigError as exc:
        _err(str(exc))
        return 1
    print(f"wrote example config: {path}")
    print("edit it: allowed_command_users, repos, state_dir, worktree_root "
          "(and [model] to change provider or spending limits)")
    return 0


# ---------------------------------------------------------------------------
# parser + entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gha-worker",
        description="Local GitHub agent worker: polls selected repos for "
                    "/agent commands and runs them one at a time.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH,
                        help=f"config path (default: {DEFAULT_CONFIG_PATH})")
        sp.add_argument("--env-file", default=DEFAULT_ENV_PATH,
                        help=f"env file (default: {DEFAULT_ENV_PATH})")

    sp = sub.add_parser("run", help="run the daemon in the foreground")
    add_common(sp)
    sp.set_defaults(func=_cmd_run)

    sp = sub.add_parser("once", help="run one job without the daemon")
    add_common(sp)
    sp.add_argument("--repo", required=True, help="OWNER/REPO")
    sp.add_argument("--kind", required=True, choices=["fix-pr", "implement-issue"])
    sp.add_argument("--number", required=True, type=int, help="PR or issue number")
    sp.add_argument("--comment-id", type=int, default=0,
                    help="commanding comment id (0 for a local submit)")
    sp.set_defaults(func=_cmd_once)

    sp = sub.add_parser("status", help="print saved state (no network)")
    add_common(sp)
    sp.set_defaults(func=_cmd_status)

    sp = sub.add_parser("stop", help="SIGTERM the running daemon")
    add_common(sp)
    sp.set_defaults(func=_cmd_stop)

    sp = sub.add_parser("provider-check",
                        help="verify config + auth, then ONE tiny live model call")
    add_common(sp)
    sp.set_defaults(func=_cmd_provider_check)

    sp = sub.add_parser("provider-switch",
                        help="point the worker at another model provider, prove it, and keep a receipt")
    sp.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH)
    sp.add_argument("--env-file", default=DEFAULT_ENV_PATH)
    sp.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint, e.g. https://api.example/v1")
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--auth-mode", default=None, choices=["broker-lease", "env"])
    sp.set_defaults(func=_cmd_provider_switch)

    sp = sub.add_parser("init-config", help="write the example config (placeholders only)")
    sp.add_argument("path", nargs="?", default=None,
                    help=f"destination (default: {DEFAULT_CONFIG_PATH})")
    sp.set_defaults(func=_cmd_init_config)

    return parser


def main(argv: list[str] | None = None, *, worker_factory: Callable | None = None,
         component_factory: Callable | None = None,
         model_client_factory: Callable | None = None) -> int:
    factories = _Factories(
        worker=worker_factory,
        component=component_factory,
        model_client=model_client_factory,
    )
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args, factories)
    except ConfigError as exc:
        if exc.missing_destination:
            print(DESTINATION_MESSAGE)
            return 2
        _err(str(exc))
        return 1
    except tomllib.TOMLDecodeError as exc:
        _err(f'config file is not valid TOML: {exc}')
        return 1
    except GitHubAuthUnavailable as exc:
        _err(str(exc))
        return 4
    except ProviderAuthMissing as exc:
        _err(str(exc))
        return 5
    except OSError as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        print("interrupted")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
