"""Tests for ghagent.queue — durability, atomicity, dedup, stop, reconcile.
All remote interaction goes through the FakeGh runner."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ghagent.interfaces import Job, JobKind, JobState
from ghagent.queue import QueueError, TERMINAL_STATES, JobQueue
from tests.mock_github import make_client

SHA_A = "a" * 40
SHA_B = "b" * 40


def make_job(**overrides) -> Job:
    fields = dict(
        job_id="job_1",
        kind=JobKind.FIX_PR,
        repo_full_name="o/r",
        number=1,
        command_comment_id=100,
        requested_by=2,
        head_sha=SHA_A,
        dedup_key=f"fix-pr:o/r:1:{SHA_A}",
    )
    fields.update(overrides)
    return Job(**fields)


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.queue = JobQueue(self.state_dir)


class AddGetUpdateAllTest(TempStateTestCase):
    def test_add_stamps_timestamps_and_round_trips(self):
        job = make_job()
        self.assertTrue(self.queue.add(job))
        self.assertTrue(job.created_at)
        self.assertTrue(job.updated_at)
        loaded = self.queue.get("job_1")
        self.assertEqual(loaded.kind, JobKind.FIX_PR)
        self.assertEqual(loaded.repo_full_name, "o/r")
        self.assertEqual(loaded.head_sha, SHA_A)
        self.assertEqual(loaded.dedup_key, job.dedup_key)
        self.assertEqual(loaded.state, JobState.RECEIVED)
        self.assertEqual(loaded.created_at, job.created_at)

    def test_persist_survives_reload_in_new_queue_instance(self):
        self.queue.add(make_job())
        second = JobQueue(self.state_dir)
        self.assertEqual([j.job_id for j in second.all()], ["job_1"])

    def test_store_file_is_valid_json_with_expected_shape(self):
        self.queue.add(make_job())
        raw = json.loads((self.state_dir / "jobs.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["jobs"]["job_1"]["kind"], "fix-pr")
        self.assertEqual(raw["jobs"]["job_1"]["state"], "received")

    def test_no_temp_files_left_behind_after_operations(self):
        self.queue.add(make_job())
        self.queue.get("job_1")
        leftovers = list(self.state_dir.glob("*.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_lockfile_created_in_state_dir(self):
        self.queue.add(make_job())
        self.assertTrue((self.state_dir / "queue.lock").is_file())

    def test_all_sorted_by_created_at(self):
        clock = iter([datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc),
                      datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)])
        with mock.patch("ghagent.queue._utcnow", side_effect=lambda: next(clock)):
            self.queue.add(make_job(job_id="job_later"))
            self.queue.add(make_job(job_id="job_earlier", dedup_key="k2"))
        self.assertEqual([j.job_id for j in self.queue.all()],
                         ["job_earlier", "job_later"])

    def test_update_stamps_updated_at_and_persists(self):
        clock = iter([datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc),
                      datetime(2026, 10, 17, 11, 0, 0, tzinfo=timezone.utc)])
        with mock.patch("ghagent.queue._utcnow", side_effect=lambda: next(clock)):
            job = make_job()
            self.queue.add(job)
            job.state = JobState.WORKING
            self.queue.update(job)
        self.assertEqual(job.updated_at, "2026-10-17T11:00:00+00:00")
        self.assertEqual(self.queue.get("job_1").state, JobState.WORKING)

    def test_update_unknown_job_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.queue.update(make_job(job_id="never_added"))

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.queue.get("nope"))


class DedupTest(TempStateTestCase):
    def test_duplicate_dedup_key_rejected(self):
        self.assertTrue(self.queue.add(make_job(job_id="job_1")))
        twin = make_job(job_id="job_2")  # different id, same dedup_key
        self.assertFalse(self.queue.add(twin))
        self.assertEqual(len(self.queue.all()), 1)

    def test_different_dedup_key_accepted(self):
        self.assertTrue(self.queue.add(make_job(job_id="job_1")))
        self.assertTrue(self.queue.add(
            make_job(job_id="job_2", head_sha=SHA_B,
                     dedup_key=f"fix-pr:o/r:1:{SHA_B}")))

    def test_dedup_survives_reload(self):
        self.queue.add(make_job())
        second = JobQueue(self.state_dir)
        self.assertFalse(second.add(make_job(job_id="job_9")))

    def test_empty_dedup_key_does_not_collide(self):
        self.assertTrue(self.queue.add(make_job(job_id="job_1", dedup_key="")))
        self.assertTrue(self.queue.add(make_job(job_id="job_2", dedup_key="")))

    def test_failed_job_key_is_retryable(self):
        first = make_job(job_id="job_1")
        self.assertTrue(self.queue.add(first))
        first.state = JobState.FAILED
        self.queue.update(first)
        self.assertTrue(self.queue.add(make_job(job_id="job_2")))

    def test_stopped_job_key_is_retryable(self):
        first = make_job(job_id="job_1")
        self.assertTrue(self.queue.add(first))
        first.state = JobState.STOPPED
        self.queue.update(first)
        self.assertTrue(self.queue.add(make_job(job_id="job_2")))

    def test_done_job_key_still_rejected(self):
        first = make_job(job_id="job_1")
        self.assertTrue(self.queue.add(first))
        first.state = JobState.DONE
        self.queue.update(first)
        self.assertFalse(self.queue.add(make_job(job_id="job_2")))


class CorruptStoreTest(TempStateTestCase):
    def _corrupt(self, content: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "jobs.json").write_text(content, encoding="utf-8")

    def test_corrupt_json_raises_instead_of_empty_state(self):
        self._corrupt("{not json")
        with self.assertRaises(QueueError):
            self.queue.all()
        with self.assertRaises(QueueError):
            self.queue.get("job_1")
        with self.assertRaises(QueueError):
            self.queue.add(make_job())

    def test_wrong_structure_raises(self):
        self._corrupt(json.dumps(["a", "list"]))
        with self.assertRaises(QueueError):
            self.queue.all()

    def test_missing_file_is_empty_queue(self):
        self.assertEqual(self.queue.all(), [])


class OpenLookupTest(TempStateTestCase):
    def test_finds_open_job_by_repo_and_number(self):
        self.queue.add(make_job(number=1))
        found = self.queue.find_open_by_repo_number("o/r", 1)
        self.assertIsNotNone(found)
        self.assertEqual(found.number, 1)

    def test_terminal_states_are_not_open(self):
        for state in (JobState.DONE, JobState.FAILED, JobState.STOPPED):
            with self.subTest(state=state):
                queue = JobQueue(self.state_dir)
                queue.add(make_job(job_id=f"job_{state.value}", state=state))
                self.assertIsNone(queue.find_open_by_repo_number("o/r", 1))

    def test_other_repo_or_number_not_matched(self):
        self.queue.add(make_job())
        self.assertIsNone(self.queue.find_open_by_repo_number("o/other", 1))
        self.assertIsNone(self.queue.find_open_by_repo_number("o/r", 2))

    def test_returns_newest_when_multiple_open(self):
        clock = iter([datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc),
                      datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)])
        with mock.patch("ghagent.queue._utcnow", side_effect=lambda: next(clock)):
            self.queue.add(make_job(job_id="job_old"))
            self.queue.add(make_job(job_id="job_new", dedup_key="k2"))
        found = self.queue.find_open_by_repo_number("o/r", 1)
        self.assertEqual(found.job_id, "job_new")


class StopJobTest(TempStateTestCase):
    def test_stop_live_job(self):
        self.queue.add(make_job(state=JobState.WORKING))
        self.assertTrue(self.queue.stop_job("job_1"))
        self.assertEqual(self.queue.get("job_1").state, JobState.STOPPED)

    def test_stop_is_idempotent(self):
        self.queue.add(make_job())
        self.assertTrue(self.queue.stop_job("job_1"))
        self.assertFalse(self.queue.stop_job("job_1"))
        self.assertEqual(self.queue.get("job_1").state, JobState.STOPPED)

    def test_stop_unknown_job_false(self):
        self.assertFalse(self.queue.stop_job("ghost"))

    def test_stop_terminal_job_false(self):
        for state in TERMINAL_STATES:
            with self.subTest(state=state):
                queue = JobQueue(self.state_dir)
                queue.add(make_job(job_id=f"j_{state.value}", state=state))
                self.assertFalse(queue.stop_job(f"j_{state.value}"))


class ReconcileTest(TempStateTestCase):
    def _reconcile_with(self, pr_view_payload):
        queue = JobQueue(self.state_dir)
        queue.add(make_job(state=JobState.WORKING))
        gh = make_client()
        gh.runner.expect_json(pr_view_payload)
        return queue, queue.reconcile_with_remote(gh), gh

    def test_merged_pr_becomes_done_with_note(self):
        queue, actions, gh = self._reconcile_with(
            {"state": "MERGED", "headRefOid": SHA_A})
        self.assertEqual(len(actions), 1)
        self.assertIn("merged", actions[0])
        self.assertIn("DONE", actions[0])
        job = queue.get("job_1")
        self.assertEqual(job.state, JobState.DONE)
        self.assertTrue(any("merged" in str(v) for v in job.notes.values()))
        self.assertEqual(gh.runner.last_call.argv[:3], ["pr", "view", "1"])

    def test_closed_pr_becomes_done(self):
        queue, actions, _ = self._reconcile_with(
            {"state": "CLOSED", "headRefOid": SHA_A})
        self.assertEqual(queue.get("job_1").state, JobState.DONE)
        self.assertEqual(len(actions), 1)
        # persisted for a fresh queue instance too
        self.assertEqual(JobQueue(self.state_dir).get("job_1").state,
                         JobState.DONE)

    def test_moved_head_recorded_state_left_to_executor(self):
        queue, actions, _ = self._reconcile_with(
            {"state": "OPEN", "headRefOid": SHA_B})
        job = queue.get("job_1")
        self.assertEqual(job.state, JobState.WORKING)  # untouched
        self.assertEqual(len(actions), 1)
        self.assertIn(SHA_B, actions[0])
        self.assertTrue(any(f"remote head moved to {SHA_B}" == v
                            for v in job.notes.values()))

    def test_unchanged_head_yields_no_actions(self):
        queue, actions, _ = self._reconcile_with(
            {"state": "OPEN", "headRefOid": SHA_A})
        self.assertEqual(actions, [])
        job = queue.get("job_1")
        self.assertEqual(job.state, JobState.WORKING)
        self.assertEqual(job.notes, {})

    def test_skips_terminal_and_non_fix_pr_jobs_without_remote_calls(self):
        queue = JobQueue(self.state_dir)
        queue.add(make_job(job_id="job_done", state=JobState.DONE))
        queue.add(make_job(job_id="job_impl", kind=JobKind.IMPLEMENT_ISSUE))
        queue.add(make_job(job_id="job_status", kind=JobKind.STATUS))
        gh = make_client()
        actions = queue.reconcile_with_remote(gh)
        self.assertEqual(actions, [])
        self.assertEqual(gh.runner.calls, [])

    def test_remote_failure_reported_not_raised(self):
        queue = JobQueue(self.state_dir)
        queue.add(make_job(state=JobState.WORKING))
        gh = make_client()
        gh.runner.expect_error("gh: Not Found (HTTP 404)", returncode=1)
        actions = queue.reconcile_with_remote(gh)
        self.assertEqual(len(actions), 1)
        self.assertIn("reconcile lookup failed", actions[0])
        self.assertEqual(queue.get("job_1").state, JobState.WORKING)


if __name__ == "__main__":
    unittest.main()
