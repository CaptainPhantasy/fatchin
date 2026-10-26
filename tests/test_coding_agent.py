"""Tests for ghagent.coding_agent — tool loop, path safety, checks.

Uses a FakeModel scripting a read -> edit -> finish conversation. No network
and no real model calls; the git worktree is a real tempfile fixture.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from tests import sandbox_available

from ghagent.coding_agent import CodingAgent
from ghagent.interfaces import Config
from ghagent.model_client import ModelError, SpendLimitExceeded


class FakeModel:
    """Pops scripted chat responses; records every message list it sees."""

    def __init__(self, script):
        self.script = list(script)
        self.chats: list[list[dict]] = []
        self.calls = 0

    def chat(self, messages, tools=None):
        self.chats.append(json.loads(json.dumps(messages)))
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def calls_used(self) -> int:
        return self.calls


def tool_call(cid, name, args):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def resp(*calls, content=None):
    return {
        "content": content,
        "tool_calls": list(calls) if calls else None,
        "raw_usage": {"total_tokens": 10},
    }


@unittest.skipUnless(sandbox_available(), 'bubblewrap must be installed and usable')
class CodingAgentTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="ghagent-coding-")
        self.addCleanup(self._cleanup_dir, base)
        self.worktree = os.path.join(base, "wt")
        os.makedirs(self.worktree)

        def git(*args):
            subprocess.run(
                ["git", "-C", self.worktree, *args],
                check=True, capture_output=True, text=True,
            )

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        with open(os.path.join(self.worktree, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("line one\nline two\n")
        git("add", "-A")
        git("commit", "-m", "init")
        self.notes_path = os.path.join(self.worktree, "notes.txt")

    @staticmethod
    def _cleanup_dir(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _cfg(self, **overrides) -> Config:
        defaults = dict(
            state_dir="/tmp/ghagent-state",
            worktree_root="/tmp/ghagent-wt",
            max_tool_iterations=5,
            run_command_timeout_seconds=10,
            run_command_output_cap=2000,
        )
        defaults.update(overrides)
        return Config(**defaults)

    def _task(self, **overrides):
        task = {
            "instruction": "Fix the wording in notes.txt.",
            "worktree_path": self.worktree,
            "repo_context": "A tiny notes repository.",
            "checks": [],
        }
        task.update(overrides)
        return task

    def _tool_messages(self, chat_index):
        return [m for m in self.model.chats[chat_index] if m.get("role") == "tool"]

    # -- the scripted loop ------------------------------------------------------

    def test_read_edit_finish_loop(self):
        self.model = FakeModel([
            resp(tool_call("call_read", "read_file", {"path": "notes.txt"})),
            resp(tool_call("call_edit", "edit_file", {
                "path": "notes.txt", "old_string": "line two", "new_string": "line two (fixed)",
            })),
            resp(tool_call("call_finish", "finish", {
                "summary": "Fixed the wording.", "changed_files": ["notes.txt"],
            })),
        ])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task())

        self.assertEqual(result.error, "")
        self.assertEqual(result.summary, "Fixed the wording.")
        self.assertEqual(result.model_calls_used, 3)
        self.assertEqual(result.changed_files, ["notes.txt"])
        self.assertEqual(result.worktree_path, os.path.realpath(self.worktree))
        with open(self.notes_path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "line one\nline two (fixed)\n")

        # Tool results travel as role:"tool" messages with the call id and a
        # JSON payload; the assistant messages echo their tool_calls.
        second_round = self.model.chats[1]
        self.assertEqual(second_round[2]["role"], "assistant")
        self.assertEqual(second_round[2]["tool_calls"][0]["id"], "call_read")
        read_result = second_round[3]
        self.assertEqual(read_result["role"], "tool")
        self.assertEqual(read_result["tool_call_id"], "call_read")
        payload = json.loads(read_result["content"])
        self.assertEqual(payload["content"], "line one\nline two\n")
        self.assertIs(payload["truncated"], False)

        # chats[2] contains both tool results; the second one is the edit ack.
        edit_result = json.loads(self._tool_messages(2)[1]["content"])
        self.assertEqual(edit_result, {"ok": True, "path": "notes.txt"})

    def test_path_escape_rejected_and_reported_to_model(self):
        base = os.path.dirname(self.worktree)
        secret_path = os.path.join(base, "secret.txt")
        with open(secret_path, "w", encoding="utf-8") as fh:
            fh.write("TOP SECRET")
        self.model = FakeModel([
            resp(tool_call("c1", "read_file", {"path": "../secret.txt"})),
            resp(tool_call("c2", "read_file", {"path": "/etc/passwd"})),
            resp(tool_call("c3", "write_file", {"path": "x/../../escape.txt", "content": "nope"})),
            resp(tool_call("c4", "finish", {"summary": "gave up", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task())

        self.assertEqual(result.summary, "gave up")
        # chats[3] holds the full history including all three rejected tool
        # results; each round appends assistant+tool, so tools sit at 3, 5, 7.
        for idx in (0, 1, 2):
            payload = json.loads(self.model.chats[3][3 + 2 * idx]["content"])
            self.assertIn("error", payload)
            self.assertIn("outside the worktree", payload["error"])
            self.assertIn("stay inside", payload["error"])
        with open(secret_path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "TOP SECRET")
        self.assertFalse(os.path.exists(os.path.join(base, "escape.txt")))
        self.assertEqual(result.changed_files, [])

    # -- checks ------------------------------------------------------------------

    def test_checks_recorded_with_failing_check(self):
        self.model = FakeModel([
            resp(tool_call("c1", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task(checks=["true", "false"]))

        self.assertEqual(len(result.checks), 2)
        passing, failing = result.checks
        self.assertEqual(passing["command"], "true")
        self.assertEqual(passing["exit"], 0)
        self.assertIs(passing["passed"], True)
        self.assertIsInstance(passing["output_tail"], str)
        self.assertEqual(failing["command"], "false")
        self.assertEqual(failing["exit"], 1)
        self.assertIs(failing["passed"], False)
        # A failing check is recorded, not raised.
        self.assertEqual(result.error, "")

    def test_injected_check_runner_receives_argv_and_is_recorded(self):
        seen_argv = []

        def runner(argv):
            seen_argv.append(list(argv))
            return (3, "runner output")

        self.model = FakeModel([
            resp(tool_call("c1", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg(), check_runner=runner)
        result = agent.run(self._task(checks=["python -m pytest -q"]))

        self.assertEqual(seen_argv, [["python", "-m", "pytest", "-q"]])
        self.assertEqual(result.checks[0]["exit"], 3)
        self.assertIs(result.checks[0]["passed"], False)
        self.assertEqual(result.checks[0]["output_tail"], "runner output")

    def test_long_check_output_tail_capped_at_4000(self):
        self.model = FakeModel([
            resp(tool_call("c1", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg(), check_runner=lambda argv: (1, "x" * 9000))
        result = agent.run(self._task(checks=["flood"]))
        self.assertLessEqual(len(result.checks[0]["output_tail"]), 4000 + len("…[truncated]\n"))
        self.assertTrue(result.checks[0]["output_tail"].endswith("x"))

    # -- run_command environment hygiene ------------------------------------------

    def test_run_command_scrubs_secret_env_vars(self):
        os.environ["GHA_MODEL_API_KEY"] = "super-secret-value"
        self.addCleanup(os.environ.pop, "GHA_MODEL_API_KEY", None)
        self.model = FakeModel([
            resp(tool_call("c1", "run_command", {"command": "printenv GHA_MODEL_API_KEY"})),
            resp(tool_call("c2", "finish", {"summary": "checked env", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        agent.run(self._task())

        payload = json.loads(self._tool_messages(1)[0]["content"])
        self.assertEqual(payload["exit"], 1)  # printenv: variable absent
        self.assertEqual(payload["output"], "")
        everything = json.dumps(self.model.chats)
        self.assertNotIn("super-secret-value", everything)

    def test_run_command_reports_timeout_and_missing_binary(self):
        self.model = FakeModel([
            resp(tool_call("c1", "run_command", {"command": "definitely-not-a-real-binary"})),
            resp(tool_call("c2", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        agent.run(self._task())
        payload = json.loads(self._tool_messages(1)[0]["content"])
        self.assertEqual(payload["exit"], 127)
        self.assertIn("command not found", payload["output"])

    # -- error paths -----------------------------------------------------------------

    def test_model_error_returned_not_raised_and_checks_still_run(self):
        self.model = FakeModel([ModelError("auth broke: invalid lease")])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task(checks=["true"]))

        self.assertIn("auth broke", result.error)
        self.assertEqual(result.model_calls_used, 0)
        self.assertEqual(result.changed_files, [])
        self.assertEqual(len(result.checks), 1)
        self.assertIs(result.checks[0]["passed"], True)

    def test_spend_limit_error_returned_not_raised(self):
        self.model = FakeModel([SpendLimitExceeded("daily cap reached")])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task())
        self.assertIn("daily cap", result.error)

    def test_max_tool_iterations_exhausted_sets_error(self):
        endless = resp(tool_call("c", "read_file", {"path": "notes.txt"}))
        self.model = FakeModel([endless, endless])
        agent = CodingAgent(self.model, self._cfg(max_tool_iterations=2))
        result = agent.run(self._task(checks=["true"]))

        self.assertIn("max_tool_iterations", result.error)
        self.assertEqual(result.model_calls_used, 2)
        self.assertEqual(len(result.checks), 1)

    def test_edit_requires_exactly_one_match(self):
        self.model = FakeModel([
            resp(tool_call("c1", "edit_file", {
                "path": "notes.txt", "old_string": "absent text", "new_string": "x",
            })),
            resp(tool_call("c2", "edit_file", {
                "path": "notes.txt", "old_string": "line ", "new_string": "x",
            })),  # appears twice
            resp(tool_call("c3", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task())

        first = json.loads(self.model.chats[1][3]["content"])
        second = json.loads(self.model.chats[2][5]["content"])
        self.assertIn("not found", first["error"])
        self.assertIn("2 times", second["error"])
        self.assertEqual(result.changed_files, [])  # nothing was actually edited

    def test_unknown_tool_reported_back_to_model(self):
        self.model = FakeModel([
            resp(tool_call("c1", "rm_rf_everything", {})),
            resp(tool_call("c2", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task())
        payload = json.loads(self._tool_messages(1)[0]["content"])
        self.assertIn("unknown tool", payload["error"])
        self.assertEqual(result.error, "")

    # -- prompts and bookkeeping -------------------------------------------------------

    def test_prompts_carry_system_rules_instruction_and_checks(self):
        self.model = FakeModel([
            resp(tool_call("c1", "finish", {"summary": "done", "changed_files": []})),
        ])
        agent = CodingAgent(self.model, self._cfg())
        agent.run(self._task(checks=["true", "false"]))

        system = self.model.chats[0][0]
        user = self.model.chats[0][1]
        self.assertEqual(system["role"], "system")
        self.assertIn(".git", system["content"])
        self.assertIn("credentials", system["content"])
        self.assertEqual(user["role"], "user")
        self.assertIn("Fix the wording in notes.txt.", user["content"])
        self.assertIn("A tiny notes repository.", user["content"])
        self.assertIn("- true", user["content"])
        self.assertIn("- false", user["content"])

    def test_changed_files_union_tracked_and_git_status(self):
        self.model = FakeModel([
            resp(tool_call("c1", "write_file", {"path": "new.txt", "content": "hello"})),
            resp(tool_call("c2", "finish", {
                "summary": "done", "changed_files": ["notes.txt", "outside-declared.txt"],
            })),
        ])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run(self._task())

        self.assertEqual(result.changed_files, ["new.txt", "notes.txt", "outside-declared.txt"])
        self.assertTrue(os.path.isfile(os.path.join(self.worktree, "new.txt")))

    def test_invalid_task_returns_error_result(self):
        self.model = FakeModel([])
        agent = CodingAgent(self.model, self._cfg())
        result = agent.run({"instruction": "", "worktree_path": self.worktree, "checks": []})
        self.assertTrue(result.error)
        result = agent.run({"instruction": "x", "worktree_path": "/nonexistent-path-xyz", "checks": []})
        self.assertTrue(result.error)
        self.assertEqual(self.model.calls, 0)


if __name__ == "__main__":
    unittest.main()
