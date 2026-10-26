"""Task cancellation and durable call totals through the real agent/client.

Only the model HTTP transport and repository checkout boundary are local
fixtures. No credential files, external services, or remote writes are used.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ghagent.coding_agent import CodingAgent
from ghagent.control_store import AuthorityStore
from ghagent.executor import JobExecutor
from ghagent.interfaces import Config, Job, JobKind, JobState, ModelConfig
from ghagent.model_client import ModelClient
from ghagent.queue import JobQueue
from ghagent.tasks import triage_issues
from ghagent.wiring import CodingAgentRunner


TASK_KINDS = (JobKind.EXPLAIN_REPO, JobKind.TRIAGE_ISSUES)


def tool(name, **arguments):
    return {"id": name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def completed(kind):
    calls = []
    if kind is JobKind.TRIAGE_ISSUES:
        calls.append(tool("write_file", path=".gha-drafts/problem.md",
                          content="A concrete problem\nSteps and expected result.\n"))
    calls.append(tool("finish", summary="The task is complete.", changed_files=[]))
    return message(*calls)


class LocalTransport:
    def __init__(self, replies, on_request=None):
        self.replies = iter(replies)
        self.on_request = on_request
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        if self.on_request is not None:
            self.on_request()
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps({"choices": [{"message": reply}]}).encode())


class LocalCheckouts:
    def __init__(self):
        self.path = None

    def add_worktree(self, path, base, repo):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        return str(self.path)

    def remove_worktree(self, path):
        pass  # TemporaryDirectory owns this fixture's scratch files.

    def prune(self):
        pass

    def current_sha(self, path):
        return "a" * 40


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config(
            state_dir=str(self.root / "state"),
            worktree_root=str(self.root / "scratch"),
            model=ModelConfig(base_url="https://model.example.invalid/v1",
                              model_id="fixture", max_calls_per_job=3),
        )
        self.queue = JobQueue(self.cfg.state_dir)
        self.counter = self.root / "counts.json"

    def job(self, kind, suffix=""):
        job = Job("job_saved_" + kind.value.replace("-", "_") + suffix,
                  kind, "owner/repo", 0, 0, 1,
                  notes={"values": {"words": "The Save button does nothing."}})
        self.queue.add(job)
        return job

    def executor(self, transport):
        model = ModelClient(self.cfg.model, lambda: "fixture",
                            day_counter_path=str(self.counter), urlopen=transport)
        checkouts = LocalCheckouts()
        executor = JobExecutor(
            object(), self.queue, self.cfg,
            lambda wt, checks: CodingAgentRunner(model, self.cfg, wt, checks),
            worktree_ops=checkouts)
        return executor, checkouts

    def ledger(self):
        return json.loads(self.counter.read_text())

    def test_queue_stop_during_response_skips_returned_tools_and_next_request(self):
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                job = self.job(kind)
                transport = LocalTransport([
                    message(tool("write_file", path="must-not-exist.txt",
                                 content="This action must be skipped.")),
                    completed(kind),
                ], lambda: self.queue.stop_job(job.job_id))
                executor, checkouts = self.executor(transport)
                returned = executor.execute(job)
                self.assertEqual(transport.calls, 1)
                self.assertFalse((checkouts.path / "must-not-exist.txt").exists())
                self.assertEqual(returned.state, JobState.STOPPED)
                self.assertEqual(returned.model_calls_used, 1)
                self.assertEqual(self.ledger()["jobs"][job.job_id], 1)

    def test_global_stop_during_response_skips_returned_tools_and_next_request(self):
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                job = self.job(kind)
                stop = threading.Event()
                transport = LocalTransport([
                    message(tool("write_file", path="must-not-exist.txt",
                                 content="This action must be skipped.")),
                    completed(kind),
                ], stop.set)
                executor, checkouts = self.executor(transport)
                executor.stop_event = stop
                returned = executor.execute(job)
                self.assertEqual(transport.calls, 1)
                self.assertFalse((checkouts.path / "must-not-exist.txt").exists())
                self.assertEqual(returned.state, JobState.STOPPED)
                self.assertEqual(returned.model_calls_used, 1)

    def test_stop_between_returned_tools_skips_the_next_tool(self):
        original_dispatch = CodingAgent._dispatch
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                job = self.job(kind)
                transport = LocalTransport([
                    message(tool("write_file", path="first.txt", content="First."),
                            tool("write_file", path="second.txt", content="Second.")),
                    completed(kind),
                ])
                executor, checkouts = self.executor(transport)

                def stop_after_tool(agent, *args):
                    result = original_dispatch(agent, *args)
                    self.queue.stop_job(job.job_id)
                    return result

                with patch.object(CodingAgent, "_dispatch", stop_after_tool):
                    returned = executor.execute(job)
                self.assertEqual(transport.calls, 1)
                self.assertTrue((checkouts.path / "first.txt").exists())
                self.assertFalse((checkouts.path / "second.txt").exists())
                self.assertEqual(returned.state, JobState.STOPPED)

    def test_real_saved_id_owns_the_persistent_model_reservation(self):
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                job = self.job(kind)
                transport = LocalTransport([completed(kind)])
                executor, _ = self.executor(transport)
                returned = executor.execute(job)
                expected_state = (JobState.DONE if kind is JobKind.EXPLAIN_REPO
                                  else JobState.WAITING_ON_YOU)
                self.assertEqual(returned.state, expected_state, returned.error)
                self.assertEqual(returned.model_calls_used, 1)
                self.assertEqual(self.ledger()["job_scope"], job.job_id)
                self.assertEqual(self.ledger()["jobs"][job.job_id], 1)
                self.assertFalse(any(scope.startswith("local-")
                                     for scope in self.ledger()["jobs"]))

    def test_failed_reservations_survive_restart_without_double_counting(self):
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                job = self.job(kind)
                denied = urllib.error.HTTPError(
                    "https://model.example.invalid/v1/chat/completions",
                    401, "Denied", {}, io.BytesIO(b"fixture refusal"))
                failed_transport = LocalTransport([denied])
                first_executor, _ = self.executor(failed_transport)
                failed = first_executor.execute(job)
                self.assertEqual(failed.state, JobState.FAILED)
                self.assertEqual(failed.model_calls_used, 1)

                # A new client/executor reads the same durable ledger and job.
                resumed = self.queue.get(job.job_id)
                resumed.state = JobState.WORKING
                self.queue.update(resumed)
                success_transport = LocalTransport([completed(kind)])
                second_executor, _ = self.executor(success_transport)
                returned = second_executor.execute(resumed)
                self.assertEqual(success_transport.calls, 1)
                self.assertEqual(returned.model_calls_used, 2)
                self.assertEqual(self.ledger()["jobs"][job.job_id], 2)
                self.assertEqual(self.queue.get(job.job_id).model_calls_used, 2)

                # A repeated execution reaches the allowance, not a fresh scope.
                resumed = self.queue.get(job.job_id)
                resumed.state = JobState.WORKING
                self.queue.update(resumed)
                last_transport = LocalTransport([completed(kind)])
                third_executor, _ = self.executor(last_transport)
                returned = third_executor.execute(resumed)
                self.assertEqual(returned.model_calls_used, 3)
                self.assertEqual(self.ledger()["jobs"][job.job_id], 3)

                resumed = self.queue.get(job.job_id)
                resumed.state = JobState.WORKING
                self.queue.update(resumed)
                blocked_transport = LocalTransport([])
                fourth_executor, _ = self.executor(blocked_transport)
                blocked = fourth_executor.execute(resumed)
                self.assertEqual(blocked.state, JobState.FAILED)
                self.assertIn("job spend ceiling", blocked.error)
                self.assertEqual(blocked_transport.calls, 0)
                self.assertEqual(blocked.model_calls_used, 3)

    def test_distinct_jobs_keep_separate_counts_with_a_shared_client(self):
        transport = LocalTransport([completed(JobKind.EXPLAIN_REPO)] * 2)
        executor, _ = self.executor(transport)
        jobs = [self.job(JobKind.EXPLAIN_REPO, suffix) for suffix in ("_a", "_b")]
        for job in jobs:
            returned = executor.execute(job)
            self.assertEqual(returned.state, JobState.DONE, returned.error)
            self.assertEqual(returned.model_calls_used, 1)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(self.ledger()["jobs"], {job.job_id: 1 for job in jobs})

    def test_original_coding_path_still_binds_stop_and_saved_id(self):
        job = self.job(JobKind.IMPLEMENT_ISSUE)
        stop = threading.Event()
        transport = LocalTransport([
            message(tool("write_file", path="must-not-exist.txt", content="Skip.")),
            completed(JobKind.EXPLAIN_REPO),
        ], stop.set)
        executor, checkouts = self.executor(transport)
        executor.stop_event = stop
        self.assertIsNone(executor._working(job, {"issue": {}, "work_items": []}))
        self.assertEqual(transport.calls, 1)
        self.assertFalse((checkouts.path / "must-not-exist.txt").exists())
        self.assertEqual(job.state, JobState.STOPPED)
        self.assertEqual(job.model_calls_used, 1)
        self.assertEqual(self.ledger()["jobs"], {job.job_id: 1})

    def test_automatic_apply_receives_the_loaded_configuration(self):
        job = self.job(JobKind.TRIAGE_ISSUES)
        AuthorityStore(Path(self.cfg.state_dir) / "authority.json").set_mode(
            job.repo_full_name, "auto")
        contexts = []

        def apply(context, gh, saved_job):
            contexts.append(context)
            self.assertIs(saved_job, job)
            return {"result": "Fixture accepted."}

        task = replace(triage_issues.TASK, apply=apply)
        transport = LocalTransport([completed(job.kind)])
        executor, _ = self.executor(transport)
        with patch("ghagent.executor.tasks.get", return_value=task):
            returned = executor.execute(job)
        self.assertEqual(returned.state, JobState.DONE, returned.error)
        self.assertEqual(len(contexts), 1)
        self.assertIs(contexts[0]["config"], self.cfg)

    def test_branch_merge_never_auto_applies_with_post_directly_permission(self):
        from ghagent.tasks import sync_branch
        job = self.job(JobKind.SYNC_BRANCH)
        job.notes['values'] = {'source': 'develop', 'target': 'main'}
        AuthorityStore(Path(self.cfg.state_dir) / 'authority.json').set_mode(
            job.repo_full_name, 'auto')
        transport = LocalTransport([completed(JobKind.TRIAGE_ISSUES)])
        executor, _ = self.executor(transport)
        with patch.object(sync_branch, 'apply') as apply:
            with patch('ghagent.executor.tasks.get',
                       return_value=replace(sync_branch.TASK, apply=apply)):
                returned = executor.execute(job)
        self.assertEqual(returned.state, JobState.WAITING_ON_YOU, returned.error)
        self.assertNotIn('posted', returned.notes)
        apply.assert_not_called()

    def test_automatic_failure_keeps_claim_and_durably_records_prior_success(self):
        for error_type in (RuntimeError, ValueError):
            with self.subTest(error_type=error_type):
                job = self.job(JobKind.TRIAGE_ISSUES, "_" + error_type.__name__)
                AuthorityStore(Path(self.cfg.state_dir) / "authority.json").set_mode(
                    job.repo_full_name, "auto")
                calls = []
                observed = []

                def apply(context, gh, saved_job):
                    name = context["draft"]["name"]
                    calls.append(name)
                    if len(calls) == 1:
                        return {"result": "First operation is confirmed."}
                    observed.append(self.queue.get(job.job_id))
                    raise error_type("The second operation's result is unknown.")

                task = replace(triage_issues.TASK, apply=apply)
                response = message(
                    tool("write_file", path=".gha-drafts/a_first.md",
                         content="First draft\nFirst body.\n"),
                    tool("write_file", path=".gha-drafts/b_second.md",
                         content="Second draft\nSecond body.\n"),
                    tool("finish", summary="Two drafts.", changed_files=[]))
                executor, _ = self.executor(LocalTransport([response]))
                with patch("ghagent.executor.tasks.get", return_value=task):
                    returned = executor.execute(job)
                self.assertEqual(calls, ["a_first", "b_second"])
                self.assertEqual(returned.state, JobState.WAITING_ON_YOU)
                self.assertEqual(observed[0].notes["posted"][0]["name"], "a_first")
                self.assertEqual(observed[0].notes["posting"], "b_second")
                recorded = self.queue.get(job.job_id)
                self.assertEqual(recorded.notes["posting"], "b_second")
                self.assertEqual([entry["name"] for entry in recorded.notes["posted"]],
                                 ["a_first"])
                self.assertIn("result is unknown", recorded.notes["verification"])
                self.assertEqual(returned.model_calls_used, 1)

    def test_only_a_trusted_pre_write_refusal_clears_the_automatic_claim(self):
        # The guard is owned by a separate patch. This module fixture tests
        # its exact exception-class contract without importing peer changes.
        guard = types.ModuleType("ghagent.task_write_guard")

        class PreWriteRefusal(ValueError):
            pass

        guard.PreWriteRefusal = PreWriteRefusal
        job = self.job(JobKind.TRIAGE_ISSUES)
        AuthorityStore(Path(self.cfg.state_dir) / "authority.json").set_mode(
            job.repo_full_name, "auto")

        def apply(context, gh, saved_job):
            raise PreWriteRefusal("A check refused the write before mutation.")

        task = replace(triage_issues.TASK, apply=apply)
        executor, _ = self.executor(LocalTransport([completed(job.kind)]))
        with patch.dict(sys.modules, {guard.__name__: guard}), \
                patch("ghagent.executor.tasks.get", return_value=task):
            returned = executor.execute(job)
        self.assertEqual(returned.state, JobState.WAITING_ON_YOU)
        recorded = self.queue.get(job.job_id)
        self.assertNotIn("posting", recorded.notes)
        self.assertEqual(recorded.notes["posted"], [])
        self.assertEqual(returned.model_calls_used, 1)


if __name__ == "__main__":
    unittest.main()
