# FatChin capabilities and boundaries

This map explains what the interface exposes and what each action means.
It is a feature reference, not a statement that every workflow has been tested
against live external services. Verification is recorded separately in
[GUI-ACCEPTANCE.md](GUI-ACCEPTANCE.md).

## Capabilities

| Area | Available workflow | Effect and boundary |
| --- | --- | --- |
| Agent fleet | Up to ten GLM and ten DeepSeek assignments across all accessible writable repositories, or a selected set | At most four active agents per repository across both pools and all task types, including source forks. Each task gets a fresh worker and conversation. Fewer eligible tasks means fewer busy slots. |
| Fleet review cycle | Review → specific finding → isolated fix with checks → another fresh agent's review → current merge-readiness check | Findings and published corrections hand off immediately. Further fixes use the same correction branch; after three automatic repair rounds a person is needed. The user merges and deletes branches on GitHub. |
| Fleet windows | Agents & queue, Open comments, Ready to merge and Activity | Readiness requires the current reviewed version and merge conditions. All agents share one GitHub identity; they cannot provide another person's required approval. |
| Fleet recovery | Automatic replacement after exits or stalls, cooldowns, per-item check configuration and retry | Save and continue reconsiders held work without an agent reset. Conflicts, uncertain writes, failing checks and automation expense holds remain visible. |
| Shared fleet status | Both pools and discovered project coverage appear throughout the workspace | Repositories and Review watches show the full searchable fleet catalog, separately from individual-task configuration and manual watches. Tasks links recent assignments to their evidence. Settings has fleet controls. |
| Explore & learn | Inspect real repository files/history/comparisons, build official and public third-party view links, copy client-specific MCP setup, learn Git in two paced modes | External sites open separately; private repository links stay internal/official. Practice is isolated; narrated actions wait for the user. See [the learning guide](EXPLORE-AND-LEARN.md). |
| Overview | Read worker status, recent tasks, configured repositories, and usage | Saved health is distinguished from live connection checks. Opening the page does not submit coding work. |
| Task creation | Select a configured repository and one of the twelve task kinds, with an issue, pull request, or short answer where that kind needs one | Issue implementation and PR fixes work on their own; the other ten kinds produce a report or a draft. The full list with each boundary is in [Task types](#task-types). |
| Task review | Inspect the target, task body, destination, checks, and limits before submission | The final action submits real work; it is not a demo or a dry run. |
| Drafts and decisions | Read an individual task's finished draft, then choose Post, Edit, or Discard | Discard keeps the task in history and posts nothing. Branch updates, new projects and merge cleanup require separate explicit confirmation, regardless of automatic posting permission. Fleet dispatch separately authorizes its automatic comments and corrections. |
| Posting permission | Read the permission for each repository and switch it between asking first and posting directly | The default is asking every time. Each non-default permission is listed as a grant and can be reversed. Ordinary draft tasks may post automatically when this choice allows it. Learning-origin drafts always ask first. |
| Updates & decisions | Read three piles: Done, Waiting on you, and Watched | A view of saved events and waiting drafts, not a second queue. An empty pile means nothing has happened; a read failure is shown as an error. |
| Checks before publishing | Read per-repository checks, armed watches, posting permission, rules readability, and today's counts | Describes the current configuration. It cannot see account-level automation or your provider's invoice. The worker's own Actions-run count is always zero. |
| Destinations | Select an account, browse its server folders, and explicitly create a folder | Output must stay within that account's home. A browser's local disk is not the server filesystem. |
| Task history | Filter and inspect saved tasks and their results | Read failures remain errors; empty results are not substituted for unreadable state. |
| Task details | Read phases, errors, model call counts, commits, check evidence, changed files, and reports where present | A completed job is not proof that every finding was fixed. Read its recorded evidence. |
| Result files | Browse exported artifacts, preview text, or download a file | Up to 500 entries per folder, 100,000 bytes per preview, and 50,000,000 bytes per download. Binary download is supported; credential/hidden/outside-task paths remain blocked. |
| Recovery | Stop open work and retry eligible failed or stopped work | A stop is cooperative. A retry can use model calls and perform new writes. Completed remote changes remain. |
| Repositories | Search GitHub, add, edit, or remove a configured repository | Each repository has its own base branch and check commands. Connected GitHub access is still required. |
| Branches and checks | Choose an actual branch; select or edit common check presets | Presets do not install tools or prove that the repository supports that command. |
| PR monitoring | Watch one PR or all current and future open PRs in a configured repository | New inline review comments can authorize fix jobs. This is active coding automation, not only an alert. |
| Successful-task follow-up | A successful fix or created PR becomes a selected watch automatically | The task's final review discloses this ongoing effect. Remove it in Review watches when no longer wanted. |
| Repository protection on add | Adding a configured repository arms a review watch for its open pull requests | Automatic follow-up can start from day one. Checks before publishing shows it as Armed; remove the watch to stop it. Removing a repository stops polling it, and its saved watch becomes dormant rather than being deleted. |
| Monitor management | Inspect or remove a watch; pause polling for all discovery | Removing a watch does not undo previous jobs or stop an already queued job. Stopping one task does not remove its watch. |
| GitHub connection | Start device authorization and check the connected identity | The identity is used for external GitHub operations. No separate workspace login is added. |
| Existing GitHub token compatibility | Honor a token already supplied in the worker's environment | No GitHub token editor is added. A token override must be removed by the maintainer before switching to device authorization. |
| Provider configuration | Configure compatible endpoint/model and broker or direct-key authentication | Switching provider is explicit; the worker does not fall back to another service on failure. |
| Provider switching | `provider-switch` backs up the configuration, writes the new endpoint, proves it with one small live call, and writes a PASS/FAIL receipt | The previous settings are restored when that call fails. Success proves a short message works, not that every coding task will. |
| Provider reasoning mode | Use provider default or disable separate reasoning output | The disabled option sends a provider-specific parameter; use it only with a provider that supports it. |
| Credentials | Save a model or broker credential without reading its value back | Tokens are kept out of ordinary responses and reports. The metadata identifies its purpose and supplier. |
| Limits | Set task/day call limits, iteration limit, timeouts, and command output cap | Local worker controls, not provider subscription quotas or prices. Daily reset uses UTC. |
| Polling | Choose polling interval and whether to discover new commands/reviews | The interval is not a guaranteed reaction time; work, backoff, and rate limits can delay polling. |
| Command users | Look up a username, add the connected account, or edit permanent GitHub IDs in advanced settings | This controls whose `/agent` comments are accepted, not access to the mesh GUI. |
| Diagnostics | Inspect configuration, service, GitHub, saved-state, and connection problems | Passive checks do not request a model completion. A provider test is a separate explicit action. |
| Provider test | Confirm and request a small live model check | At most one model completion HTTP attempt; it uses the selected subscription/provider billing terms. Success tests the model path, not the entire coding workflow. |
| Worker controls | Start, stop, or restart the identified worker | These controls affect the shared worker. Settings that require a restart must be applied while idle. |
| Copilot run handling | After its own writes, attempt to stop active Copilot review runs on this job's produced commits | Existing worker behavior; cancellation outcomes/errors are recorded. It does not cancel unrelated commits' runs or guarantee no prior charge. |
| Help | Read novice workflows and maintenance guidance | Documentation describes actual boundaries and links to a separate dated verification record. |

## Task types

Twelve kinds are offered. The third column names what happens before anything
reaches GitHub, and the fourth is the boundary that stays true afterwards.

| Task type | Needs | Before anything reaches GitHub | Boundary |
| --- | --- | --- | --- |
| Back up the repository | Repository | Nothing; it reads only | Writes one dated bundle file into your result folder and checks it. Nothing on GitHub changes, and the bundle is not a substitute for a hosted copy. |
| Bring a branch up to date | Repository, source branch, target branch | The plan, Post, then explicit merge confirmation, even with Post directly enabled | One real merge commit on the target branch. A conflict aborts the merge and stops for a person; the push is never forced. |
| Check for everyday problems | Repository | Nothing; it reads only | A report of secrets, missing license or readme, outdated dependency pins, stale branches, and leftover debug files, each with file, line, and a suggested fix. It never edits the repository. |
| Explain a repository | Repository, optional question | Nothing; it reads only | A plain-words explanation with file and line references. Nothing on GitHub changes. |
| Fix a pull request | Pull request | Nothing beyond the task review | Reads open review comments, changes code, runs the configured checks, replies inline. It resolves a thread only when its evidence gates allow it. |
| Implement an issue | Issue | Nothing beyond the task review | A branch, commits, and a pull request when the checks and write conditions allow it. |
| Organise the labels | Repository | The proposed labels and which issues fit them, then your Post | Creates the labels you kept, skipping any the repository already has. It changes no issue. |
| Review a pull request | Pull request, optional focus note | One draft review comment, then your Post | Posts the text as a comment review. It never approves a pull request and never requests changes. |
| Start a new project | Project name, one sentence | The plan, then your Post, then a confirmation | Creates one private repository under the owner named in the task, with a readme, a license, and a starter test. Nothing else is created. |
| Tidy up after a merge | A pull request that is already merged, optional release tag | The release notes, then your Post, then a confirmation | Closes the issues the pull request names, deletes the merged branch, and publishes the release. It refuses a pull request that is not merged; merging itself is never done here. |
| Turn your words into issues | Your words | Each issue text, then your Post | Creates the issues you kept, one per piece of work. It posts no comments on existing issues. |
| Write the documentation | Repository, optional focus note | The readme and setup text as a pull request, then your Post | Opens the pull request on approval, or throws the draft away. Discard posts nothing. |

Read-only tasks produce a report, never a patch that gets applied by itself.
Every kind stops rather than pushing when the remote branch moved during the
job.

## Worker behavior behind the interface

A coding task moves through retrieval, editing, verification, and reporting.
It reads the issue or PR history, creates a separate Git checkout, lets the
configured model inspect and edit code, and runs the repository's configured
commands. Before remote writes, it inspects supported repository automation
signals and rereads the target branch to detect another writer's changes.

The worker can commit and push, open a pull request for an issue, comment on
results, reply inline, and resolve a review thread only when its evidence gates
allow it. A fork or non-writable PR head may require a companion branch; that
is reported rather than presented as a successful update of the original
branch. It does not force-push and never merges a pull request. Issues are
closed by the individual merge clean-up task only after your confirmation.
The fleet can also close its own finding issues after confirming that the
intended correction merged.

Draft-producing tasks use the same checkout: the model writes candidate texts
into a drafts folder, the job stops as **Waiting on you**, and nothing is
posted, pushed, or merged until you choose Post. Read-only tasks never write to
GitHub at all; their output is a report and, for a backup, one bundle file.

Repository checks run locally. GitHub checks are separate evidence and may
still be pending. No configured checks means verification is unknown, not
passed. A write held by the repository preflight can leave a job failed even
after editing succeeded. Model-issued commands and repository checks execute
in an isolated Linux sandbox with no network, no account files, and read-only
Git metadata. The checkout's own Git directory stays readable through a
linked worktree, while other tasks' checkouts are hidden. If bubblewrap is
unavailable the command is refused rather than run unconfined. Dependency
preparation uses copied manifests in disposable space; only that step has
network access, and it never runs repository scripts with credentials.

## Monitoring scope

**Agent fleet** discovers all accessible writable repositories when that scope
is selected. Archived, disabled and read-only projects are excluded; new access
can join later scans. Priorities are actionable comments, newest-created PRs,
then eligible source versions. An unchanged file version is not repeatedly
reviewed. New remote work is discovered periodically, not through a guaranteed
one-second webhook. See [the workflow](WORKFLOW.md) and [fleet guide](AGENT-FLEET.md).

**Manual review watches** cover explicitly configured repositories. The one-PR option
covers the selected PR; the repository option includes current and future
open PRs in that repository. It does not discover every repository accessible
to the GitHub account unless those repositories are configured.

The automatic review trigger is new inline review comments. A fix job also
examines conversation history, review summaries, unresolved threads, and check
evidence when it runs. A top-level comment without a command, a CI transition
alone, or a deployment event is not a guarantee of a new automatic job.

Automatic work is bounded by the configured model limits and the per-PR
automatic-job limit of six jobs per UTC day. Existing comments establish a
baseline when a new scope first discovers the PR; use a manual task to handle
that existing feedback. Closed or merged targets are not valid push destinations.
An old comment, removed watch, removed repository, or stale branch is not
unlimited authorization to continue work.

## Other supported interfaces

The browser uses the same worker implementation as the command line and the
same running queue as the GitHub command channel. The worker accepts `/agent implement`, `/agent fix`,
`/agent status`, and `/agent stop` in the configured repositories from allowed
GitHub users. Its first poll establishes a baseline rather than replaying old
commands. A worker's own marked comments are ignored when polling.

The existing CLI remains a standard POSIX interface. From the application
directory, `PYTHONPATH=src python3 -m ghagent.cli --help` lists its commands.
The GUI starts with `PYTHONPATH=src python3 -m ghagent.web --help`.

## Explicit limits

- The worker makes code changes; it cannot decide unspecified product behavior
  on the owner's behalf. Put material decisions in the issue or PR before
  submitting work.
- Model outputs can be wrong. Configured checks cover only what they exercise.
- A stop does not undo GitHub writes or cancel every in-flight operation.
- At least one individual-task repository must remain configured. Add a
  replacement before removing the last one. This requirement does not mean
  every fleet project must be added to that configuration.
- A safe repository preflight is not a guarantee that invisible account-level
  automation cannot incur charges.
- The GUI displays logical model-call counts, not an invoice or a validated
  currency cost meter.
  A logical job call can make up to three HTTP attempts for retryable failures.
- The interface does not publish a public release of the application, change
  the repository's license, deploy an application built by a task, or expose
  this GUI publicly. A repository release is published only by the merge
  clean-up task, after your confirmation.
- Read-only tasks produce a report, never a patch applied on your behalf. A
  single exported file above 50,000,000 bytes is skipped and reported rather
  than truncated silently.
- The worker starts no GitHub Actions runs. Its own count in the safety fence is
  always zero; account-level automation outside the worker is not covered by
  that count.
- Keyboard, responsive, and accessibility behavior require actual checks; see
  the acceptance record for their present status.

## Learning-origin drafts

Documentation and issue-draft tasks handed off from a learning session preserve
its project/account/destination and always require the user’s posting decision,
even when ordinary tasks in the repository have automatic posting permission.
LLM narration is explanatory text, not authorization to execute arbitrary
commands. Actual Git practice commands and GitHub API reads have separate
receipts. Third-party views are links, not embedded or remotely controlled sites.
