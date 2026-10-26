# gha-worker operations

This is the individual-worker terminal reference. For the current FatChin
fleet, use [GUI operations](GUI-OPERATIONS.md#persistent-state-and-recovery)
and [the fleet guide](AGENT-FLEET.md). Fleet scope is separate from configured
individual-task projects. Do not clear fleet state or restart the shared
service to recover one agent; use automatic recovery and the item's controls.

For whoever keeps the worker healthy. Dated verification facts live in
RECORDS.md; first-time setup lives in ONBOARDING.md.

## Health checks (exact commands)

    systemctl --user status gha-worker
    journalctl --user -u gha-worker -f          # -n 200 for recent history
    cat ~/.local/state/gha-worker/health.json   # path = state_dir in config.toml

`health.json` is rewritten atomically (temp file + rename) every cycle:

    {
      "last_poll_iso": "2026-10-17T10:53:00+00:00",
      "jobs_open": 0,
      "jobs_done_today": 2,
      "model_calls_today": 37,
      "pid": 12345,
      "state": "running"
    }

`state` is `"running"` while the loop lives and `"stopped"` after a clean
exit — a `running` entry with a dead pid means the daemon died uncleanly;
check the journal. Per-repo poll positions are in `watermarks.json`.

`python3 -m ghagent.cli status` prints all saved state (health, watermarks,
jobs) without any network access — usable even when GitHub is unreachable.

## State layout

Everything lives under `state_dir` (inside the member's own space):

| File                | Meaning                                                  |
| ------------------- | -------------------------------------------------------- |
| `jobs.json`         | every job: id, kind, state, attempts, commits, errors    |
| `watermarks.json`   | per-repo `since=` position (`{repo: comments_since_iso}`)|
| `health.json`       | liveness, written atomically each cycle                  |
| `worker.lock`       | flock held for the daemon's lifetime (single instance)   |
| `worker.pid`        | pid record written at startup                            |
| `comments-fallback/`| worker answers that could not be written to GitHub       |

Job worktrees are created under `worktree_root`, one directory per job.

## Ports and network

The worker listens on **no ports** — nothing to collide, nothing to probe.
Outbound only: HTTPS to github.com via `gh`, and HTTP to the company
secret-broker proxy on `127.0.0.1:8080` (loopback).

## Credential / lease lifecycle

The worker never holds a long-lived model key. For each model-using job the
model client module mints a short-lived lease from the secret broker
(consumer `fatchin`, server `your-provider`, TTL `lease_ttl_seconds`, default
900 s), renews it if a job outlives it, and revokes it when the job ends.
The broker admin token (`MCP_SECRET_BROKER_ADMIN_TOKEN`) sits only in the
env file (mode 600) and is only used to mint leases. The worker code never
logs or prints token material. Verify the whole path any time with:

    python3 -m ghagent.cli provider-check

It makes exactly one tiny live call ("Reply with OK", max_tokens 512) and
prints endpoint, model id, and latency — never the token. It is the only
command that may talk to the model.

## Failure recovery

- **Graceful stop:** `python3 -m ghagent.cli stop` (SIGTERM) or
  `systemctl --user stop gha-worker`. The signal handler lets the current
  job step finish, persists state, and exits 0.
- **Crash / kill -9:** safe by design — the lock is released by the OS, the
  queue lives on disk, and on the next start
  `queue.reconcile_with_remote()` compares saved jobs with GitHub and heals
  the difference. Restart with `systemctl --user start gha-worker`.
- **Stuck job:** comment `/agent stop` on the issue/PR, or stop the daemon.
  Jobs run strictly one at a time (one writer per PR by construction), so a
  stuck job delays the queue but cannot interleave with another.
- **Loop errors never kill the daemon:** every polling/draining error is
  logged, backed off progressively (doubling, capped at 15 minutes), and
  retried next cycle; a `Retry-After` from rate limiting is honored. A clean
  cycle resets the backoff.
- **`/agent fix` on an issue:** refused — the worker logs "not a pull
  request" and moves on.
- **Remote write failure:** worker comments that cannot be posted land in
  `state_dir/comments-fallback/` instead of being lost.

## Rollback / uninstall

    deploy/uninstall.sh     # stops + disables the unit, removes the unit copy

It deliberately keeps config, env file, and state, and prints their paths
plus the manual removal commands. The unit copy lives at
`~/.config/systemd/user/gha-worker.service`; previous unit versions are
backed up beside it with dated names by `install.sh`.

## Upgrading notes

- GitHub REST API version in use: `2022-11-28` (`rest_api_version` in
  config.toml). Check
  <https://docs.github.com/en/rest/about-the-rest-api/api-versions> before
  changing it.
- After any change to the unit file: `systemctl --user daemon-reload`.
- `install.sh` is idempotent: re-running it refreshes the unit (with a dated
  backup of the previous copy) and never touches an existing env file.
