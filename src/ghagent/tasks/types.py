"""Task-type registry: one entry per thing FatChin can do, one module each.

Five profiles decide how a task runs:

- "review-fix":  fix a pull request's feedback (existing, byte-stable path)
- "implement":   implement an issue and open a PR (existing path)
- "read-only":   work in a worktree, produce a report, make NO remote writes
- "draft":       produce texts in job.notes["drafts"]; posting is a separate,
                 explicitly authorized action (per-task by default)
- "high-stakes": like "draft"/"write" but every applying action always
                 requires a per-task confirmation, regardless of settings

Every new task type is a module in this package exporting TASK = TaskType(...).
The loader discovers modules at startup; adding a task type never edits a
shared file, which keeps parallel worktrees conflict-free.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Callable

PROFILES = {"review-fix", "implement", "read-only", "draft", "write", "high-stakes"}

# Built-in kinds live here; module kinds are discovered from the package.
_BUILTINS: list["TaskType"] = []


@dataclass(frozen=True)
class TaskType:
    id: str                        # JobKind value, e.g. "explain-repo"
    label: str                     # plain words shown to people
    profile: str                   # one of PROFILES
    help: str                      # one sentence: what it does for you
    # What the task acts on: "repo" (no number), "pr" (number = PR),
    # "issue" (number = issue).
    takes: str = "repo"
    # User-supplied inputs beyond the repository: name -> {label, required}.
    # Floor-level simplicity: at most 3 required (FLUM 3.2).
    fields: dict[str, dict] = field(default_factory=dict)
    # Required verification commands for the repo, or empty = use repo config.
    uses_repo_checks: bool = False
    # Draft texts are posted by the authority gate; high-stakes always asks.
    post_confirm_required: bool = False
    # Some tasks act on finished work (cleanup acts on a MERGED pull request).
    allows_closed: bool = False
    build_instruction: Callable | None = None   # (context: dict) -> str
    validate: Callable | None = None            # (data: dict) -> None | raises
    # For draft/high-stakes kinds: performs the approved writes. Receives
    # (context, gh, job) and returns {"result": text}. Only ever called from
    # the explicit post action, never automatically for high-stakes kinds.
    apply: Callable | None = None

    def required_fields(self) -> list[str]:
        return [k for k, v in self.fields.items() if v.get("required")]


_REGISTRY: dict[str, TaskType] | None = None


def _builtin(kind_id: str, label: str, profile: str, help_text: str,
             takes: str) -> TaskType:
    return TaskType(id=kind_id, label=label, profile=profile, help=help_text,
                    takes=takes, uses_repo_checks=True)


def load_registry() -> dict[str, TaskType]:
    """Discover built-ins plus every module's TASK. Idempotent."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    builtins = [
        _builtin("fix-pr", "Fix a pull request", "review-fix",
                 "Reads every open review comment on a pull request, fixes the "
                 "code, proves it with the repository's checks, and answers the "
                 "reviewers.", "pr"),
        _builtin("implement-issue", "Implement an issue", "implement",
                 "Turns an open issue into working code on a branch, proven by "
                 "the repository's checks, and opens a pull request.", "issue"),
    ]
    registry: dict[str, TaskType] = {t.id: t for t in builtins}
    import ghagent.tasks as package
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name == "types":
            continue
        module = importlib.import_module(f"ghagent.tasks.{info.name}")
        task = getattr(module, "TASK", None)
        if task is None:
            continue
        if task.profile not in PROFILES:
            raise RuntimeError(f"task module {info.name!r} declares an unknown "
                               f"profile {task.profile!r}")
        if len(task.required_fields()) > 3:
            raise RuntimeError(f"task {task.id!r} requires more than three "
                               "inputs; simplify it before shipping")
        registry[task.id] = task
    _REGISTRY = registry
    return registry


def get(task_id: str) -> TaskType | None:
    return load_registry().get(task_id)


def catalog() -> list[dict]:
    """The GUI-facing list: plain labels, help, fields, takes."""
    items = []
    for task in sorted(load_registry().values(), key=lambda t: t.id):
        items.append({
            "id": task.id, "label": task.label, "help": task.help,
            "takes": task.takes, "profile": task.profile,
            "post_confirm_required": task.post_confirm_required,
            "fields": [{"name": name, **spec}
                       for name, spec in task.fields.items()],
        })
    return items
