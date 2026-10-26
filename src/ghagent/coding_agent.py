"""The tool-using coding agent for gha-worker.

Runs a bounded model/tool loop inside a single git worktree. Every path the
model touches is resolved against the worktree root and rejected if it
escapes. Tool errors are reported back to the model as tool results; run()
never raises past its caller for model/spend failures — it returns an
AgentResult with the error set.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable

from ghagent.interfaces import AgentResult, Config
from ghagent.model_client import ModelError  # SpendLimitExceeded subclasses it

CheckRunner = Callable[[list[str]], tuple[int, str]]

_READ_CAP_CHARS = 200_000
_OUTPUT_TAIL_CHARS = 4_000

SYSTEM_PROMPT = """\
You are a coding agent working inside a dedicated git worktree on this machine.
Produce the smallest complete change that satisfies the instruction — nothing more.

Rules:
- Read the relevant code before editing anything.
- Follow repository instruction files you find in the worktree (README,
  CONTRIBUTING, AGENTS.md and similar) when they apply to this change.
- Add a regression test only when it demonstrates the defect being fixed.
- Run every check listed in the task and make sure they pass before you finish.
- Stay strictly inside the worktree you were given. Never read, write, or
  run anything inside .git internals.
- Never send, copy, echo, or exfiltrate credentials, tokens, or keys anywhere.
- When the change is complete and the checks pass, call finish(summary,
  changed_files).
