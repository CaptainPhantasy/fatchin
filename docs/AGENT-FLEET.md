# FatChin agent fleet

Twenty seats at the workbench. You should not have to spend the day assigning
chairs.

**Agent fleet** keeps a review-and-fix cycle moving across the repositories
accessible to the connected GitHub account. It finds review comments, prepares
small corrections, sends them to a different agent for review, and puts requests
that meet the merge conditions in one window. You make the final merge decision.

A **repository** is a project on GitHub. A **pull request**, or PR, proposes
changes from one branch into another. A **finding** is a specific problem a
reviewer found, with an explanation of what needs fixing.

For a shorter first-use path, read [the push-to-merge walkthrough](WORKFLOW.md).
It maps each stage to its screen, explains saved folders and shows how to
continue a waiting item without resetting an agent.

This guide describes the fleet implementation reviewed on **October 25, 2026**.
The dated verification note below distinguishes interface testing from live work.

The October 25 follow-up connects the fleet to every workspace screen and
enforces four active agents per repository. Its 863-test Git Steward audit and
52 browser checks passed, including all 256 example repositories, restored
account selection, shared controls, a 12,000-task queue and mobile layout.
The integrated lifecycle test now advances from fix publication to independent
review and merge readiness without an intervening discovery scan.

## The pipeline in one page

A push is the trigger. For a feature branch with no pull request, the fleet
opens a **draft** pull request first; a draft cannot be merged. The review then
runs on that pull request, in the same place a person will read it. A push to a
default branch is reviewed as a commit, compared with its first parent.

The review unit is the **pushed commit**, not one file. Changed files are
reviewed in batches of at most 25, with the actual diff as evidence, and a
finding must name a file in that batch. This keeps a large account to a few
hundred bounded reviews instead of tens of thousands of per-file calls, and it
lets the reviewer see a change that spans files.

Default branch or feature branch, the mechanical limits are the same:

- **No merge exists.** The fleet can mark a pull request ready; it cannot merge.
- **No force-push.** A correction is an ordinary commit on a branch.
- **A draft pull request is the first public step**, never a direct write to a
  default branch.
- **Repository policy:** personal repositories are in scope by default and
  organisation repositories are out of scope until named. `include` and
  `exclude` always win over the namespace default.
- **Publication cap:** at most 12 public writes per repository per UTC day,
  shared by every slot.
- **Run budget:** a fleet dispatched with `max_calls_total` pauses when the run
  reaches it instead of starting more work.
- **Drain receipt:** after two quiet minutes the fleet records `drained_at` and
  counts what finished, blocked or was dismissed. That is the "caught up" state.
- **Bounded history:** terminal items are pruned to the most recent 2,000 per
  fleet, so a long catch-up run does not turn the state file into the bottleneck.

### Dry run

Select **Dry run** at dispatch, or pass `"dry_run": true` to `/api/fleet`.
Discovery, review, corrections, and every configured check run normally. The
worker records the exact comment, issue, draft pull request, push, or
finalization it would send in the item's `dry_run_actions` and in the normal
report folder, then stops before the GitHub write. No comment, branch, pull
request, or push is sent. Merge readiness is not published in a dry run.

Dry runs still use model calls. Stop the fleet and dispatch normally when the
recorded actions match what you would have approved.

## Start the fleet

If a fleet is already running, follow its work instead of dispatching another.
Use **Resume fleet** for a paused or stopped fleet. Reopening restores its
saved member and destination. The saved reports folder is
`/home/youruser/Documents/FatChin`; other members select their own folder. The
steps below describe a first dispatch.

1. Open **Agent fleet** in FatChin’s navigation. On a phone, open the navigation
   menu first.
2. Check **Connected as**. This is the GitHub account the installation uses for
   reading repositories and posting changes. Opening FatChin does not require a
   separate application login.
3. Choose **Your member account**, then **Browse folders**. Select an existing
   project folder inside your own server account. The folder browser can create
   a folder when you explicitly choose **New folder**; select **Use this folder**
   afterward. These are server folders, not folders on your phone or laptop.
