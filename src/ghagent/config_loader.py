"""Config loading, env-file parsing, and environment validation for gha-worker.

Company output contract: a tool that would write local artifacts but was given
no destination inside the member's own space must refuse. load_config marks a
config that lacks state_dir/worktree_root as "missing destination" so the CLI
can print the exact legal message and exit 2.

Env files are parsed without ever printing values: error messages carry line
numbers only, never file contents.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from . import interfaces
from .interfaces import Config

DESTINATION_KEYS = ("state_dir", "worktree_root")

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(Exception):
    """A problem with the config or env file. Safe to print: no values."""

    def __init__(self, message: str, *, missing_destination: bool = False):
        super().__init__(message)
        self.missing_destination = missing_destination


def missing_config_keys(cfg: Config) -> list[str]:
    """Top-level keys that must be set for the worker to have a destination."""
    missing = []
    if not str(cfg.state_dir).strip():
        missing.append("state_dir")
    if not str(cfg.worktree_root).strip():
        missing.append("worktree_root")
    return missing


def load_config(path: str | Path) -> Config:
    """Load a config file, raising ConfigError with friendly missing-key lists."""
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(
            f"config file not found: {p} "
            "(create one with: python3 -m ghagent.cli init-config)"
        )
    try:
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file is not valid TOML: {p}: {exc}") from None
    try:
        cfg = interfaces.Config.load(p)
    except TypeError as exc:
        raise ConfigError(
            f"config file has a field the worker does not recognize ({exc}); "
            "check the keys against deploy/config.example.toml"
        ) from None
    except ValueError as exc:
        missing = [k for k in DESTINATION_KEYS if not str(raw.get(k, "")).strip()]
        if not missing:
            missing = list(DESTINATION_KEYS)
        raise ConfigError(
            "config is missing required keys: " + ", ".join(missing)
            + f" ({exc})",
            missing_destination=True,
        ) from None
    missing = missing_config_keys(cfg)
    if missing:
        raise ConfigError(
            "config is missing required keys: " + ", ".join(missing),
            missing_destination=True,
        )
    return cfg


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse KEY=VALUE lines into a dict. Never prints or logs values."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(
        p.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{p}: line {lineno}: not a KEY=VALUE line")
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not _KEY_RE.fullmatch(key):
            raise ConfigError(f"{p}: line {lineno}: invalid variable name")
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


def model_auth_problems(cfg: Config, env: dict[str, str]) -> list[str]:
    """Problems with the model auth material only (naming the missing variable)."""
    mode = cfg.model.auth_mode
    if mode == "broker-lease":
        if not (env.get("MCP_SECRET_BROKER_ADMIN_TOKEN") or "").strip():
            return [
                "MCP_SECRET_BROKER_ADMIN_TOKEN is not set "
                "(required by model auth_mode 'broker-lease'); add it to the "
                "worker env file"
            ]
    elif mode == "env":
        if not (env.get("GHA_MODEL_API_KEY") or "").strip():
            return [
                "GHA_MODEL_API_KEY is not set "
                "(required by model auth_mode 'env'); add it to the worker env file"
            ]
    else:
        return [f"model auth_mode '{mode}' is not one of: broker-lease, env"]
    return []


def validate_environment(cfg: Config, env: dict[str, str]) -> list[str]:
    """Human-readable list of configuration problems; empty means usable."""
    problems: list[str] = []
    if not str(cfg.state_dir).strip():
        problems.append("state_dir is not set — the worker has nowhere to keep state")
    if not str(cfg.worktree_root).strip():
        problems.append("worktree_root is not set — the worker has nowhere to create worktrees")
    if not cfg.allowed_command_users:
        problems.append(
            "allowed_command_users is empty — no GitHub user would be allowed to command the worker"
        )
    if not cfg.repos:
        problems.append("repos is empty — the worker would watch no repositories")
    problems.extend(model_auth_problems(cfg, env))
    return problems


EXAMPLE_CONFIG_TOML = '''\
# gha-worker configuration (example).
# Copy to ~/.config/gha-worker/config.toml and edit every placeholder.
# Placeholders only — never put secrets in this file. Tokens live in the
# worker env file (~/.config/gha-worker/env, permission 600).

github_host = "github.com"          # GitHub host the worker talks to
rest_api_version = "2022-11-28"     # GitHub REST API version header sent with calls
poll_interval_seconds = 120         # seconds between polls of the selected repos
command_prefix = "/agent"           # a comment must start with this to be a command

# GitHub numeric user IDs allowed to command the worker. Replace the
# placeholder with your own numeric ID (find it with:
# gh api user --jq .id).
allowed_command_users = [1]

# Where the worker keeps its state (jobs.json, watermarks.json, health.json,
# worker.lock). MUST be a folder you created inside your own account space —
# create the folder first, then put its path here.
state_dir = "/home/YOURUSER/.local/state/gha-worker"

# Where per-job git worktrees are created. MUST also be inside your own space.
worktree_root = "/home/YOURUSER/work/gha-worker-worktrees"

max_tool_iterations = 40
run_command_timeout_seconds = 600
run_command_output_cap = 100000
poll_repos = true                   # false = stop watching repos; queued jobs still drain

# Repos the worker watches. Only comments inside these repos are read.
[[repos]]
owner = "example-owner"             # placeholder — replace
name = "example-repo"               # placeholder — replace
base_branch = "main"                # authorized base for issue implementations
checks = ["python3 -m unittest discover -s tests"]  # shell checks run in the job worktree
# allow_unknown_rulesets = true     # set ONLY for private free-plan repos where
#                                   # the rulesets API answers HTTP 403 (plan limit);
#                                   # records the ruleset check as unverified instead
#                                   # of holding every write. Leave off otherwise.

[model]
# Any OpenAI-compatible chat-completions endpoint with tool calling.
base_url = "http://127.0.0.1:8080/v1"
model_id = "your-model"             # must support tool calling
auth_mode = "env"                   # "env" (GHA_MODEL_API_KEY) or "broker-lease"
broker_base_url = "http://127.0.0.1:8080"
broker_consumer_id = "fatchin"
broker_server_id = "your-provider"
lease_ttl_seconds = 900             # short-lived broker lease lifetime
request_timeout_seconds = 180
max_tokens = 8192
max_calls_per_job = 60              # spend ceiling: model calls per job
max_calls_per_day = 600             # spend ceiling: model calls per UTC day

# How to switch provider:
#   - different endpoint: edit base_url and model_id above.
#   - auth_mode "env": the worker reads GHA_MODEL_API_KEY from the env file and
#     never talks to a broker.
#   - auth_mode "broker-lease": optional; mint short-lived leases from a broker
#     you run. It needs MCP_SECRET_BROKER_ADMIN_TOKEN in the env file.

# Account-wide fleet policy. Personal repositories default on; organisation
# repositories default off until named. Include/exclude always wins.
[fleet_policy]
personal_default = "include"
org_default = "exclude"
# include = ["YourOrg/important-repository"]
# exclude = ["YourUser/abandoned-experiment"]
'''


def write_example_config(dest: str | Path) -> Path:
    """Write the placeholder example config; refuses to overwrite."""
    p = Path(dest).expanduser()
    if p.exists():
        raise ConfigError(f"refusing to overwrite existing file: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    return p