"""

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file inside the worktree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory inside the worktree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path; empty for the worktree root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file inside the worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact old_string with new_string in a worktree file. "
            "old_string must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command (argv, no shell) inside the worktree. "
            "Runs in an isolated checkout with no network or account files; Git metadata is read-only.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the task. Call only when the checks pass.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "changed_files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "changed_files"],
            },
        },
    },
]


def is_secret_var_name(name: str) -> bool:
    """True for env var names the worker must never pass to child processes."""
    name = name.upper()
    return (
        any(part in name for part in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'PRIVATE_KEY'))
        or name.endswith(('_KEY', '_KEY_ID', '_ACCESS_KEY', '_ACCESS_KEY_ID'))
        or name in ('SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'GIT_ASKPASS')
    )


def scrub_env(env) -> dict[str, str]:
    return {k: v for k, v in env.items() if not is_secret_var_name(k)}


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…[truncated]\n" + text[-limit:]


class CodingAgent:
    def __init__(
        self,
        model_client,
        cfg: Config,
        check_runner: CheckRunner | None = None,
    ) -> None:
        self._model = model_client
        self._cfg = cfg
        self._check_runner = check_runner
        self._active_worktree: str = ""
        self._should_stop = lambda: False

    # -- entry point ---------------------------------------------------------

    def run(self, task: dict) -> AgentResult:
        try:
            return self._run(task)
        except ModelError as exc:  # includes SpendLimitExceeded
            return self._error_result(task, str(exc))
        except Exception as exc:  # defensive: never raise past run()
            return self._error_result(task, f"unexpected agent error: {exc!r}")

    def _error_result(self, task: dict, message: str) -> AgentResult:
        root = task.get('worktree_path', '') if isinstance(task, dict) else ''
        changed = self._git_changed_files(root) if root and os.path.isdir(root) else set()
        return AgentResult(
            summary="",
            changed_files=sorted(changed),
            worktree_path=root,
            checks=[],
            model_calls_used=0,
            error=message,
        )

    def _run(self, task: dict) -> AgentResult:
        if not isinstance(task, dict):
            raise ValueError("task must be a dict")
        instruction = task.get("instruction")
        worktree_path = task.get("worktree_path")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("task must carry a non-empty 'instruction'")
        if not isinstance(worktree_path, str) or not os.path.isdir(worktree_path):
            raise ValueError(f"task worktree_path does not exist: {worktree_path!r}")

        root = os.path.realpath(worktree_path)
        self._active_worktree = root
        changed: set[str] = set()
        summary = ""
        error = ""
        model_calls = 0
        should_stop = task.get("should_stop", lambda: False)
        self._should_stop = should_stop

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_prompt(task, root)},
        ]

        finished = False
        exhausted = False
        for _ in range(max(0, self._cfg.max_tool_iterations)):
            if should_stop():
                error = "Task stopped by a user."
                break
            try:
                resp = self._model.chat(messages, tools=TOOLS)
            except ModelError as exc:
                error = str(exc)
                break
            model_calls += 1
            if should_stop():
                error = "Task stopped by a user."
                break
            content = resp.get("content")
            tool_calls = resp.get("tool_calls") or []
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                summary = (content or "").strip() or "the model returned no summary"
                break

            for call in tool_calls:
                if should_stop():
                    error = "Task stopped by a user."
                    break
                name, args, call_id = self._parse_call(call)
                if name == "finish":
                    summary = str(args.get("summary") or "the model returned no summary")
                    declared = args.get("changed_files")
                    if isinstance(declared, list):
                        changed.update(f for f in declared if isinstance(f, str))
                    messages.append(self._tool_message(call_id, {"ok": True}))
                    finished = True
                    break
                result = self._dispatch(name, args, root, changed)
                messages.append(self._tool_message(call_id, result))
            if finished or error:
                break
        else:
            exhausted = True

        if exhausted and not error:
            error = (
                f"max_tool_iterations ({self._cfg.max_tool_iterations}) reached "
                "without the model calling finish"
            )

        checks = self._run_checks(task.get('checks') or [], should_stop)
        if should_stop():
            error = 'Task stopped by a user.'
        changed |= self._git_changed_files(root)
        return AgentResult(
            summary=summary,
            changed_files=sorted(changed),
            worktree_path=root,
            checks=checks,
            model_calls_used=model_calls,
            error=error,
        )

    # -- model loop helpers ---------------------------------------------------

    def _user_prompt(self, task: dict, root: str) -> str:
        parts = [
            f"Worktree: {root}",
        ]
        repo_context = task.get("repo_context")
        if isinstance(repo_context, str) and repo_context.strip():
            parts.append(f"Repository context: {repo_context.strip()}")
        parts.append("Instruction:")
        parts.append(task["instruction"].strip())
        checks = task.get("checks") or []
        if checks:
            parts.append("Checks that must pass before you call finish:")
            parts.extend(f"- {c}" for c in checks)
        return "\n\n".join(parts)

    def _parse_call(self, call: dict) -> tuple[str, dict, str]:
        function = call.get("function") or {}
        name = function.get("name") or ""
        call_id = call.get("id") or ""
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            return name, raw_args, call_id
        try:
            args = json.loads(raw_args) if raw_args else {}
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        return name, args, call_id

    def _tool_message(self, call_id: str, result: dict) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result),
        }

    # -- tool dispatch ---------------------------------------------------------

    def _dispatch(self, name: str, args: dict, root: str, changed: set[str]) -> dict:
        handlers = {
            "read_file": self._tool_read_file,
            "list_dir": self._tool_list_dir,
            "write_file": self._tool_write_file,
            "edit_file": self._tool_edit_file,
            "run_command": self._tool_run_command,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}; available: read_file, list_dir, "
                             "write_file, edit_file, run_command, finish"}
        try:
            return handler(args, root, changed)
        except Exception as exc:
            return {"error": f"tool {name!r} failed: {exc}"}

    def _safe_path(self, root: str, raw: Any) -> tuple[str, None] | tuple[None, str]:
        if not isinstance(raw, str) or not raw.strip():
            return None, "a non-empty path string is required"
        real = os.path.realpath(os.path.join(root, raw))
        if real != root and not real.startswith(root + os.sep):
            return None, (
                f"path {raw!r} resolves outside the worktree; "
                f"stay inside the worktree at {root}"
            )
        # The command sandbox protects Git metadata; native file tools must
        # enforce the same boundary, including aliases resolving into .git.
        relative = os.path.relpath(real, root)
        if '.git' in relative.split(os.sep):
            return None, "Git metadata is reserved for the trusted publisher"
        return real, None

    def _tool_read_file(self, args: dict, root: str, changed: set[str]) -> dict:
        real, err = self._safe_path(root, args.get("path"))
        if err:
            return {"error": err}
        if not os.path.isfile(real):
            return {"error": f"no such file: {args.get('path')}"}
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(_READ_CAP_CHARS + 1)
        truncated = len(text) > _READ_CAP_CHARS
        return {
            "path": args.get("path"),
            "content": text[:_READ_CAP_CHARS],
            "truncated": truncated,
        }

    def _tool_list_dir(self, args: dict, root: str, changed: set[str]) -> dict:
        real, err = self._safe_path(root, args.get("path") or ".")
        if err:
            return {"error": err}
        if not os.path.isdir(real):
            return {"error": f"not a directory: {args.get('path')}"}
        entries = sorted(
            (name + os.sep if os.path.isdir(os.path.join(real, name)) else name)
            for name in os.listdir(real)
        )
        return {"path": args.get("path") or ".", "entries": entries}

    def _tool_write_file(self, args: dict, root: str, changed: set[str]) -> dict:
        real, err = self._safe_path(root, args.get("path"))
        if err:
            return {"error": err}
        content = args.get("content")
        if not isinstance(content, str):
            return {"error": "content must be a string"}
        parent = os.path.dirname(real)
        os.makedirs(parent, exist_ok=True)
        with open(real, "w", encoding="utf-8") as fh:
            fh.write(content)
        changed.add(os.path.relpath(real, root))
        return {"ok": True, "path": args.get("path"), "bytes": len(content.encode("utf-8"))}

    def _tool_edit_file(self, args: dict, root: str, changed: set[str]) -> dict:
        real, err = self._safe_path(root, args.get("path"))
        if err:
            return {"error": err}
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return {"error": "old_string and new_string must be strings"}
        if not os.path.isfile(real):
            return {"error": f"no such file: {args.get('path')}"}
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        occurrences = text.count(old_string)
        if occurrences == 0:
            return {"error": "old_string not found in file; read the file again and retry"}
        if occurrences > 1:
            return {"error": (
                f"old_string matches {occurrences} times; it must match exactly once — "
                "include more surrounding context"
            )}
        with open(real, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old_string, new_string, 1))
        changed.add(os.path.relpath(real, root))
        return {"ok": True, "path": args.get("path")}

    def _tool_run_command(self, args: dict, root: str, changed: set[str]) -> dict:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"error": "command must be a non-empty string"}
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return {"error": f"could not parse command into argv: {exc}"}
        if not argv:
            return {"error": "command must be a non-empty string"}
        code, output = self._execute(argv)
        return {"exit": code, "output": _tail(output, self._cfg.run_command_output_cap)}

    def _execute(self, argv: list[str]) -> tuple[int, str]:
        """Run untrusted source in the fleet's isolation, never on the host.

        Bubblewrap clears the child environment, hides account directories,
        removes networking, and mounts Git metadata read-only. Missing
        isolation is an error, never permission for unconfined execution.
        """
        from .fleet_checks import _execute as isolated_execute
        try:
            code, output = isolated_execute(
                self._cfg, self._active_worktree, argv, self._should_stop,
                network=False,
            )
        except (RuntimeError, OSError) as exc:
            return 126, "The isolated command could not run: " + str(exc)
        # Preserve the tool's conventional missing-executable result even
        # when exec is performed by bubblewrap rather than subprocess itself.
        if code == 1 and 'bwrap: execvp ' in output and 'No such file or directory' in output:
            return 127, f"command not found: {argv[0]}"
        return code, output

    # -- checks ----------------------------------------------------------------

    def _check_runner_for_task(self) -> CheckRunner:
        if self._check_runner is not None:
            return self._check_runner
        return self._default_check_runner

    def _default_check_runner(self, argv: list[str]) -> tuple[int, str]:
        code, output = self._execute(argv)
        return code, output

    def _run_checks(self, checks: list, should_stop=lambda: False) -> list[dict]:
        self._should_stop = should_stop
        if self._check_runner is None and checks and not should_stop():
            # Dependency downloads use only copied manifests in disposable
            # space. Model commands and repository scripts never gain network
            # access, even while their declared dependencies are prepared.
            from .fleet_checks import _prepare
            from .github_client import redact_text
            try:
                valid = all(isinstance(c, str) and shlex.split(c) for c in checks)
            except ValueError:
                valid = False  # The ordinary parser below reports the error.
            if valid:
                try:
                    problem = _prepare(self._cfg, Path(self._active_worktree), checks, should_stop)
                except Exception as exc:
                    problem = 'Dependency preparation could not finish (' + type(exc).__name__ + ').'
                if problem:
                    return [{'command': checks[0], 'exit': 1, 'passed': False,
                             'stage': 'dependencies', 'output_tail': redact_text(problem)[-4000:]}]
        runner = self._check_runner_for_task()
        recorded: list[dict] = []
        for command in checks:
            if should_stop():
                break
            if not isinstance(command, str) or not command.strip():
                recorded.append({
                    "command": command,
                    "exit": 2,
                    "passed": False,
                    "output_tail": "empty check command",
                })
                continue
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                recorded.append({
                    "command": command,
                    "exit": 2,
                    "passed": False,
                    "output_tail": f"could not parse check into argv: {exc}",
                })
                continue
            if not argv:
                recorded.append({
                    "command": command,
                    "exit": 2,
                    "passed": False,
                    "output_tail": "empty check command",
                })
                continue
            try:
                exit_code, output = runner(argv)
            except Exception as exc:
                exit_code, output = 1, f"check runner failed: {exc!r}"
            recorded.append({
                "command": command,
                "exit": exit_code,
                "passed": exit_code == 0,
                "output_tail": _tail(output or "", _OUTPUT_TAIL_CHARS),
            })
        return recorded

    # -- git bookkeeping --------------------------------------------------------

    def _git_changed_files(self, root: str) -> set[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", root, "status", "--porcelain", "-z", "--untracked-files=all"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        if proc.returncode != 0:
            return set()
        changed: set[str] = set()
        entries = iter(proc.stdout.split("\0"))
        for entry in entries:
            if not entry:
                continue
            # In -z format a rename contains destination then original as
            # separate records. Preserve whitespace and literal arrows in names.
            body = entry[3:] if len(entry) > 3 else ""
            if 'R' in entry[:2] or 'C' in entry[:2]:
                next(entries, None)
            if body:
                changed.add(body)
        return changed
