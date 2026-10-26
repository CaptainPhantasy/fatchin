"""new-project: create the seed of a healthy project in a brand-new repository.

High-stakes profile: the agent works in an empty scratch git repository, writes
the README, .gitignore, license, and one runnable smoke test, and drafts
.gha-drafts/plan.md. The job stops at WAITING_ON_YOU; nothing is committed or
created on GitHub until the person confirms Post. That one action commits the
starter files (without the draft notes) and creates the private repository
under the owner named in context["repo"], pushing the first commit.

Every failure is raised as ValueError carrying one plain sentence the person
can act on, so the GUI shows what happened rather than a traceback.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from .types import TaskType
from ..task_write_guard import ensure_not_stopped

DRAFT_DIR = ".gha-drafts"
PLAN_FILE = ".gha-drafts/plan.md"
COMMIT_MESSAGE = "feat: initial project scaffold"
GIT_TIMEOUT_SECONDS = 600.0
GH_TIMEOUT_SECONDS = 120.0
MAX_NAME = 80
MAX_IDEA = 500
DEFAULT_OWNER = ""  # empty: use the connected GitHub account
LICENSE_HOLDER = "the repository owner"

_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,%d}" % MAX_NAME)


def _clip(text: object, limit: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _values(context: dict, job) -> dict:
    values = (context or {}).get("values")
    if isinstance(values, dict):
        return values
    notes = getattr(job, "notes", None) or {}
    stored = notes.get("values")
    return stored if isinstance(stored, dict) else {}


def _language(idea: str) -> str:
    """The language the idea implies; Python is the safe default."""
    text = f" {str(idea or '').casefold()} "
    for language, hints in (
        ("TypeScript", (" typescript ", " node ", " nodejs ", " javascript ",
                        " npm ")),
        ("Go", (" golang ", " go ")),
        ("Rust", (" rust ", " cargo ")),
        ("Java", (" java ", " kotlin ")),
        ("Ruby", (" ruby ", " rails ")),
        ("C#", (" c# ", " dotnet ", " .net ")),
    ):
        if any(hint in text for hint in hints):
            return language
    return "Python"


def build_instruction(context: dict) -> str:
    """Describe the starter files and the plan the person will review."""
    context = context or {}
    values = _values(context, None)
    name = _clip(values.get("name") or context.get("title") or "the new project", MAX_NAME)
    idea = _clip(values.get("idea"), MAX_IDEA)
    language = _language(idea)
    if language == "Python":
        test_line = (
            "- tests/test_smoke.py: one runnable test written with Python's "
            "built-in unittest module that asserts something true (for example "
            "that 2 + 2 == 4), so `python3 -m unittest discover -s tests` works "
            "on the very first run.")
    else:
        test_line = (
            f"- the tests/ folder: one runnable smoke test in {language}, in the "
            "file name and style that language's normal test tool expects, "
            "asserting something true so the first test run passes.")
    lines = [
        f"Task: start the project “{name}” from nothing.",
        "",
        "You are working in a brand-new, empty git repository. No files exist "
        "yet, so every file you create is the beginning of this project. The "
        "person will read your plan and choose Create before anything reaches "
        "GitHub.",
        "",
        "Create the seed of a healthy project:",
        "- README.md: what the project is in plain words, who it is for, and "
        "exactly how to start — the setup steps and the command that runs it. "
        "Write it for a first-time user.",
        "- .gitignore: the files this project should never commit — build "
        "output, installed dependencies, editor and operating-system clutter "
        f"for {language}.",
        f"- LICENSE: the full MIT license text, copyright {LICENSE_HOLDER}, "
        "unless the idea below says who should hold the copyright.",
        test_line,
        "",
        f"Write the work in {language} unless the idea clearly asks for "
        "something else; keep the files small enough to read in one sitting.",
        "",
        f"Where the plan goes: write {PLAN_FILE}. Its first line must be "
        f"exactly: Create {name}",
        "Everything after that first line is a short, plain-language list of "
        "exactly what will be created — one line per file, saying what it "
        "contains. No file that is not on this list.",
        "",
        "Leave git to FatChin: do not stage, commit, push, or create a "
        "repository yourself. The person confirms first.",
    ]
    if idea:
        lines += ["", f"The idea, in the person's words: {idea}"]
    return "\n".join(lines)


def validate(data: dict) -> None:
    """A project needs a name and one sentence about it."""
    data = data or {}
    values = data.get("values") if isinstance(data.get("values"), dict) else data
    name = str((values or {}).get("name") or "").strip()
    idea = str((values or {}).get("idea") or "").strip()
    if not name or not _NAME_PATTERN.fullmatch(name):
        raise ValueError("Give the project a name of 1–80 letters, numbers, "
                         "dots, dashes or underscores.")
    if not idea:
        raise ValueError("Write one sentence about the project so the starter "
                         "files match what you want.")
    if len(idea) > MAX_IDEA:
        raise ValueError("Keep the project idea to one sentence — 500 "
                         "characters or fewer.")
    return None


def _run_git(args: list[str], cwd: str,
             timeout: float = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run one explicit `git` command inside the scratch folder.

    Tests patch this function (or subprocess.run); nothing else here spawns git.
    """
    argv = ["git", *args]
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "git did not finish in time"
    except OSError as exc:
        return 127, "", str(exc)


