"""Tests for the pre-commit secret gate (stdlib only).

Credential-shaped fixtures are assembled by concatenation so this file never
contains a complete literal token. That keeps the repository's own file clean
for GitHub's push protection and for the gate itself.
"""
from __future__ import annotations

import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghagent import fleet_runner, secret_gate
from ghagent.interfaces import Config
from ghagent.secret_gate import (
    SecretGateRefusal,
    check_commit,
    check_paths,
    git_result,
    is_allowed,
    load_allow,
    scan_diff,
    scan_paths,
    scan_text,
)
from ghagent.wiring import WorktreeOpsAdapter
from ghagent.worktree import commit_all as worktree_commit_all

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GHO = "gho_" + "b9K2mQ7xT4vL8nR1sD6fG3hJ5kM0pW2zA4cE"
GHU = "ghu_" + "c8J3nR6yU5wM9sT2vE1fH4kL7pQ0zX3bA6dG"
GHS = "ghs_" + "d7K4mS5zV6xN0tU3wF2gJ5lM8qR1aY4cB7eH"
GH_PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEF"
SLACK = "xoxb-" + "1234567890-abcdefghijklmn"
AWS_KEY = "AKIA" + "ABCDEFGH12345678"
OPENAI = "sk-" + "abc123DEF456ghi789JKL012mno345"
GOOGLE = "AIza" + "SyA1234567890abcdefghijklmnopqrstuv"
JWT = ("eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0."
       + "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
PEM = ("-----BEGIN RSA " + "PRIVATE KEY-----\n"
       + "MIIEowIBAAKCAQEAvT8kQm3nLpZr7xYw2cVbNfHjKdSaQeWrTyUiOpAsDfGhJkLzXc\n"
       + "-----END RSA " + "PRIVATE KEY-----")
PG_URL = "postgres://app:" + "s3cr3tP4ss" + "@db.acme-fleet.io:5432/main"
REDIS_URL = "redis://:" + "hunter2hunter2" + "@cache01.acme-fleet.io:6379/0"
MONGO_URL = "mongodb+srv://u1:" + "p4ssw0rdX" + "@cluster0.acme-fleet.io/x"


def sh(argv, cwd=None, check=True):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{argv}: {proc.stderr[:300]}")
    return proc


class GitRepoCase(unittest.TestCase):
    """A disposable git repository the commit-path tests can drive."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        sh(["git", "init", "-q", "-b", "main", str(self.wt)])
        sh(["git", "-C", str(self.wt), "config", "user.name", "t"])
        sh(["git", "-C", str(self.wt), "config", "user.email", "t@local"])

    def adapter(self) -> WorktreeOpsAdapter:
        return WorktreeOpsAdapter(Config(state_dir=str(self.tmp),
                                         worktree_root=str(self.tmp)))

    def commit_count(self) -> int:
        out = sh(["git", "-C", str(self.wt), "rev-list", "--count", "--all"],
                 check=False).stdout.strip()
        return int(out or 0)

    def staged(self, text: str, name: str = "leak.py") -> None:
        (self.wt / name).write_text(text)
        sh(["git", "-C", str(self.wt), "add", "-A"])


class DetectorTests(unittest.TestCase):
    """Each detector fires on a real-shaped value."""

    def assert_fires(self, text: str, kind: str) -> None:
        findings = scan_text(text, "config.py")
        self.assertIn(kind, findings.kinds(), f"expected {kind} from {text!r}")

    def test_private_key(self):
        self.assert_fires(PEM, "private-key")

    def test_github_tokens(self):
        for token in (GHP, GHO, GHU, GHS, GH_PAT):
            with self.subTest(token=token[:5]):
                self.assert_fires(f'token = "{token}"', "github-token")

    def test_slack_token(self):
        self.assert_fires(f"SLACK={SLACK}", "slack-token")

    def test_aws_access_key(self):
        self.assert_fires(f"AWS_ACCESS_KEY_ID={AWS_KEY}", "aws-access-key")

    def test_openai_key(self):
        self.assert_fires(f"key: {OPENAI}", "openai-key")

    def test_google_api_key(self):
        self.assert_fires(f"key={GOOGLE}", "google-api-key")

    def test_jwt(self):
        self.assert_fires(f"Authorization: Bearer {JWT}", "jwt")

    def test_credential_assignments(self):
        for text in ('password = "Sup3rS3cret!"', "api_key: 9f8e7d6c5b4a39281706",
                     'secret="t0p-S3cret-value"', 'token = "abc123def456ghi"'):
            with self.subTest(text=text):
                self.assert_fires(text, "credential-assignment")

    def test_connection_strings(self):
        for text in (PG_URL, REDIS_URL, MONGO_URL):
            with self.subTest(text=text):
                self.assert_fires(text, "connection-string")

    def test_documentation_hosts_are_not_flagged(self):
        for text in ("postgres://user:p4ssw0rd@db.example.com:5432/main",
                     "redis://:s3cretpass@cache.test:6379/0"):
            with self.subTest(text=text):
                self.assertTrue(scan_text(text, "app.cfg").ok, text)

    def test_private_ip_in_network_context(self):
        for text in ("DB_HOST=10.0.0.5", 'REDIS_HOST = "172.20.1.4"',
                     "connect to 192.168.1.10:8080"):
            with self.subTest(text=text):
                self.assert_fires(text, "private-ip")

    def test_internal_hostname(self):
        self.assert_fires("upstream app.prod.internal:9000", "internal-hostname")

    def test_finding_reports_file_and_line(self):
        findings = scan_text("safe\nsafe\npassword = \"Sup3rS3cret!\"\n", "deep/app.py")
        self.assertEqual(len(findings.items), 1)
        self.assertEqual(findings.items[0].path, "deep/app.py")
        self.assertEqual(findings.items[0].line, 3)

    def test_refusal_never_echoes_the_secret(self):
        findings = scan_text(f'token = "{GHP}"', "ghagent/config.py")
        message = findings.refusal()
        self.assertNotIn(GHP, message)
        self.assertNotIn(GHP[4:20], message)
        self.assertIn("ghagent/config.py", message)
        self.assertIn("github-token", message)
        self.assertIn("[secret_gate]", message)


class PlaceholderTests(unittest.TestCase):
    """Documented stand-ins and the app's own markers never block a commit."""

    def assert_quiet(self, text: str) -> None:
        findings = scan_text(text, "app.py")
        self.assertTrue(findings.ok, f"false positive {findings.kinds()} from {text!r}")

    def test_placeholders(self):
        for text in ("password = your-token-here",
                     "token=${GITHUB_TOKEN}",
                     "api_key = process.env.API_KEY",
                     "secret=<redacted>",
                     'password=os.environ["DB_PASS"]',
                     "token=xxxxxxxxxxxxxxxx",
                     "api_key = getenv('KEY')",
                     "password: str = \"\"",
                     "token = self.token",
                     "token=GITHUB_TOKEN",
                     'secret: "{{ vault_secret }}"'):
            with self.subTest(text=text):
                self.assert_quiet(text)

    def test_fleet_marker(self):
        self.assert_quiet("marker = 'fatchin-fleet:v1:correction-pr'")
        self.assert_quiet("fatchin-fleet:v1:secret-review password=none")

    def test_pem_header_without_a_body(self):
        self.assert_quiet("-----BEGIN RSA " + "PRIVATE KEY-----")

    def test_obviously_fabricated_tokens(self):
        self.assert_quiet("AKIA" + "X" * 16)
        self.assert_quiet("ghp_" + "x" * 36)

    def test_prose_and_public_addresses(self):
        for text in ("the 10.0.0.1 range is common in offices",
                     "192.168.0.1 is a router default",
                     "8.8.8.8 is a public resolver",
                     "bind 127.0.0.1:8000"):
            with self.subTest(text=text):
                self.assert_quiet(text)


class DiffTests(unittest.TestCase):
    def test_clean_diff_has_no_findings(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -1,2 +1,3 @@\n ctx\n+x = 1\n ctx2\n")
        findings = scan_diff(diff)
        self.assertTrue(findings.ok)
        self.assertEqual(findings.items, [])

    def test_added_line_is_reported_at_its_file_line(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -1,2 +1,3 @@\n ctx\n+password = \"Sup3rS3cret!\"\n ctx2\n")
        findings = scan_diff(diff)
        self.assertFalse(findings.ok)
        self.assertEqual((findings.items[0].path, findings.items[0].line), ("x.py", 2))

    def test_removed_secret_is_not_a_finding(self):
        diff = ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
                f'-password = "{GHP}"\n+password = ""\n')
        self.assertTrue(scan_diff(diff).ok)


class TestTreeTests(unittest.TestCase):
    """A fixture password is not distinguishable from a leak; a token is."""

    def test_weak_kinds_are_not_reported_in_tests(self):
        paths = ("tests/test_a.py", "pkg/tests/helpers.py", "spec/a_spec.rb",
                 "fixtures/creds.json", "testdata/x.json", "conftest.py")
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(scan_text("DB_HOST=10.0.0.5", path).ok, path)
                self.assertTrue(scan_text('password = "Sup3rS3cret!"', path).ok, path)
                self.assertTrue(scan_text("upstream app.prod.internal", path).ok, path)

    def test_strong_kinds_still_report_in_tests(self):
        self.assertIn("github-token",
                      scan_text(f'token = "{GHP}"', "tests/test_a.py").kinds())
        self.assertIn("private-key", scan_text(PEM, "tests/test_a.py").kinds())
        self.assertIn("jwt", scan_text(JWT, "fixtures/x.json").kinds())

    def test_is_test_path(self):
        self.assertTrue(secret_gate.is_test_path("tests/test_secret_gate.py"))
        self.assertFalse(secret_gate.is_test_path("src/ghagent/wiring.py"))
        self.assertFalse(secret_gate.is_test_path("attestation.py"))


class SelfScanTests(unittest.TestCase):
    def test_the_gate_does_not_flag_its_own_files(self):
        root = Path(__file__).resolve().parents[1]
        findings = scan_paths([root / "src/ghagent/secret_gate.py",
                               root / "tests/test_secret_gate.py"], root=root)
        self.assertTrue(findings.ok, findings.kinds())


class BoundedScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_large_file_is_skipped(self):
        path = self.tmp / "big.txt"
        filler = "a = 1\n" * (secret_gate.MAX_FILE_BYTES // 5 + 1000)
        path.write_text(f'password = "Sup3rS3cret!"\n' + filler)
        self.assertGreater(path.stat().st_size, secret_gate.MAX_FILE_BYTES)
        self.assertTrue(scan_paths([path], root=self.tmp).ok)

    def test_binary_file_is_skipped(self):
        path = self.tmp / "blob.bin"
        path.write_bytes(b"\x00\x01" + f'token = "{GHP}"'.encode())
        self.assertTrue(scan_paths([path], root=self.tmp).ok)

    def test_lockfile_is_skipped(self):
        self.write("package-lock.json", f'{{"token": "{GHP}"}}')
        self.assertTrue(scan_paths(["package-lock.json"], root=self.tmp).ok)

    def test_vendored_directory_is_skipped(self):
        self.write("node_modules/pkg/index.js", f'const t = "{GHP}"')
        self.assertTrue(scan_paths([Path("node_modules")], root=self.tmp).ok)

    def test_binary_extension_is_skipped(self):
        path = self.write("logo.png", f'token = "{GHP}"')
        self.assertTrue(scan_paths([path], root=self.tmp).ok)

    def test_ordinary_source_is_still_scanned(self):
        path = self.write("src/app.py", f'token = "{GHP}"')
        findings = scan_paths([path], root=self.tmp)
        self.assertEqual(findings.items[0].path, "src/app.py")

    def test_file_count_is_capped(self):
        for index in range(3):
            self.write(f"f{index}.py", "x = 1\n")
        with mock.patch.object(secret_gate, "MAX_FILES", 1):
            findings = scan_paths([Path(".")], root=self.tmp)
        self.assertFalse(findings.ok)
        self.assertTrue(findings.errors)

    def test_submodule_directory_is_not_entered(self):
        inner = self.tmp / "vendor" / "sub"
        inner.mkdir(parents=True)
        (inner / ".git").write_text("gitdir: ../../.git/modules/sub\n")
        (inner / "creds.py").write_text(f'token = "{GHP}"\n')
        self.assertTrue(scan_paths(["vendor"], root=self.tmp).ok)


class FailClosedTests(unittest.TestCase):
    """Incomplete scans hold commits without echoing file contents or errors."""

    def test_scanner_exception_holds_the_commit_without_echoing_error(self):
        run_git = lambda argv: (0, "leak.py\x00", "")
        with mock.patch.object(secret_gate, "_scan_index", side_effect=RuntimeError(GHP)):
            with self.assertRaises(SecretGateRefusal) as refused:
                check_commit(run_git, ".")
        self.assertNotIn(GHP, str(refused.exception))
        self.assertIn("scan", str(refused.exception).lower())

    def test_git_failure_holds_the_commit_without_echoing_stderr(self):
        for run_git in (lambda argv: (1, "", GHP), mock.Mock(side_effect=OSError(GHP))):
            with self.subTest(run_git=run_git):
                with self.assertRaises(SecretGateRefusal) as refused:
                    check_commit(run_git, ".")
                self.assertNotIn(GHP, str(refused.exception))
                self.assertIn("staged file list", str(refused.exception))

    def test_unreadable_file_is_reported_and_blocks_even_with_allow_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("x = 1\n")
            with mock.patch.object(secret_gate, "_read_text", side_effect=OSError(GHP)):
                findings = scan_paths(["a.py"], root=tmp)
                self.assertFalse(findings.ok)
                self.assertNotIn(GHP, str(findings.errors))
                with self.assertRaises(SecretGateRefusal):
                    check_paths(["a.py"], root=tmp, allow=("*:*",))

    def test_missing_file_holds_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = scan_paths(["nope.py"], root=tmp)
            self.assertFalse(findings.ok)
            with self.assertRaises(SecretGateRefusal):
                check_paths(["nope.py"], root=tmp)

    def test_failed_stat_holds_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("x = 1\n")
            with mock.patch.object(Path, "stat", side_effect=PermissionError(GHP)):
                with self.assertRaises(SecretGateRefusal) as refused:
                    check_paths(["a.py"], root=tmp)
            self.assertNotIn(GHP, str(refused.exception))

    def test_real_file_with_a_secret_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "leak.py").write_text(f'token = "{GHP}"')
            with self.assertRaises(SecretGateRefusal):
                check_paths(["leak.py"], root=tmp)

    def test_empty_staged_set_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(check_commit(lambda argv: (0, "", ""), tmp).ok)

    def test_file_count_limit_holds_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.py", "b.py"):
                Path(tmp, name).write_text("x = 1\n")
            with mock.patch.object(secret_gate, "MAX_FILES", 1):
                with self.assertRaises(SecretGateRefusal):
                    check_paths(["a.py", "b.py"], root=tmp)


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.leak = self.tmp / "leak.py"
        self.leak.write_text(f'token = "{GHP}"\n')

    def test_config_allowlist_suppresses_the_hit(self):
        cfg = Config(state_dir=str(self.tmp), worktree_root=str(self.tmp),
                     secret_gate={"allow": ["leak.py:github-token"]})
        self.assertTrue(scan_paths(["leak.py"], root=self.tmp,
                                   allow=load_allow(cfg, self.tmp)).ok)

    def test_allow_file_suppresses_the_hit(self):
        (self.tmp / ".fatchin-secret-allow").write_text(
            "# acknowledged: fixture\nleak.py:github-token\n")
        self.assertTrue(check_paths(["leak.py"], root=self.tmp).ok)

    def test_allow_file_comments_and_blank_lines(self):
        (self.tmp / ".fatchin-secret-allow").write_text("# nothing\n\n")
        with self.assertRaises(SecretGateRefusal):
            check_paths(["leak.py"], root=self.tmp)

    def test_wrong_kind_is_not_suppressed(self):
        cfg = Config(state_dir=str(self.tmp), worktree_root=str(self.tmp),
                     secret_gate={"allow": ["leak.py:aws-access-key"]})
        with self.assertRaises(SecretGateRefusal):
            check_paths(["leak.py"], root=self.tmp, config=cfg)

    def test_glob_paths_are_supported(self):
        nested = self.tmp / "tests" / "fixtures" / "creds.py"
        nested.parent.mkdir(parents=True)
        nested.write_text(f'token = "{GHP}"\n')
        cfg = Config(state_dir=str(self.tmp), worktree_root=str(self.tmp),
                     secret_gate={"allow": ["tests/*:github-token"]})
        self.assertTrue(check_paths(["tests/fixtures/creds.py"], root=self.tmp,
                                    config=cfg).ok)

    def test_unrelated_file_is_still_blocked(self):
        cfg = Config(state_dir=str(self.tmp), worktree_root=str(self.tmp),
                     secret_gate={"allow": ["other.py:github-token"]})
        with self.assertRaises(SecretGateRefusal):
            check_paths(["leak.py"], root=self.tmp, config=cfg)

    def test_malformed_config_allowlist_holds_even_a_clean_commit(self):
        self.leak.write_text("x = 1\n")
        for listed in ("*:*", ["no-colon"], [123]):
            with self.subTest(listed=listed):
                cfg = Config(secret_gate={"allow": listed})
                with self.assertRaises(SecretGateRefusal):
                    check_paths(["leak.py"], root=self.tmp, config=cfg)

    def test_explicit_missing_or_unreadable_allow_file_holds(self):
        self.leak.write_text("x = 1\n")
        with mock.patch.dict("os.environ", {"FATCHIN_SECRET_ALLOW_FILE": str(self.tmp / "missing")}):
            with self.assertRaises(SecretGateRefusal):
                check_paths(["leak.py"], root=self.tmp)
        (self.tmp / ".fatchin-secret-allow").write_text("leak.py:github-token\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError(GHP)):
            with self.assertRaises(SecretGateRefusal) as refused:
                check_paths(["leak.py"], root=self.tmp)
        self.assertNotIn(GHP, str(refused.exception))

    def test_malformed_allow_file_holds(self):
        (self.tmp / ".fatchin-secret-allow").write_text("invalid entry\n")
        with self.assertRaises(SecretGateRefusal):
            check_paths(["leak.py"], root=self.tmp)

    def test_is_allowed_ignores_malformed_entries(self):
        finding = scan_paths(["leak.py"], root=self.tmp).items[0]
        self.assertFalse(is_allowed(finding, ["no-colon", "", "leak.py"]))
        self.assertTrue(is_allowed(finding, ["leak.py:github-token"]))
        self.assertTrue(is_allowed(finding, ["*:github-token"]))
        self.assertTrue(is_allowed(finding, ["*:*"]))


class CommitPathTests(GitRepoCase):
    """The gate runs before each real commit call, with real git."""

    def test_staged_secret_is_refused_even_after_working_file_is_cleaned(self):
        self.staged(f'token = "{GHP}"\n')
        (self.wt / "leak.py").write_text("token = None\n")
        with self.assertRaises(SecretGateRefusal):
            check_commit(
                git_result(lambda argv: sh(["git", *argv], cwd=self.wt, check=False)), self.wt)
        self.assertEqual(self.commit_count(), 0)

    def test_staged_clean_content_is_scanned_instead_of_dirty_working_file(self):
        self.staged("token = None\n")
        (self.wt / "leak.py").write_text(f'token = "{GHP}"\n')
        findings = check_commit(git_result(lambda argv: sh(["git", *argv], cwd=self.wt, check=False)), self.wt)
        self.assertTrue(findings.ok)
        self.assertEqual(self.commit_count(), 0)

    def test_staged_path_with_newlines_and_pathspec_characters_is_scanned(self):
        name = "[config] with\nnewline.py"
        self.staged(f'token = "{GHP}"\n', name=name)
        (self.wt / name).unlink()
        with self.assertRaises(SecretGateRefusal):
            check_commit(git_result(lambda argv: sh(["git", *argv], cwd=self.wt, check=False)), self.wt)

    def test_staged_blob_read_failure_holds_without_working_file_fallback(self):
        self.staged("x = 1\n", name="app.py")
        def run(argv):
            if argv[0] == "show":
                return (1, "", GHP)
            proc = sh(["git", *argv], cwd=self.wt, check=False)
            return proc.returncode, proc.stdout, proc.stderr
        with self.assertRaises(SecretGateRefusal) as refused:
            check_commit(run, self.wt)
        self.assertNotIn(GHP, str(refused.exception))
        self.assertEqual(self.commit_count(), 0)

    def test_staged_scan_keeps_existing_binary_vendor_lock_and_size_exclusions(self):
        (self.wt / "node_modules").mkdir()
        excluded = {
            "logo.png": f'token = "{GHP}"'.encode(),
            "node_modules/vendor.js": f'token = "{GHP}"'.encode(),
            "package-lock.json": f'{{"token": "{GHP}"}}'.encode(),
            "binary.data": b"\x00" + f'token = "{GHP}"'.encode(),
            "large.txt": f'token = "{GHP}"\n'.encode() + b"a" * secret_gate.MAX_FILE_BYTES,
        }
        for name, content in excluded.items():
            (self.wt / name).write_bytes(content)
        sh(["git", "-C", str(self.wt), "add", "-A"])
        self.assertTrue(check_commit(
            git_result(lambda argv: sh(["git", *argv], cwd=self.wt, check=False)), self.wt).ok)

    def test_worker_commit_is_blocked_and_no_commit_exists(self):
        self.staged(f'token = "{GHP}"\n')
        with self.assertRaises(SecretGateRefusal):
            self.adapter().commit_all(str(self.wt), "worker fix")
        self.assertEqual(self.commit_count(), 0)

    def test_worker_scanner_failure_holds_before_any_commit(self):
        self.staged("x = 1\n", name="app.py")
        with mock.patch.object(secret_gate, "_scan_index", side_effect=RuntimeError(GHP)):
            with self.assertRaises(SecretGateRefusal) as refused:
                self.adapter().commit_all(str(self.wt), "worker fix")
        self.assertNotIn(GHP, str(refused.exception))
        self.assertEqual(self.commit_count(), 0)

    def test_worktree_scanner_failure_holds_before_any_commit(self):
        self.staged("x = 1\n", name="app.py")
        with mock.patch.object(secret_gate, "_scan_index", side_effect=RuntimeError(GHP)):
            with self.assertRaises(SecretGateRefusal):
                worktree_commit_all(str(self.wt), "fix", "A U Thor", "a@example.com")
        self.assertEqual(self.commit_count(), 0)

    def test_worker_commit_unchanged_when_clean(self):
        (self.wt / "app.py").write_text("x = 1\n")
        sha = self.adapter().commit_all(str(self.wt), "worker fix")
        self.assertTrue(sha)
        message = sh(["git", "-C", str(self.wt), "log", "-1", "--format=%s"]).stdout
        self.assertEqual(message.strip(), "worker fix")
        names = sh(["git", "-C", str(self.wt), "show", "--name-only",
                    "--format=", "HEAD"]).stdout
        self.assertIn("app.py", names)

    def test_worktree_commit_is_blocked(self):
        self.staged(f'token = "{GHP}"\n')
        with self.assertRaises(SecretGateRefusal):
            worktree_commit_all(str(self.wt), "fix", "A U Thor", "a@example.com")
        self.assertEqual(self.commit_count(), 0)

    def test_worktree_commit_can_be_acknowledged_by_allowlist(self):
        self.staged(f'token = "{GHP}"\n')
        sha = worktree_commit_all(str(self.wt), "fix", "A U Thor",
                                  "a@example.com", allow=("leak.py:github-token",))
        self.assertTrue(sha)
        self.assertEqual(self.commit_count(), 1)

    def test_worktree_commit_unchanged_when_clean(self):
        (self.wt / "app.py").write_text("x = 1\n")
        sha = worktree_commit_all(str(self.wt), "fix", "A U Thor", "a@example.com")
        self.assertTrue(sha)

    def test_allow_file_in_the_repository_root_is_honoured(self):
        (self.wt / ".fatchin-secret-allow").write_text("leak.py:github-token\n")
        self.staged(f'token = "{GHP}"\n')
        self.assertTrue(self.adapter().commit_all(str(self.wt), "worker fix"))

    def test_git_result_adapter_shapes_a_completed_process(self):
        proc = sh(["git", "-C", str(self.wt), "status", "--porcelain"])
        code, out, err = git_result(lambda argv: proc)(["status"])
        self.assertEqual((code, out, err), (0, proc.stdout, proc.stderr))


class FleetCommitGateTests(unittest.TestCase):
    """The fleet's fix commit calls the gate before it commits."""

    def test_gate_runs_before_the_fleet_commit(self):
        source = inspect.getsource(fleet_runner)
        gate = source.index("check_commit(git_result")
        commit = source.index("commit.gpgSign=false', 'commit'")
        self.assertLess(gate, commit,
                        "the fleet commit must pass the secret gate first")

    def test_fleet_refusal_becomes_a_blocked_finding(self):
        source = inspect.getsource(fleet_runner)
        self.assertIn("except SecretGateRefusal as exc", source)
        self.assertIn("raise FleetBlocked(str(exc))", source)
        self.assertTrue(issubclass(SecretGateRefusal, RuntimeError))


if __name__ == "__main__":
    unittest.main()
