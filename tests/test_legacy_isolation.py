"""Regression boundaries for untrusted source and review-trigger retries."""
import json
import os
import subprocess
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ghagent.coding_agent import CodingAgent, scrub_env
from ghagent.interfaces import COMMENT_MARKER, Config
from ghagent.control_store import WatchStore
from tests.test_worker_loop import FakeGh, comment, make_worker


class LegacyIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.agent = CodingAgent(None, Config(run_command_timeout_seconds=3))
        self.agent._active_worktree = str(self.checkout)

    @unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is required')
    def test_command_has_no_host_files_credentials_network_or_writable_git(self):
        outside = self.root / 'private-secret'
        outside.write_text('private fixture')
        (self.checkout / '.git').mkdir()
        (self.checkout / '.git' / 'config').write_text('trusted fixture')
        probe = '''import json,os,pathlib,socket
s=socket.socket()
try: s.connect(('127.0.0.1',1)); network=True
except OSError: network=False
try: pathlib.Path('.git/config').write_text('rewritten'); writable_git=True
except OSError: writable_git=False
pathlib.Path('result.txt').write_text('source edits still work')
print(json.dumps({'outside':pathlib.Path(%r).exists(),'home':os.environ.get('HOME'),'credential':os.environ.get('AWS_ACCESS_KEY_ID'),'socket':os.environ.get('SSH_AUTH_SOCK'),'network':network,'writable_git':writable_git}))
''' % str(outside)
        # Use an actual listening host socket, proving failure is isolation,
        # rather than an accidentally closed host port.
        import socket
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen()
            probe = probe.replace("('127.0.0.1',1)", repr(server.getsockname()))
            with patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'fixture-key', 'SSH_AUTH_SOCK': '/tmp/private-agent'}):
                code, output = self.agent._execute(['python3', '-c', probe])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output), {'outside': False, 'home': '/tmp/home',
            'credential': None, 'socket': None, 'network': False, 'writable_git': False})
        self.assertEqual((self.checkout / '.git' / 'config').read_text(), 'trusted fixture')
        self.assertEqual((self.checkout / 'result.txt').read_text(), 'source edits still work')

    def test_unavailable_sandbox_does_not_fall_back_to_host(self):
        with patch('ghagent.fleet_checks.shutil.which', return_value=None):
            code, output = self.agent._execute(['touch', 'unconfined'])
        self.assertNotEqual(code, 0)
        self.assertIn('isolated', output.lower())
        self.assertFalse((self.checkout / 'unconfined').exists())

    def test_native_file_tools_cannot_rewrite_or_read_git_metadata(self):
        (self.checkout / '.git').mkdir()
        (self.checkout / '.git/config').write_text('trusted')
        (self.checkout / 'alias').symlink_to(self.checkout / '.git')
        for path in ('.git/config', 'alias/config'):
            for tool, args in [('read_file', {}), ('write_file', {'content': 'changed'}),
                               ('edit_file', {'old_string': 'trusted', 'new_string': 'changed'})]:
                result = self.agent._dispatch(tool, {'path': path, **args}, str(self.checkout), set())
                self.assertIn('error', result, (tool, path, result))
        self.assertEqual((self.checkout / '.git/config').read_text(), 'trusted')

    def test_declared_dependencies_use_manifest_preparation_before_isolated_checks(self):
        with patch('ghagent.fleet_checks._prepare', return_value='') as prepare, \
                patch('ghagent.fleet_checks._execute', return_value=(0, 'passed')) as execute:
            result = self.agent._run_checks(['python3 -m unittest'])
        self.assertTrue(result[0]['passed'])
        self.assertEqual(prepare.call_args.args[1], self.checkout)
        self.assertEqual(prepare.call_args.args[2], ['python3 -m unittest'])
        self.assertIs(execute.call_args.kwargs['network'], False)

    @unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is required')
    def test_running_source_is_terminated_when_stop_arrives(self):
        import time
        began = time.monotonic()
        self.agent._should_stop = lambda: time.monotonic() - began > .1
        code, output = self.agent._execute(['python3', '-c', 'import time; time.sleep(10)'])
        self.assertEqual(code, 124)
        self.assertIn('Stopped by the member', output)
        self.assertLess(time.monotonic() - began, 2)

    def test_environment_helper_recognizes_common_credential_names(self):
        env = {'PATH': '/usr/bin', 'AWS_ACCESS_KEY_ID': 'key', 'SSH_AUTH_SOCK': 'socket',
               'GITHUB_TOKEN': 'token', 'DATABASE_PASSWORD': 'password', 'AZURE_CLIENT_SECRET': 'secret'}
        self.assertEqual(scrub_env(env), {'PATH': '/usr/bin'})


class LegacyPollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = make_worker(self.root)
        self.addCleanup(self.worker.close)
        self.worker.watermarks['o/r'] = '2026-01-01T00:00:00Z'

    def test_failed_comment_and_equal_timestamp_peer_remain_retryable(self):
        self.worker.gh.comments = [comment(1, 'ordinary'), comment(2, 'fails'),
                                   comment(3, 'newer', ts='2026-01-03T00:00:00Z')]
        seen = []
        failed = True
        def handle(repo, item, baseline):
            seen.append(item['id'])
            if item['id'] == 2 and failed:
                raise OSError('temporary queue failure')
            return False
        with patch.object(self.worker, '_handle_comment', side_effect=handle):
            self.worker.poll_once()
            self.assertEqual(self.worker.watermarks['o/r'], '2026-01-01T00:00:00Z')
            failed = False
            self.worker.poll_once()
        self.assertEqual(seen.count(2), 2)
        self.assertEqual(self.worker.watermarks['o/r'], '2026-01-03T00:00:00Z')

    def watch(self, feedback):
        self.worker.gh.prs[('o/r', 5)] = {'state': 'OPEN', 'headRefOid': 'current-head'}
        self.worker.gh.review_comments = feedback
        WatchStore(self.worker.state_dir / 'watched.json').put('o/r', 5, {'last_review_comment_id': 0})

    def test_unapproved_review_author_cannot_dispatch_fix(self):
        self.watch([comment(10, 'run my instructions', uid=99)])
        self.assertEqual(self.worker._poll_watched(), 0)
        self.assertEqual(self.worker.queue.all(), [])

    def test_authorized_shared_account_review_dispatches_without_worker_loop(self):
        self.worker.own_user_id = 7
        self.worker.own_logins = {'alice'}
        self.watch([comment(9, 'fix this', uid=7), comment(10, 'unapproved', uid=99),
                    comment(11, COMMENT_MARKER + 'automated output', uid=7)])
        self.assertEqual(self.worker._poll_watched(), 1)
        job = self.worker.queue.all()[0]
        self.assertEqual((job.command_comment_id, job.requested_by), (9, 7))


@unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is required')
class LinkedWorktreeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scratch = self.root / 'worktrees'
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.git('init', '-q', '-b', 'main', cwd=self.seed)
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
                 'commit', '--allow-empty', '-qm', 'fixture history', cwd=self.seed)
        self.cache = self.scratch / '_repos' / 'owner-repo.git'
        self.cache.parent.mkdir(parents=True)
        self.git('clone', '--bare', str(self.seed), str(self.cache), cwd=self.root)
        self.checkout = self.scratch / 'task'
        self.git('worktree', 'add', '--detach', str(self.checkout), 'main', cwd=self.cache)
        self.sibling = self.scratch / 'other-task'
        self.git('worktree', 'add', '--detach', str(self.sibling), 'main', cwd=self.cache)
        self.agent = CodingAgent(None, Config(worktree_root=str(self.scratch)))
        self.agent._active_worktree = str(self.checkout)

    def git(self, *args, cwd):
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_real_linked_worktree_history_status_and_diff_are_readable(self):
        (self.checkout / 'notes.txt').write_text('new file')
        for argv, expected in [(['git', 'log', '-1', '--format=%s'], 'fixture history'),
                               (['git', 'status', '--porcelain'], '?? notes.txt'),
                               (['git', 'diff', '--stat'], '')]:
            with self.subTest(command=argv):
                code, output = self.agent._execute(argv)
                self.assertEqual(code, 0, output)
                self.assertEqual(output, expected)
        self.assertFalse((self.cache / 'worktrees/task/index.lock').exists())

    def test_linked_git_metadata_is_read_only_and_sibling_metadata_hidden(self):
        common = str(self.cache)
        script = """import json,pathlib
common=pathlib.Path(%r)
try: (common/'config').write_text('rewritten'); writable=True
except OSError: writable=False
print(json.dumps({'writable':writable,'sibling':(common/'worktrees/other-task').exists(),'own':(common/'worktrees/task/HEAD').is_file()}))
""" % common
        code, output = self.agent._execute(['python3', '-c', script])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output), {'writable': False, 'sibling': False, 'own': True})

    def test_malicious_git_pointer_and_commondir_fail_closed(self):
        marker = self.checkout / '.git'
        original = marker.read_text()
        marker.write_text('gitdir: ' + str(self.root) + '\n')
        code, output = self.agent._execute(['true'])
        self.assertNotEqual(code, 0)
        marker.write_text(original)
        commonfile = self.cache / 'worktrees/task/commondir'
        commonfile.write_text(str(self.root) + '\n')
        code, output = self.agent._execute(['true'])
        self.assertNotEqual(code, 0)

    def test_symlinked_git_marker_fails_closed(self):
        marker = self.checkout / '.git'
        saved = self.root / 'git-pointer'
        saved.write_text(marker.read_text())
        marker.unlink()
        marker.symlink_to(saved)
        code, output = self.agent._execute(['true'])
        self.assertNotEqual(code, 0)