4. Set the two agent counts. The defaults are **10 GLM agents** and **10 DeepSeek
   agents**. Each pool accepts zero through ten; at least one agent is required.
5. Leave **Repositories to include** set to **All accessible writable
   repositories**, or choose **Choose several repositories**, search GitHub, and
   check every project you want included. Search again to add more; existing
   selections stay selected.
6. Choose **Dispatch 20 agents**, or the button showing your selected total.

You do not need to add every project in **Repositories** first. Account-wide
scanning includes writable projects you own, collaborate on, or access through
an organization. Archived, disabled and read-only repositories are excluded.
New accessible repositories can join subsequent scans.

Starting authorizes review comments, narrowly scoped fixes and correction pull
requests for the selected scope. It does not authorize automatic merging or
branch deletion. Existing credentials stay in place.

The installation shares a maximum of twenty active fleet agents: ten per
provider. The cards show twenty logical slots; an available slot does not mean
an idle model conversation is running. A fresh process takes one task, uses a
fresh conversation when it needs the model, and retires when the task finishes.
Even with one agent working at a time, the scheduler can use a different slot
to review its correction.

At most **four agents work in one repository at a time**, across both provider
pools and all task types. Twenty active agents therefore span at least five
repositories. Source forks share that limit as well. Spare capacity waits when
there are not enough eligible projects; it does not pile twenty agents into one
project merely to keep them busy.

Each correction goes immediately into another worker’s review queue when it is
published. Findings likewise queue a fix as soon as the review finishes. A clean
review checks the current merge conditions immediately. Those handoffs do not
wait for the next whole-account discovery pass. Each stage uses a fresh worker,
and the worker that wrote a finding does not take its own fix stage.

| Pool | Model used | Maximum simultaneous tasks |
| --- | --- | --- |
| GLM | `your-model` through the existing GLM connection | 10 |
| DeepSeek | `your-second-model`, the V4.1-Flash-1017 profile with a 1M-token context window | 10 |

A **token** is a small piece of text counted by the model. The DeepSeek context
window describes that model’s capacity; it does not remove FatChin’s individual
file, change-size or task limits. This guide makes no 1M-context claim for GLM.
Provider availability is displayed in the setup form. An unavailable provider
is not silently replaced with another model. Set that pool to zero if you want
to use only the other pool.

If you stop and change the selected pools, pending tasks from a disabled pool
can continue in the pool you selected. Their history and saved write records
remain intact. A provider rate limit alone never switches the selected model.

The fleet has its own model-call counters and continues independently of the
ordinary worker’s daily call limit. Each task remains bounded. Subscription
terms and provider limits still apply; this feature does not establish free or
unlimited access to either provider.

## What gets attention first

The queue uses this order across the included repositories:

1. **Actionable review comments.** A comment must describe a concrete defect or
   correction. Praise, unrelated instructions and feedback that no longer
   applies do not justify a code change.
2. **Pull requests, newest created first.** An agent reads the current changes
   and relevant code, then records supported findings or a clear review.
3. **Background code reviews.** When higher-priority work is unavailable,
   agents inspect eligible source files on repository branches. The same
   unchanged file version is not repeatedly sent to a model just to keep a
   slot busy.

Running tasks finish their current bounded assignment before taking new work.
A new comment or request goes into the next assignment decision; it does not
interrupt an edit halfway through. After the available file versions have been
reviewed, the fleet watches for new work. This is continuous monitoring, not a
promise to review every repository within a particular day.

A background finding is recorded on GitHub and in **Open comments**. A fix
changes only the files needed for that finding. A PR comment correction targets
that PR’s source branch, including its source repository when a fork is involved;
it is not silently redirected to the default branch. Missing fork permissions
leave a visible held item.

