# gha-worker onboarding

Plain-English guide for a member who wants the worker running for their
repositories. Read OPERATIONS.md afterwards if you maintain it; RECORDS.md
holds the dated verification record.

## What this is

gha-worker is a local worker that runs on this machine (`inference`). It
watches a short list of GitHub repositories you select, and when an allowed
person leaves a comment beginning `/agent` on an issue or pull request there,
it does the work locally: it builds a git worktree, runs a coding agent
powered by the company model, runs the repo's checks, and answers in a
GitHub comment.

**Machine dependency, stated plainly:** this worker runs only while this
machine and its service run. It is a program on `inference`, started by you
or by systemd — a conversation with an assistant alone never executes it. If
the machine is off, no tasks run and nothing is queued remotely.

**Machine facts (verified 2026-10-17):** Ubuntu on host `inference`; gh
2.101.0 at `/usr/local/bin/gh`; git 2.53.0; Python 3.14.7.
The project root is `/opt/fatchin` (source under
`src/ghagent`). The model is `your-model`, a reasoning model with
tool-calling verified live on 2026-10-17, reached through the company
secret-broker proxy at `http://127.0.0.1:8080/v1`
(OpenAI-compatible).

## Where tasks come from

A task is a comment on an issue or pull request in one of the repos listed in
`config.toml`, written by a user whose numeric GitHub ID is in
`allowed_command_users`. The comment must start with the command prefix
(default `/agent`):

| Comment                  | Effect                                                       |
| ------------------------ | ------------------------------------------------------------ |
| `/agent fix`             | Fix the review findings on that pull request.                |
| `/agent implement`       | Implement the issue in a new branch off the configured base. |
| `/agent status`          | Reply immediately with queue status (no model call).         |
| `/agent stop`            | Stop the running job for that issue/PR; no further writes.   |

Example of a real request — comment on PR #42 in a selected repo:

    /agent fix

or with an explicit number from anywhere in the repo:

    /agent fix #42

The worker ignores: its own comments, comments by users not on
`allowed_command_users`, and every comment that is not a command. The first
poll of a brand-new repo only records a position (watermark) and never
replays old comments as commands.

## First-time setup (owner actions)

1. **GitHub authentication — the one step still missing on this machine
   (as of 2026-10-17 there is no GitHub.com credential here yet).** Run:

       gh auth login --hostname github.com --git-protocol https --web

   Alternative: put a token in the protected env file as `GH_TOKEN`.
   The worker refuses to start polling when `gh auth status -h github.com`
   reports no authentication — it will not poll, and it never falls back to
   any GitHub-side agent service (no Copilot agent tasks, no
   `gh agent-task create`, no Actions runs). All work happens here, locally.

2. **Model access.** Credentials flow as short-lived broker leases
   (consumer `fatchin`, server `your-provider`) minted with
   `MCP_SECRET_BROKER_ADMIN_TOKEN` from the worker env file
   (`~/.config/gha-worker/env`, permission 600). Fill that placeholder after
   install; the token is never logged or printed.

3. **Install** (from the repo's `deploy/` directory):

       ./install.sh

   It writes the env template (only if absent), installs and enables the
   systemd user unit, and prints the exact start/stop/log commands. It does
   not start anything.

4. **Create your folders and config.** Create a state folder and a worktree
   folder inside your own account space, then:

       python3 -m ghagent.cli init-config ~/.config/gha-worker/config.toml
       nano ~/.config/gha-worker/config.toml

   Set `allowed_command_users` (find yours with `gh api user --jq .id`),
   your `[[repos]]`, and point `state_dir` and `worktree_root` at the folders
   you created. A command without a destination folder in your own space
   refuses to run — that is company law, not a bug.

## Everyday commands

    python3 -m ghagent.cli provider-check        # one tiny live model call; prints endpoint, model, latency — never the token
    systemctl --user start gha-worker            # start the daemon (deliberate)
    systemctl --user status gha-worker           # is it running
    python3 -m ghagent.cli status                # saved state: health, watermarks, jobs (no network)
    journalctl --user -u gha-worker -f           # live logs
    python3 -m ghagent.cli stop                  # graceful stop (SIGTERM)
    systemctl --user stop gha-worker             # same via systemd

Run a single task by hand, without the daemon (same persistence):

    python3 -m ghagent.cli once --repo OWNER/REPO --kind implement-issue --number 17
    python3 -m ghagent.cli once --repo OWNER/REPO --kind fix-pr --number 42

## Where results land

- **State (local):** `state_dir/jobs.json` — every job with its id, state,
  attempts, commits produced, and errors; `state_dir/watermarks.json` — poll
  positions; `state_dir/health.json` — liveness written every cycle.
- **GitHub:** a branch (and pull request when the task produces one), plus a
  worker comment on the commanding issue/PR. Every worker comment carries a
  hidden marker (`<!-- gha-worker:v1:`) so the worker never re-reads its own
  output as a command.

## Changing model or spending limit

Edit `[model]` in `~/.config/gha-worker/config.toml` and restart the
service:

- different provider: change `base_url` and `model_id`
  (`auth_mode = "env"` reads `GHA_MODEL_API_KEY` from the env file instead of
  minting broker leases);
- spend ceilings: `max_calls_per_job` (default 60) and `max_calls_per_day`
  (default 600, UTC calendar day).

Restart afterwards so the change takes effect:

    systemctl --user restart gha-worker
