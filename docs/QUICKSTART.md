# Quickstart — cold start to your first worker pull request

Already using the installed FatChin workspace? Start with
[the fleet walkthrough](WORKFLOW.md#start-here). It covers both model pools,
all accessible writable repositories, saved reports folders and the review-to-merge
cycle. No installation or per-repository setup is needed to resume that fleet.

The steps below install the original individual-task worker on a new machine.
Its `[[repos]]` blocks select individual-task polling scope; they do not limit
the account-wide fleet's discovered repository catalog.

The commands below are the installation sequence. Total time from nothing to
a working daemon: about 10 minutes.

You need: a Linux machine with internet access, Python 3.11+, git, and the
`gh` CLI (≥ 2.x). No pip packages — the worker is standard-library only.

---

## 1. Authenticate the GitHub CLI (one-time)

```bash
gh auth login --hostname github.com --git-protocol https --web
gh auth status --hostname github.com
```

Expected: `✓ Logged in to github.com account <you>`. Get your numeric user ID
— the worker authorizes commands by ID, never by name:

```bash
gh api user --jq '.id'
```

Write that number down; you will paste it into the config in step 3.

> **Cost check:** authenticating costs nothing. The worker never creates
> Actions runs, never buys runners, never installs Marketplace apps, and the
> no-charge guard inspects the target repository before any write.

## 2. Get the code

```bash
git clone https://github.com/example-owner/gha-worker.git
cd gha-worker
python3 -m unittest discover -s tests -t .       # optional sanity: 289 tests, ~10 s
```

## 3. Configure

```bash
deploy/install.sh
```

`install.sh` writes two files **without overwriting existing ones**, then
installs (but does not start) the systemd user unit:

- `~/.config/gha-worker/config.toml` — the worker configuration
- `~/.config/gha-worker/env` (mode 600) — secrets; commit this to no one

Edit the config — the four values that **must** change are marked in the
file:

```toml
allowed_command_users = [<your numeric ID from step 1>]
state_dir     = "/home/<you>/.local/state/gha-worker/state"
worktree_root = "/home/<you>/.local/state/gha-worker/worktrees"
# and the [[repos]] block: repositories for individual-task polling
[[repos]]
owner = "you"
name  = "your-demo-repo"
base_branch = "main"
checks = ["python3 -m unittest discover -s tests -t ."]
```

Create those two directories first (`mkdir -p`). They must live inside your
own account space — without a valid destination the CLI refuses to start with
`Error: No destination folder specified. Supply an output path within your
user space.`

Edit the env file and set the model credential. This installation uses the
company secret-broker, so the value is its admin token (ask the server owner
or read it from the broker host's environment — it is never committed
anywhere):

```
MCP_SECRET_BROKER_ADMIN_TOKEN=<token>
chmod 600 ~/.config/gha-worker/env
```

**Using a plain provider key instead?** Skip the broker: set
`auth_mode = "env"` in the `[model]` section and put `GHA_MODEL_API_KEY=<key>`
in the env file.

## 4. Prove the model path (one tiny live call)

```bash
cd gha-worker
PYTHONPATH=src python3 -m ghagent.cli provider-check
```


Exit code 0 and a `reply: OK` mean endpoint, credential, and model all work.
Failure modes are listed in [Troubleshooting](TROUBLESHOOTING.md).

## 5. Give the worker something to do

The worker only watches the repositories in your config, and only accepts
commands from `allowed_command_users`. Two ways to submit work:

**A. One-shot (no daemon)** — on an issue asking for a small fix:

```bash
PYTHONPATH=src python3 -m ghagent.cli once \
  --repo you/your-demo-repo --kind implement-issue --number 1
```


**B. Comment commands (daemon)** — comment exactly one of these on an issue
or PR in a watched repo:

```
/agent implement     ← implement an issue, return a PR
/agent fix           ← address all actionable review feedback on a PR
/agent status        ← post the saved state of the relevant job
/agent stop          ← stop that job; no further remote writes
```

The daemon picks them up on its next poll (default every 120 s):

```bash
PYTHONPATH=src python3 -m ghagent.cli run        # foreground
systemctl --user start gha-worker                # or as the installed service
journalctl --user -u gha-worker -f               # watch it work
```

## 6. See the result

The worker pushes a branch (never force), opens a pull request with an
explicit base branch, and posts a summary comment containing its hidden
marker, the tested commit, check results, and the model-call count:


When you leave review comments, `/agent fix` addresses every actionable
thread — and resolves a thread **only** when the fix is on the PR, the
checks pass, and no newer comment exists:


`/agent status` is answered from the durable job store — failures included,
honestly:


## 7. Stop / restart / remove

```bash
systemctl --user stop gha-worker      # graceful: finishes the step, writes
                                      # health.json state=stopped, exits
systemctl --user start gha-worker     # restart; saved state reconciles with
                                      # GitHub — nothing is redone
deploy/uninstall.sh                   # removes the unit; keeps config+state
```


---

## Switching model or provider

Edit `[model]` in `~/.config/gha-worker/config.toml`:

| To | Change |
|---|---|
| different model, same broker | `model_id = "your-modelx"` (or any id the endpoint serves) |
| any OpenAI-compatible provider | `base_url = "https://host/v4"`, `auth_mode = "env"`, `GHA_MODEL_API_KEY=<key>` in the env file |
| spending limits | `max_calls_per_job` / `max_calls_per_day` — enforced before each request |

Then `provider-check` again. The worker has **no fallback provider**: if the
configured endpoint or credential is missing, it refuses and names what is
missing — it will never silently route anywhere else.

---

## 8. The button: "Fix with gha-worker" next to Copilot's

GitHub doesn't let anyone else add buttons to its web UI — so the button
ships as a tiny browser userscript instead: it draws a small panel on every
issue/PR page with **Fix / Implement with gha-worker**, Status and Stop.
Clicking posts the command comment; your worker (polling on this server)
does the rest. $0 — a comment is free.

Install (one-time, in the browser you use GitHub with):

1. Install the **Tampermonkey** extension (Chrome/Edge/Firefox).
2. Open the Tampermonkey dashboard → **Utilities → Import** — or just create
   a new script and paste
   [`deploy/github-button/gha-worker-button.user.js`](../deploy/github-button/gha-worker-button.user.js).
3. Open any issue/PR. First click asks for a GitHub token — create a
   **fine-grained** personal access token with **Issues: read and write**
   (only) for the repos you command. It is stored in your browser only and
   used solely to post your command comments.

No browser? The CLI has the same buttons:

```bash
gh fix 2        # = post "/agent fix" on PR 2 of the configured repo
gh implement 1  # = post "/agent implement" on issue 1
gh wstatus 1    # = post "/agent status"
```
