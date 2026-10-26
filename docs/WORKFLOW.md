# FatChin: from a new push to your merge decision

You push your work. FatChin reviews it, turns supported problems into small
corrections, and has another fresh agent review the result. You open **Ready to
merge** when you want to make the final decision. The coffee is optional.

A **repository** is a project on GitHub. A **branch** holds a separate line of
work. A **commit** is a saved version of that work. A **pull request**, or PR,
asks to bring one branch's changes into another after review. A **finding** is
a specific problem with enough detail to fix it.

## Start here

1. Open FatChin in your browser, then **Agent fleet**. On a phone, open the
   navigation menu first.
2. If a fleet already exists, read its status. A running fleet needs no second
   dispatch. Use **Resume fleet** if it is paused or stopped.
3. For a first dispatch, choose your member account and a reports folder you
   created inside that account. **Browse folders → Use this folder** chooses a
   folder on the server. This installation already has a saved folder,
   `/home/youruser/Documents/FatChin`; reopening the fleet restores its saved choice.
   Other members choose their own folder.
4. Leave **All accessible writable repositories** selected to cover the
   connected GitHub account. You do not need to add projects one at a time.
   Alternatively choose several repositories and select the ones you want.
5. Keep **10 GLM agents** and **10 DeepSeek agents**, or reduce either count.
   Choose **Dispatch 20 agents**, or the button showing your selected total.
   Dispatch permits comments, corrections and correction PRs in that scope.

The two pools share a maximum of **20 active tasks**, with **at most four in
any one repository**, across both pools and every task type. At full capacity,
at least five repositories have work in progress. Source forks count toward
the limit too. Fewer eligible projects or tasks means fewer busy agents.

A slot is capacity for one assignment. Every assignment starts a fresh worker
and model conversation; that worker retires afterward. It does not collect a
day's worth of unrelated code in one conversation.

## What happens after a push

| Stage | What FatChin does | What you see or do |
| --- | --- | --- |
| Discover | Scans included repositories for current PRs, actionable comments and eligible source versions. | **Repositories** and **Review watches** show the searchable discovered catalog. Discovery is periodic; a push is not a promise of a one-second response. |
| Review | A fresh agent reads the current changes and records supported findings, or a clean review. | Follow its assignment in **Agents & queue**, or open recent fleet work from **Tasks**. |
| Correct | Another fresh agent fixes the specific finding in its own isolated working copy and branch, then runs the project's checks. It publishes the correction only if the checks and write conditions allow it. | **Open comments** connects the source feedback, correction PR and review. Read the check output on a held item. |
| Review the correction | A different agent examines the published version. It does not review its own correction or take its own finding's fix stage. | The correction enters the review queue immediately, without waiting for the next account scan. |
| Decide readiness | FatChin checks the reviewed commit against current GitHub checks, approvals, conflicts and unresolved feedback. | Eligible PRs appear in **Ready to merge**. Held requirements remain visible. |
| Merge | You follow the item's GitHub link, inspect the changes and merge there. | Use GitHub's **Delete branch** afterward if appropriate. The fleet does not merge or delete it for you. |
| Finish and continue | FatChin confirms the correction merged before closing supported source threads or its own finding issues, then takes the next eligible task. | **Activity** records the handoffs and outcomes. Reports stay in your selected folder. |

A push without a PR can be examined through background source review. If that
finds a concrete defect, a correction can become a PR; an ordinary push does
not automatically become a PR merely because it exists.

If a correction needs more work, the next fix updates the same correction
branch. After three automatic repair rounds, it waits for a person instead of
opening an endless stack of PRs. A fix must stay within the described problem.

## Which task goes next?

Across the included repositories, actionable comments take priority, followed
by newest-created PRs, followed by background source reviews. An agent finishes
its current assignment before taking the next one. Newer work can therefore
take the next available place without interrupting an edit halfway through.

The four-agent limit is a ceiling, not four stages reserved in every project.
Work only starts when its prerequisites are ready. FatChin avoids repeated
reviews of unchanged source versions. When there is nothing eligible left,
the fleet watches for new work; it does not manufacture busywork.

## Know which screen answers your question

