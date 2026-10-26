"""CLI subprocess acceptance against isolated filesystem and protocol fixtures.

Every command enters the real ``python -m ghagent.cli`` module in another
process. The worker, executor, model HTTP client, queue, checks, and Git are
real. A child-only sitecustomize redirects gh binary discovery to a temporary
protocol fixture, and standard Git URL rewriting directs the configured
``github.invalid`` remote to a local bare repository. Model completions use a
loopback HTTP fixture. These tests do not verify real GitHub/provider access
and cannot contact the installed worker, credentials, or production state.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import sandbox_available
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
EXACT_DESTINATION = (
    "Error: No destination folder specified. Supply an output path within your user space."
)


FAKE_GH = r'''import json, os, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit, unquote

args = sys.argv[1:]
event = {"argv": args}
if "--input" in args:
    event["input"] = sys.stdin.read()
if "--body-file" in args:
    event["body"] = Path(args[args.index("--body-file") + 1]).read_text()
with open(os.environ["CLI_E2E_GH_LOG"], "a") as handle:
    handle.write(json.dumps(event) + "\n")

def output(value):
    if "--slurp" in args:
        value = [value]
    print(json.dumps(value))
    raise SystemExit(0)

def fail(message="HTTP 404 Not Found"):
    print(message, file=sys.stderr)
    raise SystemExit(1)

def sha(branch):
    p = subprocess.run(["git", "--git-dir", os.environ["CLI_E2E_ORIGIN"],
                        "rev-parse", "--verify", "refs/heads/" + branch],
                       capture_output=True, text=True)
    if p.returncode:
        fail()
    return p.stdout.strip()

if os.environ.get("CLI_E2E_GH_AUTH") == "fail":
    fail("HTTP 401 Unauthorized; gh auth login required")
if args[:2] == ["api", "graphql"]:
    output({"data": {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
if args and args[0] == "api":
    candidates = [a for a in args[1:] if a.lstrip("/").startswith("repos/") or a in ("/user", "user")]
    if not candidates:
        fail("Unexpected fixture gh API invocation")
    path = urlsplit(candidates[-1]).path.strip("/")
    if path == "user":
        output({"login": "fixture", "id": 7})
    if path == "repos/qa/repo":
        output({"default_branch": "main"})
    if path.endswith("/rulesets"):
        output([])
    if "/contents/" in path:
        fail()
    if "/branches/" in path:
        output({"commit": {"sha": sha(unquote(path.split("/branches/", 1)[1]))}})
    if path.endswith("/actions/runs"):
        output({"workflow_runs": []})
    if path.endswith(("/comments", "/reviews")):
        output([])
    fail("Unexpected fixture API path: " + path)
if args[:2] == ["pr", "view"]:
    if os.environ.get("CLI_E2E_PR") == "missing":
        fail()
    output({"number": int(args[2]), "url": "https://github.invalid/qa/repo/pull/7",
            "state": "OPEN", "title": "Correct the answer", "body": "Set ANSWER to 2.",
            "headRefOid": sha("feature"), "headRefName": "feature", "baseRefName": "main",
            "baseRefOid": sha("main"), "isCrossRepository": False,
            "maintainerCanModify": True})
if args[:2] == ["pr", "checks"]:
    output([])
if args[:2] == ["issue", "view"]:
    output({"number": int(args[2]), "url": "https://github.invalid/qa/repo/issues/9",
            "state": "OPEN", "title": "Correct the answer", "body": "Set ANSWER to 2.",
            "updatedAt": "2026-10-19T00:00:00Z"})
if args[:2] == ["pr", "create"]:
    print("https://github.invalid/qa/repo/pull/91")
    raise SystemExit(0)
if args[:2] == ["pr", "comment"]:
    print("https://github.invalid/qa/repo/pull/7#issuecomment-9001")
    raise SystemExit(0)
fail("Unexpected fixture gh command: " + " ".join(args[:2]))
'''


@unittest.skipUnless(sandbox_available(), 'bubblewrap must be installed and usable')
class CliSubprocessAcceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gha-cli-e2e-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bootstrap = self.root / "bootstrap"
        self.bootstrap.mkdir()
        self.gh_log = self.root / "gh.jsonl"
        fake = self.bin / "gh"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_GH)
        fake.chmod(0o700)
        (self.bootstrap / "sitecustomize.py").write_text(
            "import json, os, sys\n"
            "from ghagent import github_client\n"
            "github_client.DEFAULT_GH_PATH = os.environ['CLI_E2E_GH_BINARY']\n"
            "original_kill = os.kill\n"
            "def observed_kill(pid, sig):\n"
            "    result = original_kill(pid, sig)\n"
            "    if sig and os.environ.get('CLI_E2E_SIGNAL_LOG'):\n"
            "        with open(os.environ['CLI_E2E_SIGNAL_LOG'], 'a') as handle:\n"
            "            handle.write(json.dumps({'pid': pid, 'signal': sig}) + '\\n')\n"
            "    return result\n"
            "os.kill = observed_kill\n"
            "def restrict_network(event, args):\n"
            "    if event == 'socket.connect' and isinstance(args[1], tuple):\n"
            "        if args[1][0] not in ('127.0.0.1', '::1', 'localhost'):\n"
            "            raise OSError('CLI fixture forbids non-loopback network connections')\n"
            "sys.addaudithook(restrict_network)\n"
        )
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.defpath + os.pathsep + str(Path(sys.executable).parent),
            "PYTHONPATH": str(self.bootstrap) + os.pathsep + str(PROJECT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONWARNINGS": "default::ResourceWarning",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "CLI_E2E_GH_BINARY": str(fake),
            "CLI_E2E_GH_LOG": str(self.gh_log),
            "CLI_E2E_ORIGIN": str(self.root / "origin.git"),
        }
        self.children = []
        self.model_requests = []
        self.model_status = 200
        self.model_changes = True
        self.model_started = threading.Event()
        self.model_release = threading.Event()
        self.model_release.set()
        fixture = self

        class ModelHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.model_requests.append({"path": self.path, "body": value})
                fixture.model_started.set()
                if not fixture.model_release.wait(8):
                    self.send_error(504)
                    return
                message = {"role": "assistant", "content": "OK"}
                if value.get("tools"):
                    calls = []
                    if fixture.model_changes:
                        calls.append({"id": "write", "type": "function", "function": {
                            "name": "write_file", "arguments": json.dumps({"path": "answer.py", "content": "ANSWER = 2\n"})}})
                    calls.append({"id": "finish", "type": "function", "function": {
                        "name": "finish", "arguments": json.dumps({"summary": "Corrected the answer.",
                            "changed_files": ["answer.py"] if fixture.model_changes else []})}})
                    message = {"role": "assistant", "content": None, "tool_calls": calls}
                body = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(fixture.model_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.start()
        self.addCleanup(self.cleanup_runtime)
        self.config = self.home / "config.toml"
        self.env_file = self.home / "worker.env"
        self.env_file.write_text("GHA_MODEL_API_KEY=fixture-only-key\n")
        self.env_file.chmod(0o600)
        self.write_config()
        self.prepare_repository()

    def cleanup_runtime(self):
        self.model_release.set()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def write_config(self, *, path=None, state=None, with_destination=True, checks=None, **model):
        path = path or self.config
        state = state or self.home / "state"
        checks = checks if checks is not None else [str(sys.executable) + ' -c "from answer import ANSWER; assert ANSWER == 2"']
        lines = [
            'github_host = "github.invalid"', "poll_interval_seconds = 10",
            "poll_repos = false", 'command_prefix = "/agent"', "allowed_command_users = [7]",
        ]
        if with_destination:
            lines += ["state_dir = " + json.dumps(str(state)),
                      "worktree_root = " + json.dumps(str(self.home / "worktrees"))]
        lines += ['[[repos]]', 'owner = "qa"', 'name = "repo"', 'base_branch = "main"',
                  "checks = " + json.dumps(checks), "[model]",
                  'auth_mode = "env"', "base_url = " + json.dumps(f"http://127.0.0.1:{self.server.server_port}/v1"),
                  'model_id = "fixture-model"', "max_calls_per_job = 5", "max_calls_per_day = 50",
                  "request_timeout_seconds = 10"]
        for name, value in model.items():
            lines = [line for line in lines if not line.startswith(name + " = ")]
            lines.append(name + " = " + json.dumps(value))
        path.write_text("\n".join(lines) + "\n")
        return path

    def prepare_repository(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git is required for CLI subprocess acceptance")
        self.git_binary = git
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.git("init", "--bare", "-q", "--initial-branch=main", str(self.origin))
        self.git("init", "-q", "--initial-branch=main", str(self.seed))
        self.git("-C", str(self.seed), "config", "user.name", "CLI Fixture")
        self.git("-C", str(self.seed), "config", "user.email", "fixture@invalid")
        (self.seed / "answer.py").write_text("ANSWER = 1\n")
        self.git("-C", str(self.seed), "add", "answer.py")
        self.git("-C", str(self.seed), "commit", "-qm", "Initial fixture")
        self.git("-C", str(self.seed), "push", "-q", str(self.origin), "HEAD:main", "HEAD:feature")
        self.start_sha = self.git("--git-dir", str(self.origin), "rev-parse", "main").strip()
        self.git("--git-dir", str(self.origin), "update-ref", "refs/pull/7/head", self.start_sha)
        (self.home / ".gitconfig").write_text(
            '[user]\n name = CLI Fixture\n email = fixture@invalid\n'
            '[protocol "file"]\n allow = always\n'
            f'[url "{self.origin.as_uri()}"]\n insteadOf = https://github.invalid/qa/repo.git\n'
        )

    def git(self, *args):
        result = subprocess.run([self.git_binary, *args], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def command(self, *args, config=True, timeout=30, env=None):
        actual = [sys.executable, "-m", "ghagent.cli", *args]
        if config:
            actual += ["--config", str(self.config), "--env-file", str(self.env_file)]
        return subprocess.run(actual, env={**self.env, **(env or {})}, cwd=self.home,
                              capture_output=True, text=True, timeout=timeout)

    def spawn_worker(self, config=None):
        child = subprocess.Popen(
            [sys.executable, "-m", "ghagent.cli", "run", "--config", str(config or self.config),
             "--env-file", str(self.env_file)], env=self.env, cwd=self.home,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def wait_for(self, predicate, child=None, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if child and child.poll() is not None:
                out, err = child.communicate()
                self.fail(f"Worker exited before the condition: {child.returncode}\n{out}\n{err}")
            time.sleep(0.02)
        self.fail("The expected isolated worker state was not reached in time")

    def health(self, state=None):
        try:
            return json.loads(((state or self.home / "state") / "health.json").read_text())
        except FileNotFoundError:
            return {}

    def jobs(self):
        path = self.home / "state" / "jobs.json"
        return list(json.loads(path.read_text())["jobs"].values()) if path.exists() else []

    def gh_events(self):
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()] if self.gh_log.exists() else []

    def assert_clean_cli(self, result, expected=0):
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("ResourceWarning", result.stderr)
        self.assertNotIn("fixture-only-key", result.stdout + result.stderr)

    def queue_job(self):
        from ghagent.interfaces import Job, JobKind
        from ghagent.queue import JobQueue
        job = Job("job_cli_process", JobKind.FIX_PR, "qa/repo", 7, 0, 7, head_sha=self.start_sha,
                  dedup_key="cli-subprocess-fixture")
        JobQueue(str(self.home / "state")).add(job)
        return job

    def test_help_for_every_command_and_unknown_command(self):
        for command in ([], ["run"], ["once"], ["status"], ["stop"], ["provider-check"], ["init-config"]):
            with self.subTest(command=command):
                result = self.command(*command, "--help", config=False)
                self.assert_clean_cli(result)
                self.assertIn("usage:", result.stdout)
        self.assertEqual(self.command("no-such-command", config=False).returncode, 2)
        self.assertEqual(self.model_requests, [])
        self.assertEqual(self.gh_events(), [])

    def test_init_config_creates_once_and_preserves_existing_content(self):
        dest = self.home / "new folder" / "worker.toml"
        first = self.command("init-config", str(dest), config=False)
        self.assert_clean_cli(first)
        original = dest.read_bytes()
        second = self.command("init-config", str(dest), config=False)
        self.assert_clean_cli(second, 1)
        self.assertIn("refusing to overwrite", second.stderr)
        self.assertEqual(dest.read_bytes(), original)

    def test_missing_destination_is_exact_exit_two_before_connections(self):
        self.write_config(with_destination=False)
        for command in (("run",), ("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")):
            with self.subTest(command=command):
                result = self.command(*command)
                self.assert_clean_cli(result, 2)
                self.assertEqual(result.stdout.strip(), EXACT_DESTINATION)
        self.assertEqual(self.model_requests, [])
        self.assertEqual(self.gh_events(), [])

    def test_missing_configuration_is_friendly_for_read_and_write_commands(self):
        self.config.unlink()
        for command in (("run",), ("status",), ("stop",), ("provider-check",),
                        ("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")):
            with self.subTest(command=command):
                result = self.command(*command)
                self.assert_clean_cli(result, 1)
                self.assertIn("not found", result.stderr)

    def test_invalid_toml_is_friendly_for_all_non_initializer_commands(self):
        self.config.write_text("this is not valid TOML [\n")
        for command in (("run",), ("status",), ("stop",), ("provider-check",),
                        ("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")):
            with self.subTest(command=command):
                result = self.command(*command)
                self.assert_clean_cli(result, 1)
                self.assertTrue(result.stderr.strip())
        self.assertEqual(self.model_requests, [])

    def test_run_and_once_missing_model_auth_are_exit_five_without_connections(self):
        self.env_file.write_text("")
        for command in (("run",), ("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")):
            with self.subTest(command=command):
                result = self.command(*command)
                self.assert_clean_cli(result, 5)
                self.assertIn("GHA_MODEL_API_KEY", result.stderr)
        self.assertEqual(self.model_requests, [])
        self.assertEqual(self.gh_events(), [])

    def test_run_and_once_github_auth_failure_is_exit_four_without_model_completion(self):
        for command in (("run",),
                        ("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7"),
                        ("once", "--repo", "qa/repo", "--kind", "implement-issue", "--number", "9")):
            with self.subTest(command=command):
                result = self.command(*command, env={"CLI_E2E_GH_AUTH": "fail"})
                self.assert_clean_cli(result, 4)
                self.assertIn("GitHub authentication unavailable", result.stderr)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.model_requests, [])

    def test_provider_check_uses_real_http_client_once_and_persists_reservation(self):
        result = self.command("provider-check")
        self.assert_clean_cli(result)
        self.assertIn("reply: OK", result.stdout)
        self.assertEqual(len(self.model_requests), 1)
        self.assertEqual(self.model_requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(self.model_requests[0]["body"]["max_tokens"], 512)
        ledger = json.loads((self.home / "state" / "model-call-counts.json").read_text())
        self.assertEqual(ledger["count"], 1)

    def test_provider_rejection_and_retryable_failure_make_only_one_attempt_each(self):
        for status in (401, 503):
            with self.subTest(status=status):
                self.model_status = status
                before = len(self.model_requests)
                result = self.command("provider-check")
                self.assert_clean_cli(result, 1)
                self.assertIn("failed", result.stderr)
                self.assertEqual(len(self.model_requests) - before, 1)

    def test_provider_missing_auth_is_exit_five_without_http(self):
        self.env_file.write_text("")
        result = self.command("provider-check")
        self.assert_clean_cli(result, 5)
        self.assertEqual(self.model_requests, [])

    def test_status_reads_real_queue_and_preserves_corrupt_files(self):
        self.queue_job()
        result = self.command("status")
        self.assert_clean_cli(result)
        self.assertIn("job_cli_process", result.stdout)
        self.assertIn("received=1", result.stdout)
        state = self.home / "state" / "jobs.json"
        state.write_text("broken queue data")
        corrupt = self.command("status")
        self.assert_clean_cli(corrupt)
        self.assertIn("unreadable", corrupt.stdout)
        self.assertEqual(state.read_text(), "broken queue data")

    def test_run_single_instance_lock_and_stop_clean_shutdown(self):
        worker = self.spawn_worker()
        self.wait_for(lambda: self.health().get("state") == "running", worker)
        duplicate = self.command("run")
        self.assert_clean_cli(duplicate, 3)
        self.assertIn("another gha-worker", duplicate.stderr)
        simultaneous_once = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")
        self.assert_clean_cli(simultaneous_once, 3)
        self.assertIn("another gha-worker", simultaneous_once.stderr)
        stopped = self.command("stop")
        self.assert_clean_cli(stopped)
        out, err = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, out + err)
        self.assertNotIn("ResourceWarning", err)
        self.assertEqual(self.health()["state"], "stopped")
        self.assertEqual(self.model_requests, [])

    def test_once_holds_worker_lock_until_work_finishes(self):
        self.model_release.clear()
        once = subprocess.Popen(
            [sys.executable, "-m", "ghagent.cli", "once", "--repo", "qa/repo", "--kind", "fix-pr",
             "--number", "7", "--config", str(self.config), "--env-file", str(self.env_file)],
            env=self.env, cwd=self.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(once)
        self.assertTrue(self.model_started.wait(8), "The one-shot task did not reach the HTTP fixture")
        for command in (("run",), ("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")):
            with self.subTest(command=command):
                result = self.command(*command)
                self.assert_clean_cli(result, 3)
        self.assertEqual(len(self.model_requests), 1)
        self.model_release.set()
        out, err = once.communicate(timeout=10)
        self.assertEqual(once.returncode, 0, out + err)
        self.assertNotIn("ResourceWarning", err)
        self.assertEqual(self.jobs()[0]["state"], "done")
        self.assertEqual(self.git("--git-dir", str(self.origin), "show", "feature:answer.py"), "ANSWER = 2\n")

    def test_once_sigterm_during_http_response_stops_without_writing(self):
        self.model_release.clear()
        once = subprocess.Popen(
            [sys.executable, "-m", "ghagent.cli", "once", "--repo", "qa/repo", "--kind", "fix-pr",
             "--number", "7", "--config", str(self.config), "--env-file", str(self.env_file)],
            env=self.env, cwd=self.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(once)
        self.assertTrue(self.model_started.wait(8), "The one-shot task did not reach the HTTP fixture")
        os.kill(once.pid, signal.SIGTERM)
        self.model_release.set()
        out, err = once.communicate(timeout=10)
        self.assertEqual(once.returncode, 0, out + err)
        self.assertNotIn("ResourceWarning", err)
        self.assertIn("-> stopped", out)
        self.assertEqual(self.jobs()[0]["state"], "stopped")
        self.assertEqual(self.git("--git-dir", str(self.origin), "show", "feature:answer.py"), "ANSWER = 1\n")
        self.assertFalse(any(e["argv"][:2] in (["pr", "create"], ["pr", "comment"]) for e in self.gh_events()))

    def test_stop_refuses_unrelated_process_and_preserves_it(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        state = self.home / "state"
        state.mkdir()
        (state / "health.json").write_text(json.dumps({"pid": process.pid, "state": "running"}))
        result = self.command("stop")
        self.assert_clean_cli(result, 1)
        self.assertIn("refusing", result.stderr)
        self.assertIsNone(process.poll())

    def test_stop_refuses_misleading_ghagent_text_in_an_unrelated_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "ghagent-unrelated-data"],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        state = self.home / "state"
        state.mkdir()
        (state / "health.json").write_text(json.dumps({"pid": process.pid, "state": "running"}))
        result = self.command("stop")
        self.assert_clean_cli(result, 1)
        self.assertIsNone(process.poll(), "An unrelated process with a matching word must not be terminated")

    def test_stop_refuses_a_worker_started_with_a_different_configuration(self):
        other_state = self.home / "other-state"
        other_config = self.write_config(path=self.home / "other.toml", state=other_state)
        process = self.spawn_worker(other_config)
        self.wait_for(lambda: self.health(other_state).get("state") == "running", process)
        state = self.home / "state"
        state.mkdir()
        (state / "health.json").write_text(json.dumps({"pid": process.pid, "state": "running"}))
        result = self.command("stop")
        self.assert_clean_cli(result, 1)
        self.assertIsNone(process.poll(), "A stale PID must not stop the worker for a different config")

    def test_stop_missing_or_corrupt_pid_does_not_modify_state(self):
        state = self.home / "state"
        state.mkdir()
        path = state / "health.json"
        for content in ("{}", "not json", '{"pid": 0}'):
            with self.subTest(content=content):
                path.write_text(content)
                result = self.command("stop")
                self.assert_clean_cli(result, 1)
                self.assertEqual(path.read_text(), content)

    def test_once_implements_issue_with_real_git_checks_and_local_push(self):
        result = self.command("once", "--repo", "qa/repo", "--kind", "implement-issue", "--number", "9")
        self.assert_clean_cli(result)
        self.assertIn("-> done", result.stdout)
        job, = self.jobs()
        self.assertEqual(job["state"], "done")
        self.assertEqual(len(job["produced_commits"]), 1)
        self.assertTrue(job["notes"]["checks"][0]["passed"])
        self.assertEqual(self.git("--git-dir", str(self.origin), "show", "ghagent/issue-9:answer.py"), "ANSWER = 2\n")
        creates = [e for e in self.gh_events() if e["argv"][:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("--base", creates[0]["argv"])

    def test_once_fixes_pr_without_creating_another_pr(self):
        result = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")
        self.assert_clean_cli(result)
        self.assertEqual(self.git("--git-dir", str(self.origin), "show", "feature:answer.py"), "ANSWER = 2\n")
        self.assertFalse(any(e["argv"][:2] == ["pr", "create"] for e in self.gh_events()))
        self.assertEqual(self.jobs()[0]["state"], "done")

    def test_failed_check_blocks_local_remote_push_and_is_retryable(self):
        self.write_config(checks=[str(sys.executable) + ' -c "raise SystemExit(7)"'])
        result = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")
        self.assert_clean_cli(result, 1)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "feature").strip(), self.start_sha)
        self.assertIn("verification failed", self.jobs()[0]["error"])
        self.assertFalse(any(e["argv"][:2] in (["pr", "create"], ["pr", "comment"]) for e in self.gh_events()))
        self.write_config()
        retried = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")
        self.assert_clean_cli(retried)
        self.assertEqual(sorted(j["state"] for j in self.jobs()), ["done", "failed"])

    def test_once_duplicate_completed_head_does_not_repeat_the_model_or_job(self):
        self.model_changes = False
        self.write_config(checks=[str(sys.executable) + ' -c "print(123)"'])
        first = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")
        self.assert_clean_cli(first)
        before = len(self.model_requests)
        second = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7")
        self.assert_clean_cli(second)
        self.assertIn("identical job already exists", second.stdout)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(self.model_requests), before)

    def test_once_invalid_inputs_and_wrong_target_do_not_call_model(self):
        for extra in (("--repo", "missing-slash", "--number", "7"),
                      ("--repo", "qa/repo", "--number", "0")):
            with self.subTest(extra=extra):
                result = self.command("once", "--kind", "fix-pr", *extra)
                self.assert_clean_cli(result, 1)
        missing = self.command("once", "--repo", "qa/repo", "--kind", "fix-pr", "--number", "7",
                               env={"CLI_E2E_PR": "missing"})
        self.assert_clean_cli(missing, 1)
        self.assertIn("not a pull request", missing.stderr)
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.model_requests, [])

    def test_run_drains_real_queue_and_persists_finished_work(self):
        self.queue_job()
        process = self.spawn_worker()
        self.wait_for(lambda: self.jobs() and self.jobs()[0]["state"] == "done", process)
        self.assertEqual(self.git("--git-dir", str(self.origin), "show", "feature:answer.py"), "ANSWER = 2\n")
        result = self.command("stop")
        self.assert_clean_cli(result)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, out + err)
        self.assertNotIn("ResourceWarning", err)
        self.assertEqual(self.health()["state"], "stopped")

    def test_run_stop_during_http_response_never_executes_returned_write_tool(self):
        self.queue_job()
        self.model_release.clear()
        process = self.spawn_worker()
        self.assertTrue(self.model_started.wait(8), "The loopback model fixture was not reached")
        signal_log = self.root / "delivered-signals.jsonl"
        stop = subprocess.Popen(
            [sys.executable, "-m", "ghagent.cli", "stop", "--config", str(self.config), "--env-file", str(self.env_file)],
            env={**self.env, "CLI_E2E_SIGNAL_LOG": str(signal_log)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(stop)
        # Observe successful delivery of the real SIGTERM before releasing the
        # HTTP response. Starting the stop subprocess alone is insufficient:
        # Python imports may still be running when the response is released.
        self.wait_for(lambda: signal_log.exists() and
                      {"pid": process.pid, "signal": signal.SIGTERM} in
                      [json.loads(line) for line in signal_log.read_text().splitlines()], stop)
        self.model_release.set()
        out, err = stop.communicate(timeout=10)
        self.assertEqual(stop.returncode, 0, out + err)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, out + err)
        self.assertEqual(self.jobs()[0]["state"], "stopped")
        self.assertEqual(self.git("--git-dir", str(self.origin), "show", "feature:answer.py"), "ANSWER = 1\n")
        self.assertFalse(any(e["argv"][:2] in (["pr", "create"], ["pr", "comment"]) for e in self.gh_events()))


if __name__ == "__main__":
    unittest.main()
