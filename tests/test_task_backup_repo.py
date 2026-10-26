"""backup-repo: registry contract, validation, instruction, and a read-only run.

Offline only: the executor runs against the fakes in tests/test_executor_flow.py,
the result export is recorded instead of run, and no network, gh, or model is
ever touched. git commands are described to the agent, never run here.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ghagent import results, tasks
from ghagent.interfaces import AgentResult, Job, JobKind, JobState
from ghagent.tasks import backup_repo
from ghagent.tasks.types import PROFILES

from tests.test_executor_flow import FakeWT, default_cfg, happy_gh, run_job

REPO = "owner/repo"
BUNDLE = "backup/repo-20261022.bundle"
SUMMARY = (f"Backup written: {BUNDLE}, about 12 MB — roughly the size of a few "
           f"photos. Restore it with: git clone {BUNDLE} restored-copy")


class BackupAgent:
    """Coding-agent factory stub: writes the untracked bundle, then reports."""

    def __init__(self, summary=SUMMARY, bundle=BUNDLE, payload=b"bundle bytes"):
        self.summary = summary
        self.bundle = bundle
        self.payload = payload
        self.instructions: list[str] = []
        self.worktree = None
        self.instantiations = 0

    def __call__(self, worktree_path=None, checks=None):
        self.instantiations += 1
        self.worktree = worktree_path
        return self

    def run(self, instruction):
        self.instructions.append(instruction)
        path = Path(self.worktree) / self.bundle
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return AgentResult(summary=self.summary, changed_files=[self.bundle],
                           worktree_path=self.worktree, model_calls_used=2)


def make_job(**over):
    data = dict(job_id="job_backup", kind=JobKind.BACKUP_REPO,
                repo_full_name=REPO, number=0, command_comment_id=0,
                requested_by=1, dedup_key="backup-repo:owner/repo:0:x",
                notes={"values": {}})
    data.update(over)
    return Job(**data)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_module_is_discovered_with_its_contract(self):
        tasks.types._REGISTRY = None
        task = tasks.load_registry()["backup-repo"]
        self.assertIs(task, backup_repo.TASK)
        self.assertEqual(task.id, "backup-repo")
        self.assertEqual(task.profile, "read-only")
        self.assertIn(task.profile, PROFILES)
        self.assertEqual(task.takes, "repo")
        self.assertEqual(task.fields, {})
        self.assertTrue(callable(task.build_instruction))
        self.assertTrue(callable(task.validate))

    def test_read_only_profile_carries_no_apply(self):
        self.assertIsNone(backup_repo.TASK.apply)

    def test_it_needs_no_inputs_at_all(self):
        task = backup_repo.TASK
        self.assertEqual(task.required_fields(), [])
        self.assertLessEqual(len(task.required_fields()), 3)

    def test_label_and_help_are_plain_words(self):
        task = backup_repo.TASK
        self.assertEqual(task.label, "Back up the repository")
        self.assertIn("backup", task.help.lower())
        self.assertIn("nothing on github is changed", task.help.lower())

    def test_the_gui_catalog_lists_it_without_fields(self):
        tasks.types._REGISTRY = None
        listed = {item["id"]: item for item in tasks.catalog()}
        self.assertIn("backup-repo", listed)
        entry = listed["backup-repo"]
        self.assertEqual(entry["profile"], "read-only")
        self.assertEqual(entry["takes"], "repo")
        self.assertEqual(entry["fields"], [])

    def test_the_job_kind_exists_for_gui_submission(self):
        self.assertIs(JobKind("backup-repo"), JobKind.BACKUP_REPO)


class BundleNamingTests(unittest.TestCase):
    def test_the_name_is_dated_and_sits_in_the_backup_folder(self):
        self.assertEqual(backup_repo.bundle_name(date(2026, 10, 22)), BUNDLE)
        self.assertEqual(backup_repo.BUNDLE_DIR, "backup")
        self.assertTrue(BUNDLE.endswith(".bundle"))

    def test_each_day_is_zero_padded(self):
        self.assertEqual(backup_repo.bundle_name(date(2026, 1, 2)),
                         "backup/repo-20260102.bundle")

    def test_the_default_is_today_in_utc(self):
        before = datetime.now(timezone.utc).date()
        name = backup_repo.bundle_name()
        after = datetime.now(timezone.utc).date()
        allowed = {f"backup/repo-{day:%Y%m%d}.bundle" for day in (before, after)}
        self.assertIn(name, allowed)

    def test_the_export_limit_matches_the_executor(self):
        self.assertEqual(backup_repo.EXPORT_LIMIT_BYTES, 50_000_000)
        self.assertEqual(backup_repo.EXPORT_LIMIT_BYTES, results.EXPORT_LIMIT_BYTES)
        self.assertEqual(backup_repo.EXPORT_LIMIT_WORDS, "50 MB")


class ValidationTests(unittest.TestCase):
    def test_every_input_shape_is_valid(self):
        for data in (None, {}, {"repo": REPO},
                     {"repo": REPO, "number": 0, "values": {}},
                     {"repo": REPO, "values": {"anything": "x"}}):
            with self.subTest(data=data):
                self.assertIsNone(backup_repo.validate(data))


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.text = backup_repo.build_instruction({"repo": REPO})

    def test_it_names_the_repository_and_the_bundle_file(self):
        self.assertIn(REPO, self.text)
        self.assertIn(backup_repo.bundle_name(), self.text)
        self.assertIn("mkdir -p backup", self.text)

    def test_it_gives_the_exact_create_and_verify_commands(self):
        self.assertIn("git bundle create", self.text)
        self.assertIn("--all", self.text)
        self.assertIn("git bundle verify", self.text)

    def test_it_asks_for_size_in_plain_words_and_how_to_restore(self):
        self.assertIn("plain words", self.text)
        self.assertIn("size", self.text.lower())
        self.assertIn("git clone", self.text)
        self.assertIn("restored-copy", self.text)
        self.assertIn("every branch", self.text)

    def test_it_explains_the_export_limit(self):
        self.assertIn("50 MB", self.text)
        self.assertIn(str(backup_repo.EXPORT_LIMIT_BYTES), self.text)
        self.assertIn("too large to be copied", self.text)
        self.assertIn("limit shared by all exported files", self.text)

    def test_it_forbids_remote_writes(self):
        self.assertIn("push, post or comment nothing", self.text)
        self.assertIn("Change no repository files", self.text)
        self.assertIn("only reads", self.text)

    def test_instruction_without_a_repo_still_reads(self):
        text = backup_repo.build_instruction({})
        self.assertIn("this repository", text)
        self.assertIn("git bundle create", text)


class ReadOnlyExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.cfg = default_cfg()
        self.cfg.worktree_root = str(self.temp / "worktrees")

    def tearDown(self):
        tasks.types._REGISTRY = None

    def test_run_reports_and_makes_zero_remote_or_commit_calls(self):
        tasks.types._REGISTRY = None
        agent = BackupAgent()
        gh = happy_gh()
        returned, queue, wt, _, git_rec, gh_rec = run_job(
            make_job(), gh, cfg=self.cfg, wt=FakeWT(), agent=agent)

        self.assertEqual(returned.state, JobState.DONE, returned.error)
        self.assertEqual(returned.notes["phase"], "reporting")
        self.assertEqual(returned.notes["agent_summary"], SUMMARY)
        self.assertIn("reads and reports", returned.notes["verification"])

        states = [state for state, _ in queue.history]
        self.assertIn("retrieving", states)
        self.assertIn("working", states)
        self.assertNotIn("responding", states)

        self.assertEqual(agent.instantiations, 1)
        wt_path = agent.worktree
        self.assertTrue(wt_path.startswith(str(self.temp)))
        self.assertIn(("add_worktree", wt_path, "main"), wt.calls)
        self.assertIn("git bundle create", agent.instructions[0])
        self.assertIn("git bundle verify", agent.instructions[0])
        self.assertIn(backup_repo.bundle_name(), agent.instructions[0])
        self.assertIn(REPO, agent.instructions[0])

        self.assertEqual(returned.notes["changed_files"], [BUNDLE])
        self.assertTrue((Path(wt_path) / BUNDLE).is_file())
        self.assertEqual(returned.produced_commits, [])
        self.assertFalse([c for c in wt.calls if c[0] == "commit_all"])
        self.assertIn(("remove_worktree", wt_path), wt.calls)
        self.assertEqual(git_rec.calls, [])
        self.assertEqual(gh.rest_calls, [])
        self.assertEqual(gh_rec.calls, [])

    def test_the_untracked_bundle_is_handed_to_the_result_export(self):
        output = self.temp / "output"
        output.mkdir()
        job = make_job(notes={"values": {}, "output_dir": str(output),
                              "member": "alice",
                              "member_home": str(self.temp)})
        exported = []

        def record(job_arg, wt_arg):
            exported.append((job_arg.job_id, str(wt_arg)))

        agent = BackupAgent()
        with patch.object(results, "export_worktree", record):
            returned, _, _, _, _, _ = run_job(
                job, happy_gh(), cfg=self.cfg, wt=FakeWT(), agent=agent)

        self.assertEqual(returned.state, JobState.DONE, returned.error)
        self.assertEqual(exported, [("job_backup", agent.worktree)])
        self.assertTrue((Path(agent.worktree) / BUNDLE).is_file())
        self.assertIsNone(returned.notes.get("result_export_error"))
        self.assertTrue((output / "gha-worker-job_backup" / "report.md").is_file())


if __name__ == "__main__":
    unittest.main()