The next independent review examines the correction’s current saved version.
Further findings repair the same correction branch. After three automatic repair
rounds, the item needs a person’s review instead of another uncontrolled loop.
An unrelated new feature, cleanup or rewrite does not belong in that correction.

## Follow the work in four windows

The setup form collapses after dispatch so the work stays close at hand.
Choose **Review setup** to inspect it again. Search and repository filters apply
to the current window; they do not change the fleet’s repository scope.

| Window | What you can do there |
| --- | --- |
| **Agents & queue** | See each pool’s slots, current tasks, stages, automatic resets and waiting work. Open the linked GitHub item for its original context. |
| **Ready to merge** | Open requests whose reviewed version and merge conditions have been checked. Finish the merge on GitHub and use GitHub’s **Delete branch** action afterward. |
| **Open comments** | Follow a finding from **Source comment** to **Correction pull request** to **Independent review**, where those records exist. Read failures and continue held work from the same page. |
| **Activity** | Read task handoffs, automatic recovery, retries and recorded failures. |

The view refreshes while you work. Searching or typing a folder does not discard
your input. If the connection fails, the interface identifies saved information
as last-known evidence. A partial scan is displayed as incomplete; a missing
result is not treated as proof that a request closed.

The same fleet status appears throughout the workspace: Overview, Tasks,
Repositories, Review watches, Updates & decisions, Settings, publishing checks
and Explore & learn. Repositories and Review watches list every discovered
project with a search field; saved individual-task settings and additional
manual watches are shown separately. Tasks includes recent fleet assignments,
and selecting one opens its matching evidence in the fleet window. Settings
controls pause and resume for both pools. Reopening Fleet restores its saved
account, destination and controls. Large queues show 200 matching tasks at a
time with **Show more tasks**; search still covers the full queue.

### What “Ready to merge” means

A request enters this window only when the fleet has a completed review of its
current head—the exact saved commit containing the proposed changes—and current
GitHub evidence allows it:

- It is open, not a draft, and GitHub reports it can merge without a conflict.
- Required approvals and merge requirements are satisfied.
- Current GitHub checks are successful, or, when GitHub has no check rollup,
  FatChin has passing local checks recorded for that exact reviewed commit.
- No unresolved blocking finding or review thread prevents readiness.

A new commit, unresolved feedback, unreadable requirements or incomplete scan
can remove a request from the window. An old green result is not reused for
changed code. Read GitHub’s current merge controls before merging; the state
may have changed since the last scan.

All agents use the connected GitHub identity. A different agent slot gives a
separate review process and fresh context, not a second GitHub account. It
cannot supply an independent account approval for its own PR or bypass a rule
requiring another person’s approval.

FatChin does not merge the request or delete its branch from this window. Click
the item, merge on GitHub when you are satisfied, then delete the branch there
if appropriate. Source comments remain open until the intended correction is
confirmed merged. Supported review threads and fleet-created finding issues are
resolved or closed only after the merge evidence is checked.

## When a check needs attention

A **verification command** is the project’s test or build command. Saved project
commands take precedence. FatChin can identify checks from familiar project
files and prepare supported dependencies in the task’s isolated working copy.
Unknown build systems, inaccessible dependencies and failed tests are reported;
a project is not called verified because a command was merely configured.
For a PR from a fork, **Set verification command** saves the source fork’s
settings. Existing successful GitHub checks can verify a review when no local
command is recognized; publishing a correction still requires local checks on
the proposed files.

For a project with no recognized check:

1. Open its waiting item and choose **Set verification command**.
2. Enter the command documented by that project, such as `npm test`,
   `python3 -m pytest -q`, or `cargo test`. Put separate commands on separate
   lines. These are examples, not interchangeable checks for every project.
3. Leave the default-branch field empty to use GitHub’s default, or enter the
   known branch name.
4. Choose **Save and continue**. The trusted project settings are saved and
   waiting work is reconsidered automatically. No worker reset is required.

