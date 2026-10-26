# FatChin user guide

FatChin is a browser interface to the company's existing GitHub coding
worker. It offers twelve kinds of task: implement an issue, fix or review a
pull request, write documentation, explain a repository, tidy up after a merge,
back up your code, and more. Use it to start that work, decide what reaches
GitHub, follow review feedback, and manage the worker's repositories and
settings. **Agent fleet** adds continuous review and fix work across all
accessible writable repositories, with ten GLM slots and ten DeepSeek slots.

Open [the workspace](https://fatchin.example.com:18465/) while connected to the company
mesh. The configured address is private to that network.

This is a self-hosted tool available on your private network. The interface does
not create a public service or a software release. Connecting GitHub gives the
worker permission to use GitHub; it is separate from opening this workspace.

For maintenance, see [Operations](GUI-OPERATIONS.md). For the boundaries of each
feature, see [Capabilities](GUI-CAPABILITIES.md). Dated verification belongs in
the [acceptance record](GUI-ACCEPTANCE.md).

## Start with the fleet or choose one task

For ongoing account-wide review and corrections, start with
[the push-to-merge walkthrough](WORKFLOW.md). It explains every handoff, the
screen to use, what stays automatic and what needs your decision. A running
fleet needs no duplicate dispatch. **Resume fleet** continues a paused or
stopped one. Reopening restores its saved member, reports folder and controls.

This installation already saves your reports to
`/home/youruser/Documents/FatChin`. Reuse that choice. Other members select a
folder they created within their own account. Temporary coding copies are
separate from reports.

## Before your first individual task

Have these things ready:

- A GitHub repository you can read and write.
- An open issue describing the work, or an open pull request that needs fixes.
- A folder you chose within your own server account for the task's report.
- A working model connection. A model is the service that reads the task and
  proposes code changes; usage follows its provider's subscription or billing terms.

In **Settings → Connections**, connect GitHub, choose the model connection, and
select **Run setup checks**. Check the worker's status before submitting work.
A running web interface does not by itself mean the coding worker is running.

The interface reads real saved jobs and real GitHub data. An empty list means
there are no matching records when the read succeeds. A connection or file read
error is shown as an error; it is not an empty task list.

The navigation includes these pages:

| Page | Use it for |
| --- | --- |
| Overview | Shared fleet status for both model pools, recent fleet work, individual tasks and quick actions. |
| Tasks | Recent fleet assignments link to matching fleet evidence; individual jobs offer details, files, stop and retry. |
| Agent fleet | Dispatch up to twenty agents across your accessible projects, follow open comments and open requests ready for your merge decision. |
| Explore & learn | Browse files, compare changes, open useful views and learn one explained Git step at a time. |
| Updates & decisions | What finished, what needs your decision, and what is being watched (the existing Doorbell view). |
| Repositories | Search all discovered fleet projects; separately add or edit individual-task projects, branches and checks. |
| Review watches | Search fleet coverage; separately create or remove manual follow-up watches. |
| Checks before publishing | Repository checks, automation inspection and posting choices (the existing Safety fence view). |
| Settings | Account-wide fleet controls and both pools; separately labeled individual-task connections, limits and worker controls. |
| Help & getting started | In-app guidance and links to these documents. |


## Keep a fleet working across your projects

For a first dispatch, open **Agent fleet**, choose your member account and results folder, then
choose **Dispatch 20 agents**. The defaults are ten GLM agents and ten
DeepSeek agents, with **All accessible writable repositories** included.
You can reduce either pool to zero through ten, or choose several specific
repositories. At least one agent is required.

No more than **four agents work in any one repository at a time**, counting
both pools and every task type. Twenty busy agents therefore span at least
five repositories. This is a limit, not four reserved stages per project;
tasks start only when their prerequisites are ready. Fewer eligible tasks means
fewer busy agents. Each coding assignment uses its own isolated working copy
and correction branch, with locks to coordinate shared GitHub destinations.

New remote pushes and PRs are found through periodic discovery. Findings queue
their fixes as soon as a review finishes, and published corrections go directly
to another agent's review queue without waiting for another full account scan.
Further findings repair that same correction branch, up to three automatic
repair rounds before a person is needed.

The next free agent handles actionable comments first, then the newest pull
requests, then eligible code that has not been reviewed at its current version.
Each task gets a fresh process and conversation. Failed workers reset
automatically, and rate limits wait for a cooldown. A held item shows its
specific problem; the rest of the eligible work can continue.

Use **Open comments** to follow feedback through a correction and a different
agent’s review. If a check fails, expand its command output. If FatChin cannot
identify a project’s check, choose **Set verification command** on the item;
saving it allows waiting work to continue without a worker reset.

Use **Ready to merge** to open candidates on GitHub. This list requires a
current reviewed commit and verified checks and merge conditions. All agents
share the connected GitHub identity and cannot supply another person’s
required approval. You merge and delete the branch on GitHub.

**Pause after current tasks** stops new assignments. **Stop now** prevents new
write admission and requests cancellation; a write already admitted or sent can
still finish. **Resume fleet** allows work again. Stopping the fleet before
changing its setup preserves the work history.

Read the [complete fleet guide](AGENT-FLEET.md) for provider details, repository
scope, automatic recovery, verification limits and interface fixtures.
The existing **New task** and **Review watches** workflows below remain
available for work you choose individually.

## Choose the right task

| You want to… | Choose | Expected result |
| --- | --- | --- |
| Build or change something described in an issue | Implement an issue | A new branch and pull request, when the worker produces changes and its checks allow the write |
| Address review feedback or check failures on a pull request | Fix a pull request | Changes pushed to that pull request's branch when it is writable; results and any remaining findings recorded |
| Keep responding to new inline review feedback | A review monitor | Follow-up fix jobs for the selected scope, subject to call limits and the monitor's own limits |
| Read progress or inspect a failure | Open the task's details | Saved phase, results, changed files, checks, errors, and report where available |

A pull request is GitHub's proposal to merge a branch's changes. An issue is a
request or problem description. They can share the same number format, so use
the correct task type before selecting a target.

## What FatChin can do

**New task** offers twelve kinds, listed here in alphabetical order. The third
column says what stops and waits for you. Nothing in the task list below ever
merges a pull request or runs a GitHub Action on its own.

| Task type | What it does | What waits for you |
| --- | --- | --- |
| Back up the repository | Writes one complete backup of the repository — every branch and commit — into a single file in your result folder, checks that copy is sound, and explains how to restore it. | Nothing. It reads only; the backup and its restore steps appear in the task's report and files. |
| Bring a branch up to date | Shows which commits one branch would bring into another, then merges them with one merge commit and pushes the result. A conflict stops it and never forces anything. | The plan. Choose **Post** to fetch, merge, and push; a conflict ends the task and names a person to resolve it. |
| Check for everyday problems | Checks a repository for leaked secrets, a missing license or readme, outdated dependency pins, stale branches, and leftover debug files, each with its file and line and a suggested fix. | Nothing. It reads only and reports. |
| Explain a repository | Explains a repository in plain words, with file and line references. | Nothing. It reads only and reports. |
| Fix a pull request | Reads every open review comment on a pull request, fixes the code, proves it with the repository's checks, and answers the reviewers. | Nothing beyond the usual task review. It pushes when the checks and write conditions allow it. |
| Implement an issue | Turns an open issue into working code on a branch, proven by the repository's checks, and opens a pull request. | Nothing beyond the usual task review. |
| Organise the labels | Reads every open issue and proposes a short set of labels, showing which issues each label fits. | The proposal. Choose **Post** to create only the labels you kept. |
| Review a pull request | Explains what a pull request changes in everyday words and says whether its checks pass. | The review comment. **Post** sends it as a comment review — it never approves a pull request. |
| Start a new project | Creates a private repository with a readme, a license, and a starter test. | The plan, and a second confirmation. This is a high-stakes task. |
| Tidy up after a merge | Reads a pull request you already merged, drafts the release notes, then closes the issues it names, deletes the merged branch, and publishes the release. | The draft notes, and a second confirmation. It refuses a pull request that is not merged yet. |
| Turn your words into issues | Reads your words, checks the repository for context, and writes each piece of work as a separate issue. | Every issue, one by one. **Post** creates the ones you kept. |
| Write the documentation | Reads the real code, writes or refreshes the readme and the setup steps a first-time user needs. | The pull request. **Post** opens it; **Discard** throws it away. |

Five of these are read-only or finish by themselves: the backup, the two report
tasks, the PR fix, and the issue implementation. The other seven prepare
drafts. By default they wait for your decision; eligible ordinary drafts can
post under an explicit repository permission, as explained next. Read-only
tasks produce a report, never a patch that gets applied by itself.

## Decide what gets posted

Seven task types prepare drafts. With the default **Ask me every time**
permission, they stop at **Waiting on you** before anything is posted.

1. Open **Updates & decisions** and choose **Post** or **Edit** for the task.
   When several drafts remain, choose the one you want from the draft menu.
   Already posted drafts are excluded.
2. Read the selected draft. **Edit → Save draft** saves your wording without
   posting. **Post selected draft** sends only that draft after its required
   checks and confirmation. **Discard** stops remaining drafts; anything
   already posted stays on GitHub.

Three kinds always require explicit confirmation, even when a repository
allows automatic posting:

- **Bring a branch up to date** merges the chosen source into the chosen target
  and pushes directly, without a PR. Read the branch names and plan, then
  choose **Confirm merge and push** only if you want that target changed.
- **Start a new project** creates a private repository with starter files.
- **Tidy up after a merge** can close supported linked issues, remove an
  eligible merged branch and publish a release. It refuses an unmerged PR.

The fleet never merges PRs. The separate branch-update task can merge branches,
but it always waits for your explicit decision; **Post directly** cannot make
it automatic.

Posting permission is per repository and starts at **Ask me every time**.
**Checks before publishing** lets you choose **Post directly** for an ordinary
repository draft. This is an actual permission: eligible ordinary tasks can
post automatically, including documentation PRs, review comments, labels and
issue drafts. Their records say they were posted under that setting. Switch
back to asking first to remove the standing permission. Learning-session drafts
always wait for you regardless of this setting. Failed or unreadable permission
state never grants automatic posting.

Fleet dispatch separately authorizes its automatic comments and corrections
within the chosen scope. Its findings can close only after the correction is
confirmed merged. It does not use Post for every stage, and it does not merge
or delete a PR branch for you.

## Submit a task

1. Select **New task**.
2. In **Repository**, choose the project and the kind of task from the twelve
   offered, then select **Continue**. Some kinds ask for a target item and a
   short field, such as your question or your words; they are shown on the next
   step.
3. In **Work item**, select the target from the GitHub list. If you know its
   number, use **Look up by number → Look up**. Read its title and description
   and follow **View on GitHub** when you need more context. Repository tasks
   such as **Back up the repository** skip this step.
4. In **Destination**, select **Your account → Browse folders**. Navigate to
   your project folder and select **Use this folder**. The folder browser shows
   folders on the server, not files on the computer running your browser.
5. In **Review**, read the complete task summary, destination, configured
   checks, and call limits. Use **Back** to correct a selection.
6. Select **Queue task** once, then open its details in **Tasks** to follow
   progress.


The review screen above was inspected without queueing the shown issue.

Submission authorizes a real task. For coding work, the worker can call the
configured model, execute repository commands, commit changes, push branches,
create a pull request for an issue implementation, and post GitHub comments. It
does not merge pull requests or start GitHub Actions runs.

Some of the twelve kinds go further, but only after you choose **Post**: the
merge cleanup closes the issues it names, deletes the merged branch, and
publishes a release, and the new-project task creates a private repository.
Those two, and Bring a branch up to date, ask for a separate confirmation first.

After pushing its own commits, the worker can also attempt to stop active
Copilot review runs tied to those commits. This existing behavior records its
outcome and does not stop runs on unrelated commits.

After a successful task, the worker also creates a review watch for the fixed
PR or the PR created from the issue. Later inline review comments can therefore
start follow-up model requests and GitHub updates. This effect is disclosed in
the final review. Remove the watch in **Review watches** if you do not want
automatic follow-up; stop any already queued follow-up task separately.

If the same work is already queued or completed, the worker can reject the
duplicate. Check the existing task before submitting again. Failed or stopped
tasks can be retried after the cause is corrected; a retry can make new
model requests.

### Choose a destination

Select a folder within the home directory of the account you selected. You can
browse existing folders or explicitly create a new folder in the browser.
Choosing an account does not change ownership of another person's files.

There is no shared default output folder. If you leave the destination empty,
the tool shows:

> Error: No destination folder specified. Supply an output path within your user space.

Create a folder within your account, select it, and submit again. If the tool
cannot write there, use the path and access guidance in the error. Grant access
to the service account or the shared output group for that folder; you do not
need to grant access to your whole home directory.

Choose a project folder rather than the home directory itself. Hidden folders,
such as `.config`, are excluded from destination selection. The browser skips
symlink entries so a link cannot be used to send output outside your account.

Each task has a separate `gha-worker-<job_id>/` folder inside the destination
you choose. Its exports include `report.json`, `changes.patch`, and copies of
changed files under `files/` where available. The report identifies skipped
files, for example files outside the export size limits. The file browser shows
these exported artifacts. The worker's temporary repository checkout stays in
its configured scratch space for editing and checks; it is not your output
folder or a replacement for a report.

## Read progress and results

The worker drains GUI and GitHub-command jobs one at a time. A waiting task may be healthy
while another task is running. After submission, the server performs the work;
closing the browser does not cancel the task or its review watches. Return to
**Tasks** later to read the saved result.

| Displayed phase | What it means |
| --- | --- |
| Queued | The request is saved and waiting to run (`received` in the downloaded record). |
| Retrieving | The worker is reading the issue or pull request and its history. |
| Working | The model is inspecting and changing files in a separate checkout. |
| Verifying | The worker is evaluating the repository's configured checks. |
| Responding | The worker is checking whether writes are allowed, then pushing and reporting results. |
| Waiting on you | A draft is ready and requires your decision. Post applies the selected draft; Edit saves wording; Discard stops remaining drafts. Changes posted earlier remain on GitHub. |
| Done | The execution finished. Read the report to see exactly what changed and what was verified. |
| Failed | A step failed or a write was held. The error explains the reason. |
| Stopped | A stop was requested and recorded. Already completed remote writes remain. |

**Done** does not promise that every review thread was resolved or that every
possible behavior was tested. The task report distinguishes local checks,
GitHub checks, produced commits, posted comments, unresolved findings, and
failures.

In **Tasks**, use the search box and status/repository menus to find a task.
Open it to see **Task details**. Select **Browse task files** to inspect its
exported artifacts. **Download report** saves the task's JSON record to your
browser's download location; it does not replace the report in your server
destination.
Use **Refresh task** to update an open detail dialog. The workspace behind it
refreshes every ten seconds; the dialog's explicit refresh keeps it from
changing while you read a result or select a file.
If that refresh fails, the previous details stay visible with an error. Treat
them as the last successful read until a later refresh succeeds.

Each exported file has a **Download** link. A text preview also offers
**Download file**. Binary files cannot be previewed as text, but you can
download them. Downloads are limited to 50,000,000 bytes per file; text previews
are limited to 100,000 bytes and say when shortened. Hidden paths, credential
files, and paths outside this task's export remain unavailable through either
control.

Older tasks created before the GUI may have no selected member, export folder,
or browsable files. Their saved history and GitHub links remain available.
Create a new task with an explicit destination if new work is needed; the tool
does not invent an output location for an old record.

Checks are commands configured for the repository, such as its test suite or
build command. If no checks are configured, the worker cannot claim that the
change passed repository checks. Use the repository's edit form to select
appropriate checks before asking it to make changes.

### Stop or retry

Open **Task details → Stop task** to request a stop. Stopping is cooperative:
the worker checks before and after model requests and before each tool action.
A model request or shell command already in progress can continue until it
returns or reaches its timeout.
Stopping does not undo a pushed commit, delete a branch, retract a comment, or
refund a model request.

Read the error before retrying. Correct missing access, broken commands,
provider configuration, or call limits first. The worker checks GitHub again
and refuses to overwrite a pull request that changed while it was working.
Use **Retry task** only after correcting the cause. Only failed or stopped
tasks are eligible.

Stopping a task does not remove its review watch. Use **Review watches** to
remove that watch as well if you want to prevent future automatic tasks.

## Add individual-task settings for a repository

The fleet's discovered catalog already includes every eligible project in its
selected scope. You do not need to follow these steps for each watched project.
Use them to save a base branch and checks for a project you will choose in
**New task**. Removing a saved project or a manual watch does not remove it
from an account-wide fleet; change the fleet scope separately.

1. Open **Repositories → Add repository**.
2. Use **Find a repository on GitHub → Search**, or enter the exact owner and
   repository name in their fields.
3. Select **Load branches**, then choose the base branch. Issue implementations start
   from this branch; fixing a pull request starts from that pull request.
4. Choose a command under **Add verification commands → Add**, or type
   **Verification commands (one per line)**. Use the commands that actually
   apply to the project. Presets are editable starting points, not a promise
   that those tools are installed.
5. Select **Add repository**, then **Restart worker** when it is idle. Later
   changes use the repository's **Edit → Save repository** controls and also
   require a restart.

The application is not limited to LAILIVE. Each configured repository has its
own base branch and checks. GitHub access is still required for every
repository. Adding a repository does not give the connected GitHub account new
permissions.

Adding a repository also arms a review watch for every open pull request in it,
so new review comments can start follow-up work from day one. **Checks before publishing**
shows that watch as **Armed**. Remove it in **Review watches** if you do not
want automatic follow-up, and stop any already queued follow-up task
separately.

Removing a repository from the configuration does not delete its GitHub
repository, branches, pull requests, or existing task records. Its watch stops
being armed: a removed repository is no longer polled, and **Checks before publishing** only
lists configured repositories. At least one repository must remain configured,
so add its replacement before removing the last repository.
The saved watch for a removed repository becomes dormant rather than being
deleted. Adding that repository again can reactivate it. Remove the watch
first if you do not want that later automatic follow-up.

## Add a manual review watch

These watches are additional individual-task automation. The fleet has its own
account-wide discovery and comment queue, visible above the manual watches.
Fleet discovery also handles existing actionable feedback; the baseline rule
below applies to manual watches.

Open **Review watches → Add review watch**. Under **What to watch**, choose
**One selected pull request** or **All current and future open pull requests**
in one configured repository. Select a destination for resulting reports,
review the scope and automatic-work acknowledgment, then select **Create
watch**.

A monitor authorizes follow-up coding jobs, not just notifications. Those jobs
can use the model and change GitHub. Remove the watch when you no longer want
that scope. **Pause polling** pauses discovery for all repositories and watches;
it does not stop already queued work.

Monitoring operates only on configured repositories while the worker is
running and polling is enabled. The polling interval is a delay between checks,
not an immediate notification guarantee. A busy worker or a rate limit can
delay the next check.

New inline review comments are the automatic fix trigger. A manually submitted
fix task also reads review summaries, conversation history, and GitHub check
evidence. A new CI result by itself is not a promise of an automatic task.
Use **New task** to request a fix for a check failure when needed.
Editing an older inline comment is not a new-comment trigger; submit a manual
task when you want that edited feedback reconsidered.

When a pull request closes or merges, the worker does not keep pushing to it.
If the branch changes during a job, refresh and start again from the new head.
Removing a watch prevents future automatic tasks in its scope; stop an
already queued task separately if you also want that work to stop.
If a previously closed PR is reopened, add its one-PR watch again; an active
repository-wide scope can discover the reopened PR on a later poll.

A newly added one-PR monitor starts after the inline comments already present.
It does not replay those old comments as fresh automatic requests. Submit a
manual fix task if you want the existing feedback handled now.

A repository-wide monitor establishes the same baseline when it first
discovers each open PR. Automatic follow-up is limited to six jobs per PR per
UTC day, in addition to the shared model call limits. New feedback held by that
six-job limit remains pending for a later poll after the UTC day changes.

<a id="the-doorbell"></a>

## Updates & decisions

**Updates & decisions** is the page that answers "what needs me?" without opening tasks. It
shows three piles, newest first, in full sentences:

| Pile | What appears in it |
| --- | --- |
| Done | One line for each task that finished, including failed ones, with a link to its details. |
| Waiting on you | Drafts ready for **Post**, **Edit**, or **Discard**, plus other decisions that concern you. |
| Watched | The review watches that are armed, and any pull request that has gone stale. |

The page reads the worker's own saved events, so an empty pile means nothing has
happened yet — not that the page failed to load. A read error is shown as an
error above the piles. The count on each pile is the number of lines in it.

The Updates & decisions page is a view, not a queue. Choosing **Discard** there posts nothing and
keeps the draft's task in your history; the same three choices are on the task
details.

<a id="the-safety-fence"></a>

## Checks before publishing

**Checks before publishing** shows how changes are checked and when they can
reach GitHub, repository by repository:

- Whether verification commands are configured. A repository without them is
  shown as **No checks**, and write tasks refuse to run there unless you
  explicitly accept an unverified change for that one task.
- Whether an automatic review follow-up is **Armed** or **Not armed**.
- The base branch that issue work starts from.
- Whether the repository rules could be read; **Could not be read** is shown as
  unknown rather than as safe.
- The posting permission for that repository, with the one-tap control described
  in [Decide what gets posted](#decide-what-gets-posted).

It also counts what the worker did today: model calls used, Copilot review runs
it cancelled after its own pushes, and GitHub Actions runs it started — that
count is always zero, because the worker never starts one. Model calls are the
worker's own local count; provider allowances are separate.

A fence entry is a description of the current configuration, not a guarantee.
It cannot see account-level automation or your provider's invoice.

## Connections, settings, and model usage

### GitHub

Use the GitHub connection controls to start browser authorization. Open the
GitHub page shown, enter the displayed device code when asked, and complete
GitHub's instructions. Return to the workspace to check the connection.

Connect or reconnect while the worker is idle. Reconnecting changes the shared
GitHub identity used for every configured repository. Closing the connection
dialog hides it; it does not revoke access already granted on GitHub. An
unfinished device connection expires after 15 minutes. Reopen **Connect
GitHub** to resume checking the current attempt or begin a new one after it
expires.

Some older installations use a GitHub token already saved in the worker's
environment. The GUI honors that existing connection. If it overrides browser
authorization, **Connect GitHub** explains that the maintainer must remove the
override before changing modes. Do not put a GitHub token in the model
credential form: that form is for the model provider or broker.

The connected GitHub account is the identity used for GitHub reads and writes.
The allowed GitHub users setting controls whose `/agent` comments the worker
accepts. It is not an application login or a membership list for the workspace.

In **Settings → GitHub commands**, enter a GitHub username under **Add a
GitHub command user**, then select **Add user**. **Add connected account** adds
the current identity without typing its username. The interface stores GitHub's
permanent numeric ID, so you do not need to find it with a terminal command. Use the
advanced ID list only to remove/import IDs directly. Save and restart while
idle to apply changes to accepted comment authors.

### Model provider

Choose the connection method that matches your installation:

| Method | What to enter | How it works |
| --- | --- | --- |
| Secret broker | Broker URL, consumer/server identifiers, and the broker credential | The broker supplies short-lived request credentials for its configured model service. |
| Direct provider | Compatible provider URL, model identifier, and its API key | The worker reads the provider key from its protected environment file. |

This installation's `your-provider` broker route was verified on October 19, 2026:
it uses Z.AI's **Coding Plan** endpoint,
`https://api.z.ai/api/coding/paas/v4`, with no fallback endpoint configured.
The worker model remains `your-model`. This is the Coding Plan route, not
the separate pay-as-you-go route described in
[Z.AI's endpoint documentation](https://zcode.z.ai/en/docs/configuration#bigmodel--zai-api-endpoints).
The GUI does not independently verify your account's subscription entitlement
or invoice.

The provider must support the chat-completions and tool-calling interface used
by the worker. A model name alone does not prove compatibility. Credential
inputs are write-only: saving a key does not make it available for reading
back from the workspace.

**Provider endpoint** is the provider's base URL. The worker adds
`/chat/completions`; do not append that path yourself or include a key in the
URL. Use **Settings → Advanced → Provider reasoning mode** only when your
provider supports it. **Use provider default** omits the extra control;
**Disable separate reasoning output** sends a provider-specific option that
an incompatible provider can reject.

Passive diagnostics do not request a model completion. A live provider test is
a separate action with its own usage confirmation and at most one model
completion HTTP attempt.
Submitting a coding task or enabling a monitor can also lead to model requests.

### Switching provider from the terminal

A maintainer can move the worker to another model provider with one command.
The steps are in a fixed order, and the switch is proven before it stays:

1. The current configuration file is backed up beside itself, with a timestamp
   in the name.
2. The new endpoint, model, and sign-in mode are written in place.
3. The worker makes **one** tiny live call to the new endpoint.
4. A receipt line with the result, the endpoint, the model, the sign-in mode,
   and the round-trip time is appended to `provider-receipts.jsonl` in the
   worker's state folder.
5. If that call fails, the previous settings are put back before the command
   returns, so a bad switch never leaves the worker pointing at a broken
   endpoint.

From the application directory:

```sh
PYTHONPATH=src python3 -m ghagent.cli provider-switch \
    --base-url https://api.example.com/v1 \
    --model-id example-model \
    --auth-mode broker-lease
```

`--auth-mode` is either `broker-lease` (the company secret broker supplies
short-lived credentials) or `env` (the provider key is read from the worker's
protected environment file). Leave out any of the three options to keep the
current value. Missing sign-in material stops the command before anything is
written and it says so.

The command prints the new model and endpoint, the live check's PASS or FAIL
with its time, and the name of the backup file. Exit status is **0** when the
call passed and non-zero when it did not. Restart the worker while it is idle so
the change takes effect. The receipt is a record of that one check; success
proves the endpoint answers a short message, not that every coding task will
work.

### Limits and shared settings

Set the model call limit per task and per UTC day, the tool iteration limit,
request timeout, command timeout, and output limit as appropriate. A call limit
is a local worker control, not a provider-plan quota or a price. The installed
limits are 100 logical calls per task and 600 per UTC day; changing them does
not change your subscription. Provider subscription/billing terms determine
any actual charges. A logical model call within a job can make up to three HTTP
attempts for retryable network, rate-limit, or server errors. Those attempts
count as one logical call under the configured job/day call ceilings. A retry
uses additional requests under the same provider/subscription terms.

The worker reserves each logical call before sending it. Failed requests and
confirmed provider tests use the shared daily allowance as well. Reservations
are saved across restarts, with an independent allowance for each task. A
retried task has a new task allowance; it still shares the daily limit. This
usage covers this worker and its connection tests, not model requests made by
other applications.

Daily counters reset at midnight UTC, which can be a different date from your
browser's local date. Displayed event times use the browser's local time where
shown; saved records retain UTC timestamps.

Settings affect the shared worker and all configured repositories. Wait for
current work to finish before applying changes that restart it. Read the
confirmation so you understand which service changes and whether queued work
will continue.

Select **Save settings**, then **Restart worker** while idle to apply them.
**Reload saved** lets you discard unsaved form edits after confirmation. If
another window saved a newer configuration, the server refuses your stale save:
reload the saved settings, review them, and reapply your changes. Installation
paths and the GitHub host are shown for reference; changing them requires the
maintainer to update the installation.

Leaving Settings with unsaved edits, including through browser Back, asks
before discarding them. While a save is in progress, wait for its result before
editing the form again. If the connection fails, read the error and reload
saved settings before assuming the change took effect.

## When something goes wrong

For fleet assignments, start with the [same-screen recovery table](WORKFLOW.md#handle-a-waiting-item-without-resetting-the-application).
Worker exits and stalls recover automatically. **Set verification command →
Save and continue** reconsiders waiting work without restarting the worker.
Fleet cooldowns and counters are separate from individual-task daily limits.
The table below also covers the individual-task worker.

| What you see | What to do |
| --- | --- |
| GitHub is disconnected or the repository list cannot load | Reconnect GitHub, then refresh. Check the connected account's repository access. |
| Model credential is missing or rejected | Check the selected connection method and replace the correct credential. The worker does not silently switch providers. |
| The daily or task call limit was reached | Read the task's call count. Wait for the UTC daily reset or deliberately adjust the relevant limit, then retry. |
| Repository check failed | Open the check output. Correct the code or the check command; do not mark a failing check as passed. |
| Writes held for review | Read the recorded preflight reasons. Workflow triggers, repository rules, or unreadable settings can hold writes. |
| Remote moved | Someone changed the pull request branch. Refresh and retry from its current revision. |
| Target closed or merged | Choose an open target. Already completed work remains in the history. |
| Workspace is offline | Keep the page open or reconnect and refresh. Saved work is on the server; a browser connection failure does not prove that the worker stopped. |
| Report or file export failed | Keep the task ID and recorded export error. Restore access to the chosen destination before recovery; retained scratch is working material, not a successful report. GitHub changes that already landed remain. |
| State file is unreadable | Preserve the error and contact the maintainer with the affected file and time. Do not replace saved state with an empty list. |

Do not repeatedly retry an identical error. Keep the task identifier, displayed
message, check output, and report. Do not include API keys or device codes in a
support report.

## Keyboard and accessibility

Use **Tab** and **Shift+Tab** to move between links and controls, **Enter** or
**Space** to activate them, and the arrow keys to choose native menu options.
The **Skip to content** link moves past navigation. Dialogs keep their
controls together; use the close control or **Escape** to dismiss a dialog
without submitting it.

Use **Alt+N** to start a new task and **Alt+R** to refresh the workspace. Your
browser or operating system may reserve a shortcut; the visible controls
remain available.

Status uses text alongside icons and color. Browser zoom and a narrow window
are supported; use the navigation menu on smaller screens. The interface uses
locally served fonts and icons, so opening it does not require a font CDN.
Use [the acceptance record](GUI-ACCEPTANCE.md) for the accessibility checks
actually performed; these instructions are not a certification of every assistive
technology.

## Explore a project and learn Git

Start in **Explore & learn**. Choose a repository and branch, inspect its files
and history, or begin a paced lesson. **Guide me** lets you perform each step;
**Show me** waits for you before performing one explained step. Practice changes
stay in a disposable repository. For instructions, provider behavior, real-project
draft handoff and recovery, read [Explore and learn](EXPLORE-AND-LEARN.md).
