# Explore a project. Learn the moves.

Open **Explore & learn** when you want to understand a repository before
changing it, or learn Git while watching what each command actually does.
Git is the tool that records a project's changes. GitHub is the website where
those projects can be shared and reviewed.

Choose a repository, then a branch. A branch is a named line of work, such as
`main` or `feature/setup`. FatChin reads the version selected when you load the
view; it shows the commit identifier, the exact saved version, so you know
which code you are looking at. Reload when you want newer information.

## Pick the view that answers your question

| Your question | Where to look | What to expect |
| --- | --- | --- |
| What files are here? | File explorer | Open a folder, then a file. Text is shown as text; code is never run just by opening it. |
| What changed and when? | History | Recent commits, which are saved changes, with the author and date. |
| How do two lines of work differ? | Compare | Choose the starting and ending versions. The GitHub comparison starts at their shared ancestor, the last version they had in common. |
| What is the overall shape? | GitDiagram | An external AI diagram. Check its claims against source; it is not a verified map of every function call. |
| Can I give an AI a text snapshot? | GitIngest | An external summary, folder tree and selected file contents. Filters and file-size limits can leave things out. |
| Can I read generated explanations? | DeepWiki | An external generated wiki and questions about indexed code. A new repository may need indexing first. |
| Can I edit in the browser? | GitHub browser editor | The official github.dev editor. Sign in to GitHub; there is no terminal or code runtime. Only browser-compatible extensions work. |
| Can I browse in a VS Code layout? | GitHub1s | A separate external, read-only explorer. It is not the official GitHub editor. |
| Can another AI query the documentation? | GitMCP setup | Choose your client and copy its configuration. This connects a documentation/code query service; it does not give that service permission to push changes. |

External views open in another tab. FatChin cannot see what you click there.
Some services apply limits, require indexing or offer paid additions; FatChin
does not start those additions for you. The [source-checked fact sheet](GITHUB-EXPLORER-FACTS.md)
explains each claim from the supplied GitHub tips article.

For a private repository, use the internal explorer or official GitHub links.
FatChin does not pass its GitHub credentials to GitDiagram, GitIngest,
GitHub1s, DeepWiki or GitMCP. Third-party repository links and setup snippets
are unavailable when visibility is private or unknown. You can still explore
the repository using the access already connected to FatChin.

## Keep useful links close

The source controls build links for the selected repository and version:

- **Markdown source** uses `?plain=1` to show unrendered Markdown on GitHub.
  **Raw** is a separate file view.
- **Permanent link** pins a file to a commit. The text stays tied to that
  version while the repository and commit remain accessible.
- **Blame** explains which saved change last touched each line. The name is
  Git's; use it to find context, not somebody to blame.
- **Patch** and **Diff** show one commit's changes as text. Reading a patch
  does not apply it.
- **Download ZIP** downloads a code snapshot. It is not a full Git backup:
  history, submodule contents and large-file handling need separate care.
  FatChin uses the repository's actual default branch or selected version,
  rather than assuming every project uses `main`.
- **Avatar** opens the repository owner's public profile image.

On GitHub itself, `t` finds files, `y` pins a file URL to its commit, `b`
opens blame, `l` jumps to a line, and `?` lists the shortcuts available on that
page. Press `.` to open the official browser editor. These keys depend on the
page, keyboard focus and your GitHub accessibility settings. The buttons in
FatChin also work without a physical keyboard.

## Choose how to learn

Select your account and an existing project folder for the lesson transcript.
This is a folder on the server, inside your account, just as with task results.
A missing folder is a request to choose one, not permission to invent a shared
output location.

**Guide me** gives you one instruction and an editor or action button. You
perform that step yourself. FatChin records the result of the action it ran.
It does not treat a completion checkbox as proof that a Git command succeeded.

**Show me** explains the pending step and waits for you to run it. FatChin then
performs that one prepared operation and displays its actual result. Nothing
advances on a countdown. You can stop to read or ask a question as long as you
need.

Ask for the LLM explanation before running a step. The LLM is the configured
language model, which explains the current evidence in ordinary words. Its
narration is separate from the command and receipt. It cannot invent and run
shell commands. The same configured model connection and credentials are used;
opening the explorer or preparing a built-in lesson does not make a model call.
Each requested explanation or answer makes one logical model call with one
HTTP attempt. A failed request stays visible and offers an explicit retry or
the clearly labeled built-in explanation.

Selected source excerpts, the current step and its recorded results are sent
to the configured provider when you request an explanation. Environment files,
credential files and the app's configuration are not lesson context. A
provider error does not cause a switch to another model or endpoint.

## Practice a complete change

The Git lesson works in a disposable practice repository. Its editor changes
only `lesson.txt`. The screen labels this as practice; your selected real
repository and the FatChin installation are unaffected.

The lesson walks through creating the repository, inspecting its state,
changing a file, checking the difference, choosing what to include, saving a
commit, reading history and changing branches. Each step shows:

1. What is about to happen and why.
2. The exact command, with a plain explanation and a memory aid.
3. The real output after you run it.
4. A separate **Next** action when you are ready to move on.

Useful memory aids are short and literal: `status` asks “where do things
stand?”, `diff` means “difference”, `add` adds a change to the next saved
snapshot, `commit` commits that snapshot to history, `log` reads the record,
and `switch` switches branches. Staging is choosing the changes for the next
commit; it is not uploading them. A commit saves locally; a push uploads to a
remote repository. The practice lesson does not push.

Repository-tour steps read your selected project's files and history. Their
receipts identify actual GitHub API reads separately from equivalent Git
commands you could use in a local checkout. FatChin does not claim it ran
`git blame` when it opened a GitHub link instead.

## Move from learning to your own project

Use the real-project task handoff to prepare documentation or issue drafts
for the selected project. Review the repository, instructions, account and
results folder in the normal task wizard. A project must be configured before
FatChin can queue work for it.

The handoff leaves the paced lesson. The normal worker prepares its draft in
the background; it does not pause after every internal command. Follow its
recorded progress in Tasks, then return to the draft decision.

Tasks started from a learning session always wait for your draft decision,
even if that repository normally permits automatic posting. Read the changes
and checks, then choose whether to post. Following a successful pull request
can involve ongoing review monitoring; inspect and manage that separately in
Review watches. A lesson itself does not turn on automatic publishing.

The model explanation is guidance. The actual task details and check results
are the evidence of what changed. You can inspect those while learning and ask
about what they mean before deciding what reaches GitHub.

## Pause, reload and recover

**Pause** prevents the next step. A command already running can finish; its
receipt remains visible. **Resume** returns to the saved session. Reloading
the page does not repeat a command. When another tab changes the same session,
reload its latest state before taking another action.

**End** stops future steps. Export first if you want to keep the transcript
and practice text. Temporary practice files are cleaned only after an active
operation is finished, so cleanup cannot remove a directory beneath a running
Git command. The export stays in the folder you selected.

If an interruption leaves the outcome uncertain, inspect the recorded state
and reconciliation evidence. FatChin refuses to replay a possibly completed
command blindly. Start a fresh practice session if the old one cannot safely
continue. A failed explanation can be retried without repeating a Git action.

Lists and previews have explicit limits. A shortened file or comparison is
marked; use the official GitHub link for the complete view. A connection,
permission or rate-limit error remains an error, rather than appearing as an
empty repository.

## Desktop and phone walkthrough views

These full-page captures show actual Git practice through the browser and
HTTP backend on October 25, 2026, in an isolated verification workspace.
The examples use explicitly chosen built-in explanations; a separate live
check verified the configured GLM narration. Both use the shipped interface.


