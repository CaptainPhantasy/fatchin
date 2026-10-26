# Troubleshooting

Start with the current fleet guidance below. The later terminal entries retain
the individual-worker failure modes recorded on 2026-10-17; their date does
not imply the fleet existed then. Exit codes are stable: `0` ok, `1` general, `2` no
destination folder, `3` already running, `4` no GitHub auth, `5` no model
auth.

## Fleet: find the problem in the same window

| What you see | What to do |
| --- | --- |
| Only a few projects in a list | Search the fleet catalog in Repositories or Review watches. Saved individual-task configurations and manual watches are separate smaller lists. Check fleet scope and whether discovery is incomplete. |
| Only one provider in ordinary settings | That section configures individual tasks. The shared fleet panel and Agent fleet show both GLM and DeepSeek, with active and configured counts. An unavailable provider is reported, never silently substituted. |
| Fewer than twenty busy agents | Check eligible work, holds and cooldowns. Capacity is ten per pool and four per repository, across all task types. Unchanged file versions are not repeatedly reviewed just to fill slots. |
| An agent needs recovery | Exits and stalls recover automatically with a fresh worker. Check Activity for its outcome; do not clear state or reset the entire fleet. |
| No project check was recognized | Choose Set verification command on the item, use that project's documented test/build command, then Save and continue. Waiting work is reconsidered automatically. No worker restart is needed. |
| A check failed | Expand Verification failed — read the command output. Address the actual failure before Retry this item. A configured check is not a passing check. |
| A rate limit or temporary service error | Affected work cools down and retries automatically. Do not repeatedly dispatch a new fleet. |
| Comments exist but the PR is not ready | Follow the source, correction and review links in Open comments. Required external approval, failed checks, conflicts or unresolved feedback can prevent readiness. The same GitHub account cannot approve its own PR through a different agent. |
| Writes are held | Read the recorded automation, permissions or rules reason. Existing expense checks remain active. Retrying does not waive them. |
| Closing the browser did not stop work | The fleet runs on the server. Use Pause after current tasks, Stop now or Resume fleet in Agent fleet or Settings. A write already admitted or sent can finish. |
| The folder picker already has a destination | Reuse the saved choice. The saved folder is /home/youruser/Documents/FatChin. Other members choose their own account folder; scratch checkouts are separate. |

For the full process and controls, see [the workflow](WORKFLOW.md) and
[fleet guide](AGENT-FLEET.md). New remote work is discovered periodically;
completed findings and published corrections hand off immediately without
waiting for a full account scan. Repairs update the same correction branch,
with a person required after three automatic rounds.

---

## Startup refusals (by design)

### `Error: No destination folder specified. Supply an output path within your user space.` (exit 2)

`state_dir` / `worktree_root` are missing from the config. Create the
directories inside your own home and set both keys.

### `GitHub authentication unavailable` (exit 4)

`gh auth login --hostname github.com --git-protocol https --web`, then retry.
Check with `gh auth status`.

### `MCP_SECRET_BROKER_ADMIN_TOKEN is not set` / `GHA_MODEL_API_KEY is not set` (exit 5)

The model credential is missing from `~/.config/gha-worker/env` (mode 600).
The worker names the variable it wants and **never falls back** to any other
provider — that refusal is the point.

### `another gha-worker already holds <state_dir>/worker.lock` (exit 3)

One daemon is already running. `gha-worker status` shows its pid;
`gha-worker stop` stops it. If a stale lock survives a hard kill, remove the
`worker.lock` file — the lock is advisory, state data is not affected.

## Remote-write guards (the job fails rather than spending money)

### `trigger preflight held all writes: could not inspect repo rulesets (… HTTP 403 …)`

GitHub's rulesets API is plan-limited: on **private repos of free accounts**
it answers 403 "Upgrade to GitHub Pro…". That is a plan limit, not a danger
signal. If you accept an unverified ruleset check for this one repository,
set in its `[[repos]]` block:

