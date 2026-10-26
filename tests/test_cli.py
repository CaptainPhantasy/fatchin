"""Unit tests for the CLI: exit codes 0/1/2/3/4/5, the exact destination
message, provider-check refusing without auth (and never leaking tokens),
status/stop from saved state, and the python3 -m ghagent.cli entry point.
Network-free: daemon components and the model client are injected fakes."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghagent import cli, worker as worker_mod
from ghagent.interfaces import Job, JobKind, JobState
from test_worker_loop import FakeExecutor, FakeGh, FakeQueue

EXACT_DESTINATION = (
    "Error: No destination folder specified. "
    "Supply an output path within your user space."
)


class FakeModelClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return {"content": "OK", "tool_calls": None}

    @property
    def calls_used(self):
        return len(self.calls)


class FakeWorker:
    def __init__(self, behavior):
        self.stop_event = threading.Event()
        self.behavior = behavior
        self.started = False

    def startup(self):
        if self.behavior == "already-running":
            raise worker_mod.WorkerAlreadyRunning("another daemon holds the lock")
        if self.behavior == "no-github-auth":
            raise worker_mod.GitHubAuthUnavailable("GitHub authentication unavailable")
        if self.behavior == "no-model-auth":
            raise worker_mod.ProviderAuthMissing(
                "MCP_SECRET_BROKER_ADMIN_TOKEN is not set")
        self.started = True

    def run_forever(self):
        self.stop_event.set()


def write_config(tmp, *, with_state=True, auth_mode="broker-lease"):
    lines = [
        "poll_interval_seconds = 1",
        'command_prefix = "/agent"',
        "allowed_command_users = [7]",
    ]
    if with_state:
        lines += [f'state_dir = "{tmp}/state"', f'worktree_root = "{tmp}/wt"']
    lines += [
        "[[repos]]",
        'owner = "o"',
        'name = "r"',
        "[model]",
        f'auth_mode = "{auth_mode}"',
        'base_url = "http://127.0.0.1:8080/v1"',
        'model_id = "your-model"',
    ]
    path = Path(tmp) / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_main(self, argv, **factories):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv, **factories)
        return code, out.getvalue(), err.getvalue()

    def clear_env_vars(self, *names):
        saved = {}
        for name in names:
            if name in os.environ:
                saved[name] = os.environ.pop(name)
        self.addCleanup(os.environ.update, saved)

    def preserve_signal_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            old = signal.getsignal(sig)
            self.addCleanup(signal.signal, sig, old)

    def must_not_build(self, *a, **k):
        raise AssertionError("daemon/model factory must not run in this path")

    def reap_child(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


class RunExitCodesTest(CliTestBase):
    def test_destination_contract_exact_message_exit_2(self):
        cfg = write_config(self.tmp, with_state=False)
        code, out, err = self.run_main(
            ["run", "-c", cfg], worker_factory=self.must_not_build)
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), EXACT_DESTINATION)
        self.assertEqual(cli.DESTINATION_MESSAGE.strip(), EXACT_DESTINATION)

    def test_run_exit_codes(self):
        cases = {
            "ok": 0,
            "already-running": 3,
            "no-github-auth": 4,
            "no-model-auth": 5,
        }
        for behavior, expected in cases.items():
            with self.subTest(behavior=behavior):
                cfg = write_config(self.tmp)
                self.preserve_signal_handlers()
                code, out, err = self.run_main(
                    ["run", "-c", cfg],
                    worker_factory=lambda c, e, b=behavior: FakeWorker(b),
                )
                self.assertEqual(code, expected)
                if expected != 0:
                    self.assertTrue(err.strip(), "expected an error message")

    def test_run_missing_config_is_exit_1(self):
        code, out, err = self.run_main(
            ["run", "-c", str(self.tmp / "nope.toml")],
            worker_factory=self.must_not_build)
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class ProviderCheckTest(CliTestBase):
    def test_refuses_without_auth_and_never_calls_model(self):
        self.clear_env_vars("MCP_SECRET_BROKER_ADMIN_TOKEN",
                            "GHA_MODEL_API_KEY")
        cfg = write_config(self.tmp, with_state=False)
        # Pin an EMPTY env file: the host may legitimately have a populated
        # default env file, and this test must stay hermetic.
        empty_env = Path(self.tmp) / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        calls = []

        def factory(*a, **k):
            calls.append(1)
            raise AssertionError("no live call may happen without auth")

        code, out, err = self.run_main(
            ["provider-check", "-c", cfg, "--env-file", str(empty_env)],
            model_client_factory=factory)
        self.assertEqual(code, 5)
        self.assertEqual(calls, [])
        self.assertIn("MCP_SECRET_BROKER_ADMIN_TOKEN", err)

    def test_refuses_env_mode_without_key(self):
        self.clear_env_vars("MCP_SECRET_BROKER_ADMIN_TOKEN",
                            "GHA_MODEL_API_KEY")
        cfg = write_config(self.tmp, with_state=False, auth_mode="env")
        empty_env = Path(self.tmp) / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        code, out, err = self.run_main(
            ["provider-check", "-c", cfg, "--env-file", str(empty_env)],
            model_client_factory=self.must_not_build)
        self.assertEqual(code, 5)
        self.assertIn("GHA_MODEL_API_KEY", err)

    def test_success_prints_endpoint_model_latency_never_token(self):
        self.clear_env_vars("MCP_SECRET_BROKER_ADMIN_TOKEN",
                            "GHA_MODEL_API_KEY")
        cfg = write_config(self.tmp, with_state=True)
        env_file = Path(self.tmp) / "env"
        env_file.write_text(
            'MCP_SECRET_BROKER_ADMIN_TOKEN="super-secret-token-value"\n',
            encoding="utf-8")
        client = FakeModelClient()
        seen_cfg = []

        def factory(model_cfg, env):
            seen_cfg.append(model_cfg)
            self.assertEqual(
                env.get("MCP_SECRET_BROKER_ADMIN_TOKEN"),
                "super-secret-token-value")
            return client

        code, out, err = self.run_main(
            ["provider-check", "-c", cfg, "--env-file", str(env_file)],
            model_client_factory=factory)
        self.assertEqual(code, 0)
        self.assertIn("endpoint: http://127.0.0.1:8080/v1", out)
        self.assertIn("model: your-model", out)
        self.assertIn("latency:", out)
        self.assertIn("reply: OK", out)
        self.assertNotIn("super-secret-token-value", out + err)
        self.assertEqual(len(client.calls), 1)
        messages, tools = client.calls[0]
        self.assertEqual(messages, [{"role": "user", "content": "Reply with OK"}])
        self.assertEqual(seen_cfg[0].max_tokens, 512)  # tiny probe call
        self.addCleanup(os.environ.pop, "MCP_SECRET_BROKER_ADMIN_TOKEN", None)


class StatusStopTest(CliTestBase):
    def test_status_without_state(self):
        cfg = write_config(self.tmp, with_state=True)
        code, out, err = self.run_main(["status", "-c", cfg])
        self.assertEqual(code, 0)
        self.assertIn("no state yet", out)

    def test_status_prints_saved_state_without_network(self):
        cfg = write_config(self.tmp, with_state=True)
        state = Path(self.tmp) / "state"
        state.mkdir()
        (state / "health.json").write_text(json.dumps({
            "last_poll_iso": "2026-10-17T10:00:00+00:00",
            "jobs_open": 1, "jobs_done_today": 2, "model_calls_today": 37,
            "pid": 424242, "state": "running"}), encoding="utf-8")
        (state / "watermarks.json").write_text(
            json.dumps({"o/r": "2026-10-17T09:00:00Z"}), encoding="utf-8")
        job = Job(job_id="job_x", kind=JobKind.FIX_PR, repo_full_name="o/r",
                  number=5, command_comment_id=9, requested_by=7,
                  state=JobState.DONE)
        (state / "jobs.json").write_text(
            json.dumps([json.loads(job.to_json())]), encoding="utf-8")
        code, out, err = self.run_main(["status", "-c", cfg])
        self.assertEqual(code, 0)
        self.assertIn('"state": "running"', out)
        self.assertIn("job_x", out)
        self.assertIn("o/r", out)

    def test_stop_without_daemon(self):
        cfg = write_config(self.tmp, with_state=True)
        (Path(self.tmp) / "state").mkdir()
        code, out, err = self.run_main(["stop", "-c", cfg])
        self.assertEqual(code, 1)
        self.assertIn("no running gha-worker", out)

    def test_status_reads_actual_keyed_queue_schema(self):
        from ghagent.queue import JobQueue
        cfg = write_config(self.tmp, with_state=True)
        state = Path(self.tmp) / 'state'
        queue = JobQueue(str(state))
        queue.add(Job('job_real', JobKind.FIX_PR, 'o/r', 5, 9, 7, state=JobState.DONE))
        code, out, err = self.run_main(['status', '-c', cfg])
        self.assertEqual(code, 0)
        self.assertIn('job_real', out)
        self.assertIn('done=1', out)
        self.assertNotIn('could not be interpreted', out)

    def test_stop_signals_the_recorded_pid(self):
        cfg = write_config(self.tmp, with_state=True)
        state = Path(self.tmp) / "state"
        state.mkdir()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             "ghagent-simulated-worker"])
        self.addCleanup(self.reap_child, proc)
        (state / "health.json").write_text(
            json.dumps({"pid": proc.pid, "state": "running"}),
            encoding="utf-8")
        # Process identity is exercised with real daemons in test_cli_e2e.
        with patch.object(cli, '_worker_process_matches', return_value=True) as matches:
            code, out, err = self.run_main(["stop", "-c", cfg])
        matches.assert_called_once_with(proc.pid, cfg)
        self.assertEqual(code, 0)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)

    def test_stop_refuses_unrelated_pid(self):
        cfg = write_config(self.tmp, with_state=True)
        state = Path(self.tmp) / "state"
        state.mkdir()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.reap_child, proc)
        (state / "health.json").write_text(
            json.dumps({"pid": proc.pid, "state": "running"}),
            encoding="utf-8")
        code, out, err = self.run_main(["stop", "-c", cfg])
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        self.assertNotEqual(proc.poll(), -signal.SIGTERM)


class OnceTest(CliTestBase):
    def _once(self, extra_args, gh=None, executor=None, queue=None):
        cfg = write_config(self.tmp, with_state=True)
        gh = gh or FakeGh()
        queue = queue or FakeQueue()
        executor = executor or FakeExecutor()
        code, out, err = self.run_main(
            ["once", "-c", cfg, "--repo", "o/r"] + extra_args,
            component_factory=lambda c, e: (gh, queue, executor))
        return code, out, err, queue, executor

    def test_fix_pr_happy_path(self):
        gh = FakeGh(prs={("o/r", 5): {"headRefOid": "abc123"}})
        code, out, err, queue, executor = self._once(
            ["--kind", "fix-pr", "--number", "5"], gh=gh)
        self.assertEqual(code, 0)
        self.assertEqual(executor.executed, [queue.all()[0].job_id])
        self.assertEqual(queue.all()[0].state, JobState.DONE)
        self.assertEqual(queue.all()[0].dedup_key, "fix-pr:o/r:5:abc123")
        self.assertIn("-> done", out)

    def test_implement_issue_happy_path(self):
        code, out, err, queue, executor = self._once(
            ["--kind", "implement-issue", "--number", "9"])
        self.assertEqual(code, 0)
        job = queue.all()[0]
        self.assertEqual(job.kind, JobKind.IMPLEMENT_ISSUE)
        self.assertEqual(job.number, 9)

    def test_failed_job_is_exit_1_and_persisted(self):
        executor = FakeExecutor()
        executor.fail = True
        code, out, err, queue, _ = self._once(
            ["--kind", "implement-issue", "--number", "9"], executor=executor)
        self.assertEqual(code, 1)
        self.assertEqual(queue.all()[0].state, JobState.FAILED)
        self.assertIn("boom", out)

    def test_fix_pr_on_issue_refused(self):
        code, out, err, queue, _ = self._once(
            ["--kind", "fix-pr", "--number", "6"])  # no PR at #6
        self.assertEqual(code, 1)
        self.assertIn("not a pull request", err)
        self.assertEqual(queue.all(), [])

    def test_bad_repo_format_refused(self):
        cfg = write_config(self.tmp, with_state=True)
        code, out, err = self.run_main(
            ["once", "-c", cfg, "--repo", "noslash", "--kind", "fix-pr",
             "--number", "5"],
            component_factory=self.must_not_build)
        self.assertEqual(code, 1)
        self.assertIn("OWNER/REPO", err)


class InitConfigAndEntryTest(CliTestBase):
    def test_init_config_writes_loadable_file_and_refuses_overwrite(self):
        dest = Path(self.tmp) / "sub" / "config.toml"
        code, out, err = self.run_main(["init-config", str(dest)])
        self.assertEqual(code, 0)
        self.assertTrue(dest.exists())
        from ghagent.config_loader import load_config
        cfg = load_config(dest)  # placeholders must still load
        self.assertEqual(cfg.model.model_id, "your-model")
        code, out, err = self.run_main(["init-config", str(dest)])
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", err)

    def test_module_entry_point(self):
        repo_root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": "src"}
        proc = subprocess.run(
            [sys.executable, "-m", "ghagent.cli", "status", "-c",
             str(self.tmp / "missing.toml")],
            cwd=repo_root, env=env, capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not found", proc.stderr)
        proc2 = subprocess.run(
            [sys.executable, "-m", "ghagent.cli", "init-config",
             str(self.tmp / "entry.toml")],
            cwd=repo_root, env=env, capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc2.returncode, 0)
        self.assertTrue((self.tmp / "entry.toml").exists())


if __name__ == "__main__":
    unittest.main()


class ProviderSwitchTest(CliTestBase):
    def test_switch_writes_config_and_keeps_a_receipt(self):
        cfg = write_config(self.tmp)
        env = Path(self.tmp) / 'env'
        env.write_text('MCP_SECRET_BROKER_ADMIN_TOKEN="t"\n')
        client = FakeModelClient()
        code, out, err = self.run_main(
            ['provider-switch', '-c', cfg, '--env-file', str(env),
             '--base-url', 'https://new.example/v1', '--model-id', 'model-b'],
            model_client_factory=lambda m, e: client)
        self.assertEqual(code, 0, err)
        text = Path(cfg).read_text()
        self.assertIn('model-b', text)
        self.assertIn('https://new.example/v1', text)
        receipts = Path(self.tmp) / 'state' / 'provider-receipts.jsonl'
        line = json.loads(receipts.read_text().splitlines()[0])
        self.assertEqual(line['result'], 'PASS')
        self.assertEqual(line['model_id'], 'model-b')
        self.assertEqual(line['auth_mode'], 'broker-lease')
        self.assertEqual(len(list(Path(cfg).parent.glob('config.toml.bak-switch-*'))), 1)
        self.assertEqual(len(client.calls), 1)

    def test_failed_probe_restores_the_previous_settings(self):
        cfg = write_config(self.tmp)
        before = Path(cfg).read_text()
        env = Path(self.tmp) / 'env'
        env.write_text('MCP_SECRET_BROKER_ADMIN_TOKEN="t"\n')

        def broken(m, e):
            raise RuntimeError('endpoint refused the request')

        code, out, err = self.run_main(
            ['provider-switch', '-c', cfg, '--env-file', str(env),
             '--model-id', 'model-x'],
            model_client_factory=broken)
        self.assertEqual(code, 1)
        self.assertEqual(Path(cfg).read_text(), before)  # previous settings restored
        self.assertIn('restored', err)
        self.assertFalse((Path(self.tmp) / 'state' / 'provider-receipts.jsonl').exists())