For a failed check, expand **Verification failed — read the command output**.
You can see the command, whether it passed, its exit status and the recorded
output. An **exit status** is the number a command returns; zero usually means
success. The next-step message explains the available recovery. Correct a wrong
command through **Set verification command**, or address the specific setup or
source problem shown by the output. **Retry this item** retries that item while
retaining its history; it does not reset the whole fleet.

FatChin holds changes when it cannot verify them. Other eligible work continues.
Credentials, Git internals, symbolic links, binary changes and oversized reviews
have explicit limits. A large or unsupported review remains visible for human
attention; it is not silently treated as complete.

## Automatic recovery and the controls

You do not have to babysit a reset button. The coordinator detects an exited or
stalled worker, retires the failed process and arranges a fresh attempt. It
retains the task’s recorded progress and checks uncertain GitHub writes before
repeating them. A lost network response is not proof that a push failed.

Provider and GitHub rate limits put affected work into a visible waiting period.
The fleet retries after the service’s cooldown. Repeated service failures also
cool down instead of creating a rapid retry loop. Persistent task faults,
conflicting remote changes or an outcome that cannot be established can hold
that item for attention while other work proceeds.

| Control | Effect |
| --- | --- |
| **Pause after current tasks** | Stops new assignments. Current tasks can finish. |
| **Resume fleet** | Allows eligible work to be assigned again. |
| **Stop now** | Prevents new write admission and requests cancellation of the running agents. A request already admitted or sent to GitHub may still finish. Its result is checked during recovery. |
| **Refresh fleet** | Requests another scan and refreshes the displayed state. It does not bypass cooldowns or verification. |
| **Retry this item** | Gives a held item another bounded attempt while preserving its previous evidence and write records. |

Stop the fleet before dispatching a replacement with different counts,
repositories or destination. If the old processes are still stopping, wait for
that state to settle. Restarting your browser does not stop the saved fleet.

## Reports, privacy and GitHub expenses

Reports go to the folder you named in your own account. Task checkouts are
separate temporary working copies. The fleet does not use a shared results pot
or invent a destination when none was supplied.

Repository content needed for a model task goes to that task’s configured
provider. The fleet excludes sensitive paths and redacts recognized credential
patterns. An arbitrary secret hidden in ordinary source text is not guaranteed
to be recognized; do not commit credentials to project files.

The fleet runs on the existing server and uses the existing provider connections.
It does not create new GitHub Actions runners or dispatch a GitHub-hosted coding
job. The existing expense guard inspects supported automation and repository
conditions before writes, and holds writes that could trigger unapproved or
potentially chargeable automation. Those holds remain visible in the item.
This protects the actions the guard actually checks; it is not a guarantee about
invisible external automation, GitHub account billing or provider charges.

## Screenshots and dated verification

**October 25, 2026:** the focused browser fixture passed **32 checks** for fleet
setup, both provider pools, account-wide and selected-repository dispatch,
comments and merge windows, recovery messages, failed check output, keyboard
navigation, mobile layout and safe handling of untrusted text and links.

The browser fixture intercepts every `/api/` request and uses example
repositories. It proves interface rendering and interaction; it does not show
agents running against live repositories. No live twenty-agent fleet is
required to exercise the interface.

The integrated service was then reloaded and verified over HTTPS. The same
32 browser assertions passed against its served assets, Git Steward passed the
856-test suite, and both configured providers completed a small live tool-call
round trip. The complete review/fix/merge lifecycle regression uses actual Git
and isolated checks with simulated GitHub transport and model replies. These
checks establish the implemented process; they do not claim a live account-wide
backlog was processed during testing.



The fixture is [tests/gui-fleet.mjs](../tests/gui-fleet.mjs). Reproduce it against
a server serving the real interface assets, with an explicit results folder:

```sh
node tests/gui-fleet.mjs http://127.0.0.1:PORT /home/YOUR_ACCOUNT/fleet-ui-checks
```

That command checks the browser against simulated API responses. Broader
integration tests, provider probes and installed-service checks are
deployment-specific; run them against your own installation.