```toml
allow_unknown_rulesets = true
```

The report then records the ruleset check as *unverified (owner allowlisted)*
and proceeds. Every other unknown — repository metadata, workflow files,
`.gitattributes` — still holds all writes.

### `Copilot` / AI review runs appearing on the worker's PRs

On 2026-10-17, opening a demo PR started a **"Running Copilot Code Review"
run (event `dynamic`)** from the account-level automatic-review setting, plus
an installed `chatgpt-codex-connector` app auto-reviewed. Repo-side config
was clean — this trigger is invisible to repository APIs, so the no-charge
guard cannot pre-detect it. What to do:

```bash
gh run list -R OWNER/REPO --json databaseId,name,status
gh run cancel <run-id> -R OWNER/REPO        # stops the chargeable review
```

Then disable automatic review if you want it gone for good:
**github.com/settings/copilot → Automatic code review → off**, and/or
uninstall the review app from the repository (Settings → GitHub Apps). The
worker itself starts no runs; this only happens when account features react
to *any* new PR.

### `verification failed: …` and the job stops before pushing

At least one configured repo check exited non-zero. The failure output (last
4000 chars per check) is in the job record: `gha-worker status`, or
`python3 -c "import json;print(json.load(open('<state_dir>/jobs.json')))"`.
Fix the cause (or fix the check command) and re-run — a failed job's dedup
key is retryable, a *successful* one is not.

## Git behavior

### `fatal: '<branch>' is already used by worktree at '<path>'`

A leftover worktree held the base branch (fixed in the current code — job
worktrees are created detached and cleaned up on failure). If you still have
an old state directory from a previous version:

```bash
git worktree list --work-tree <repo>     # or inspect <worktree_root>/
git worktree remove --force <path>
git worktree prune
```

### `remote moved; re-run to rebase`

Someone else pushed to the PR branch between the worker's read and write.
The worker deliberately refuses to overwrite (it never force-pushes). Re-run
the job; it starts from the new head.

## Commands that "do nothing"

### `/agent status` gets no reply on the daemon's first poll

The **first** poll of a repository records a baseline watermark and
deliberately does not treat older comments as commands (this prevents
replaying the whole history on first connect). Post the command again after
the daemon has completed one poll cycle (~2 minutes at the default interval).

### `an identical job already exists (dedup_key …); nothing to do`

You asked for exactly the same work (same PR head / same commanding comment)
that already succeeded. That is the exactly-once guarantee working. If you
want a re-run, push a new commit or post a fresh `/agent implement` comment
(issue revision changes). Failed and stopped jobs are always retryable.

### The daemon replies to every comment except the owner's commands — or only to them

Own-output detection is **marker-based**: every worker comment carries the
hidden `<!-- gha-worker:v1:… -->` marker and is ignored on later polls. In a
single-account setup (owner and worker share one GitHub identity) commands
from that account are valid commands — this was fixed on 2026-10-17 (an
earlier version dropped any comment authored by the worker's own account).
Update to the current code.

## Model errors

### `ModelError: … 401/403` on the proxy

The lease token expired or was revoked mid-job, or the broker lost its
registry read. The worker retries bounded and then fails the job — it never
switches providers. Check broker health (`curl -s
http://127.0.0.1:8080/health`) and re-run.

### `SpendLimitExceeded`

You hit `max_calls_per_job` or `max_calls_per_day`. Raise the ceiling in the
config if the work genuinely needs it — the counters live in
`<state_dir>/day_counter.json` (per-UTC-day, resets itself).

### Empty model replies, `finish: length`

your-model is a reasoning model: it spends tokens thinking before
answering. Raise `max_tokens` in `[model]` (default 8192) rather than
lowering the task complexity.

---

Still stuck? `gha-worker status`, `journalctl --user -u gha-worker`, and
`<state_dir>/health.json` between them always say what the worker last did;
the job records carry every error verbatim.