def _worktree(context: dict, job) -> str:
    notes = getattr(job, "notes", None) or {}
    for candidate in (context.get("worktree"), notes.get("worktree_path"),
                      notes.get("worktree")):
        text = str(candidate or "").strip()
        if text and os.path.isdir(text):
            return text
    raise ValueError("The scratch folder for this run is gone, so there was "
                     "nothing to commit. Run the task again to rebuild the "
                     "starter files.")


def _has_draft_files(worktree: str) -> bool:
    folder = Path(worktree) / DRAFT_DIR
    try:
        return any(path.is_file() for path in folder.rglob("*"))
    except OSError:
        return folder.is_dir()


def apply(context: dict, gh, job) -> dict:
    """Commit the scaffold and create the private repository, exactly once.

    The draft notes stay out of the commit; the push is the ordinary first
    push done by `gh repo create --push`, never a force-push.
    """
    context = context or {}
    values = _values(context, job)
    name = str(values.get("name") or "").strip()
    repo = str(context.get("repo")
               or getattr(job, "repo_full_name", "") or "").strip()
    if "/" not in repo:
        if not name:
            raise ValueError("The project name could not be read, so nothing "
                             "was created on GitHub.")
        owner = DEFAULT_OWNER
        if not owner:
            owner = str((gh.rest("GET", "user") or {}).get("login") or "").strip()
        if not owner:
            raise ValueError("The connected GitHub account could not be read, "
                             "so no owner was chosen. Nothing was created.")
        repo = f"{owner}/{name}"
    worktree = _worktree(context, job)

    rc, out, err = _run_git(["add", "-A"], cwd=worktree)
    if rc != 0:
        raise ValueError("The starter files could not be staged (git said: "
                         f"{_clip(err or out)}). Nothing was created on GitHub.")
    if _has_draft_files(worktree):
        rc, out, err = _run_git(["reset", "-q", "HEAD", "--", DRAFT_DIR],
                                cwd=worktree)
        if rc != 0:
            raise ValueError("The draft notes could not be kept out of the "
                             f"first commit (git said: {_clip(err or out)}). "
                             "Nothing was created on GitHub.")
    rc, out, err = _run_git(["commit", "-m", COMMIT_MESSAGE], cwd=worktree)
    if rc != 0:
        raise ValueError("The starter files could not be committed (git said: "
                         f"{_clip(err or out)}). Nothing was created on GitHub.")

    from .. import responder  # lazy: importing responder pulls in gh plumbing

    rc, out, err = None, "", ""
    ensure_not_stopped(context)  # repo create --push is the first remote write
    try:
        rc, out, err = responder._run_gh(
            ["repo", "create", repo, "--private", "--source", worktree,
             "--push"],
            timeout=GH_TIMEOUT_SECONDS)
    except Exception as exc:
        raise ValueError("The starter files were committed on this machine, "
                         "but the GitHub command could not run ("
                         f"{_clip(exc)}). Nothing was pushed; run the task "
                         "again once the connection works.") from None
    if rc != 0:
        raise ValueError("The starter files were committed on this machine, "
                         f"but GitHub refused to create the repository (gh "
                         f"said: {_clip(err or out)}). Nothing was pushed; run "
                         "the task again once the connection works.")
    return {"result": f"Created {repo} — README, license, and starter tests "
                      "are in place."}


TASK = TaskType(
    id="new-project",
    label="Start a new project",
    profile="high-stakes",
    help="Creates a private repository with a README, a license, and a "
         "starter test, after you review and confirm the plan.",
    takes="none",
    fields={
        "name": {"label": "Project name", "required": True},
        "idea": {"label": "One sentence about it", "required": True},
    },
    post_confirm_required=True,
    build_instruction=build_instruction,
    validate=validate,
    apply=apply,
)