| Your question | Open |
| --- | --- |
| Are both models working, and how many projects are covered? | The shared fleet status on every screen; **Agent fleet → Agents & queue** for details. |
| Where are all my repositories? | **Repositories** or **Review watches**. Search the fleet catalog. The smaller list of saved individual-task projects is separate. |
| What is this agent doing? | **Agents & queue**. Recent fleet tasks in **Tasks** link to the matching fleet evidence. |
| Did the comment get fixed? | **Open comments**, then the source comment and correction links. |
| What can I merge? | **Ready to merge**, then click through to GitHub. |
| Why did an item stop? | Its recorded error and check output; **Activity** for recovery history. |
| What needs my decision? | **Updates & decisions** for recent fleet outcomes and individual-task drafts; **Open comments** for the full feedback cycle. |
| How do I change the checks? | **Set verification command** on the held item; **Checks before publishing** shows project check information. |
| How do I pause both model pools? | Fleet controls in **Agent fleet** or **Settings**. |
| Where are my reports? | The folder you selected. Task details offer available report files. |
| How do I learn what Git is doing? | **Explore & learn → Learn step by step**. **Guide me** lets you perform a move; **Show me** explains and runs one move at a time. Choose the next move yourself. |

## Handle a waiting item without resetting the application

| What you see | What to do |
| --- | --- |
| No recognized verification command | Choose **Set verification command**. Use the command from that project's documentation, then **Save and continue**. Waiting work is reconsidered automatically. For a fork PR this saves the source fork's settings. |
| Verification failed | Expand **Verification failed — read the command output**. Correct the specific problem. A configured command is not evidence that it passed. Use **Retry this item** after the cause is addressed. |
| Provider or GitHub rate limit | Leave the fleet running. Affected work waits through the service's cooldown and retries; other eligible work can continue. |
| Worker exited or stalled | Automatic recovery replaces it with a fresh worker and checks any uncertain GitHub write before repeating it. No manual agent reset is required. |
| Remote branch changed or permissions are missing | Read the item. FatChin holds conflicting work instead of overwriting another push. Restore required access or address the branch problem before retrying. |
| GitHub write held | Read the automation or ruleset reason. The existing expense guard may hold a write that could trigger unapproved automation or whose conditions cannot be checked. Do not repeatedly retry the same unresolved hold. |
| PR absent from Ready to merge | Inspect its current review, checks, required approvals, unresolved threads and conflicts on GitHub. Another fresh agent uses the same GitHub identity; it cannot provide another person's required approval. |

Successful current GitHub checks can support a review when no local check is
recognized. Publishing a code correction still requires passing local checks
on the proposed version. Failed local checks are not replaced by a green badge
from an earlier commit.

## Pause, stop or change the scope

- **Pause after current tasks** stops new assignments and lets current work
  finish. **Resume fleet** allows assignments again.
- **Stop now** requests cancellation and prevents admission of new GitHub
  writes. A write already admitted or sent may finish. It does not undo changes.
- **Refresh fleet** requests a scan; it does not bypass checks or cooldowns.
- To change counts, scope or destination, stop the fleet, wait for its workers
  to finish stopping, then dispatch the replacement setup. Saved history stays.

Closing the browser does not stop the fleet. Restarting the shared worker is a
maintenance action; it is not the normal way to recover an individual agent.
**Pause polling** belongs to individual-task discovery and does not replace
the fleet's controls.

## Choose one task instead

Use **New task** when you want to implement an issue, prepare documentation,
back up a repository, or choose one of the other twelve individual task types.
Those use saved individual-task repository settings and their own worker limits.
Ordinary drafts use **Waiting on you → Post / Edit / Discard** by default;
repository **Post directly** permission can publish eligible ordinary drafts
automatically. Branch updates, new projects and merge cleanup still require
explicit confirmation, and learning-session drafts always wait for you. The fleet
dispatch separately authorizes its automatic review-and-fix cycle; it does not
wait for Post on every correction.

Manual **Review watches** follow future inline feedback on their chosen PRs.
Removing one does not remove that repository from an account-wide fleet.
Change fleet scope separately if you want to stop fleet work there.

Read the [user guide](GUI-GUIDE.md) for individual tasks and learning sessions,
the [fleet guide](AGENT-FLEET.md) for detailed behavior and limits, and
[troubleshooting](TROUBLESHOOTING.md) for recovery. FatChin runs the coding work
on this server using the existing model connections. It does not create new
GitHub-hosted coding jobs; provider usage still follows the provider's terms.
No promise here turns either model subscription into unlimited free service.
