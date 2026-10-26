"""Fleet controls and isolation at real process, file and HTTP boundaries."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests import sandbox_available
from unittest.mock import patch

from ghagent.fleet_checks import auto_repository_policy, discover_checks, run_repository_checks
from ghagent.fleet_locks import FleetBusy, repo_lock, target_lock
from ghagent.fleet_providers import model_config, provider_status
from ghagent.interfaces import Config, ModelConfig, RepoConfig
from ghagent.web_control import ApiError
from tests.test_web_control import GuiFixture


class FleetIntegrationTests(unittest.TestCase):
    def test_provider_pools_use_exact_models_and_private_runtime_settings(self):
        cfg = Config(model=ModelConfig(max_calls_per_day=600, max_calls_per_job=100))
        env = {'MCP_SECRET_BROKER_ADMIN_TOKEN': 'example-broker-value'}
        before = dataclasses.asdict(cfg)
        glm, _ = model_config(cfg, env, 'glm')
        deepseek, copied = model_config(cfg, env, 'deepseek')
        self.assertEqual(glm.model.model_id, 'your-model')
        self.assertEqual(deepseek.model.model_id, 'your-second-model')
        self.assertEqual(deepseek.model.base_url, cfg.model.broker_base_url + '/proxy/deepseek')
        self.assertEqual(deepseek.model.broker_server_id, 'deepseek')
        self.assertEqual(deepseek.model.max_calls_per_job, 100)
        self.assertGreater(deepseek.model.max_calls_per_day, 1_000_000)
        self.assertEqual(dataclasses.asdict(cfg), before)
        self.assertEqual(env, copied)
        self.assertNotIn('example-broker-value', json.dumps(provider_status(cfg, env)))
        with self.assertRaises(ValueError):
            model_config(cfg, env, 'substitute')

    def test_repository_locks_allow_different_fleet_targets_but_hold_legacy_writes(self):
        with tempfile.TemporaryDirectory() as state:
            with repo_lock(state, 'Owner/Project'), target_lock(state, 'owner/project', 'feature-a'):
                with repo_lock(state, 'owner/project', blocking=False), target_lock(state, 'owner/project', 'feature-b', blocking=False):
                    pass
                with self.assertRaises(FleetBusy):
                    with repo_lock(state, 'OWNER/PROJECT', exclusive=True, blocking=False):
                        pass
                with self.assertRaises(FleetBusy):
                    with target_lock(state, 'owner/project', 'feature-a', blocking=False):
                        pass
            with repo_lock(state, 'owner/project', exclusive=True, blocking=False):
                with self.assertRaises(FleetBusy):
                    with repo_lock(state, 'owner/project', blocking=False):
                        pass

    def test_automatic_project_checks_preserve_saved_commands_and_reject_placeholders(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / 'package.json').write_text(json.dumps({'scripts': {'test': 'echo "Error: no test specified" && exit 1', 'build': 'tsc'}}))
            self.assertEqual(discover_checks(root), ['npm run build'])
            cfg = Config()
            inferred = auto_repository_policy(cfg, None, 'owner/project', 'main', work)
            self.assertEqual(inferred.repos[0].checks, ['npm run build'])
            self.assertFalse(cfg.repos)
            cfg.repos = [RepoConfig('owner', 'project', checks=['npm run custom-test'])]
            self.assertIs(auto_repository_policy(cfg, None, 'owner/project', 'main', work), cfg)
            (root / 'package.json').unlink()
            with self.assertRaisesRegex(ValueError, 'Set verification command'):
                auto_repository_policy(Config(), None, 'owner/project', 'main', work)

    @unittest.skipUnless(sandbox_available(), 'bubblewrap is required for isolated repository checks')
    def test_actual_repository_test_runs_without_host_credentials_or_account_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'checkout'; root.mkdir()
            private = Path(tmp) / 'account-private.txt'; private.write_text('example-private-data')
            (root / '.git').mkdir()
            (root / '.git' / 'config').write_text('[core]\n\tbare = false\n')
            tests = root / 'tests'; tests.mkdir()
            (tests / 'test_isolation.py').write_text(
                'import os,pathlib,unittest\nclass Isolation(unittest.TestCase):\n'
                ' def test_account_is_not_mounted(self):\n'
                f'  self.assertFalse(pathlib.Path({str(private)!r}).exists())\n'
                '  self.assertIsNone(os.environ.get("GITHUB_TOKEN"))\n'
                '  self.assertEqual(os.environ.get("HOME"),"/tmp/home")\n'
                '  with self.assertRaises(OSError):\n'
                '   pathlib.Path(".git/config").write_text("[credential] helper=evil")\n')
            checks = discover_checks(root)
            self.assertEqual(checks, ['python3 -m unittest discover -s tests'])
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'example-parent-only'}):
                result = run_repository_checks(Config(run_command_timeout_seconds=10), root, checks)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0]['passed'], result)
            self.assertTrue(result[0]['isolated'])
            self.assertIn('Ran 1 test', result[0]['output_tail'])
            self.assertNotIn('example-parent-only', json.dumps(result))

    @unittest.skipUnless(sandbox_available(), 'bubblewrap is required for isolated repository checks')
    def test_failing_checks_are_real_and_no_unconfined_fallback_is_used(self):
        with tempfile.TemporaryDirectory() as root:
            result = run_repository_checks(Config(run_command_timeout_seconds=10), root,
                                           ['python3 -c "raise SystemExit(7)"'])
            self.assertEqual(result[0]['exit'], 7)
            self.assertFalse(result[0]['passed'])
            with patch('ghagent.fleet_checks.shutil.which', return_value=None):
                result = run_repository_checks(Config(), root, ['python3 -c "print(123)"'])
            self.assertFalse(result[0]['passed'])
            self.assertNotIn('123', result[0]['output_tail'])

    def test_deepseek_tool_protocol_preserves_opaque_reasoning_only_when_present(self):
        from tests.test_model_client import _ScriptedHTTPServer, _openai_ok
        from ghagent.model_client import ModelClient
        def respond(record, count):
            data = _openai_ok('A concise result.')
            data['choices'][0]['message']['reasoning_content'] = 'opaque-provider-state'
            return 200, {}, data
        server = _ScriptedHTTPServer(respond)
        self.addCleanup(server.close)
        model = ModelClient(ModelConfig(base_url=server.url), lambda: 'example-bearer')
        reply = model.chat([{'role': 'user', 'content': 'Test protocol.'}])
        self.assertEqual(reply['reasoning_content'], 'opaque-provider-state')
        self.assertEqual(reply['content'], 'A concise result.')


class FleetControlTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.config_path.write_text(self.config_path.read_text().replace('model_id = "model-a"', 'model_id = "your-model"'))
        self.env_path.write_text(self.env_path.read_text() + 'MCP_SECRET_BROKER_ADMIN_TOKEN=example-broker-test\n')
        self.start = {'action': 'start', 'account': 'alice', 'output_dir': str(self.output),
                      'workers': 10, 'deepseek_workers': 10, 'scope': 'all'}

    def test_twenty_agent_dispatch_binds_real_identity_and_starts_inactive_service(self):
        self.runner.active = False
        before = self.env_path.read_bytes()
        result = self.control.fleet_action({**self.start, 'github_id': 999, 'github_login': 'forged'})
        fleet = result['fleets'][0]
        self.assertEqual((fleet['github_id'], fleet['github_login']), (42, 'test-member'))
        self.assertEqual((fleet['workers'], fleet['deepseek_workers'], fleet['scope']), (10, 10, 'all'))
        self.assertEqual(result['max_workers'], 20)
        self.assertTrue(self.runner.active)
        self.assertTrue(any(argv == ['systemctl', '--user', 'start', 'gha-worker.service'] for argv, _ in self.runner.calls))
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertNotIn('example-broker-test', json.dumps(result))

    def test_deepseek_only_fleet_starts_real_components_without_glm_auth(self):
        from ghagent import cli
        from ghagent.config_loader import load_env_file
        from ghagent.worker import ProviderAuthMissing
        from tests.test_worker_loop import FakeGh
        self.env_path.write_text('DEEPSEEK_API_KEY=example-deepseek-test\n')
        self.control.fleet_action({**self.start, 'workers': 0, 'deepseek_workers': 1})
        cfg, env = self.control.config(), load_env_file(self.env_path)
        with self.assertRaises(ProviderAuthMissing):
            cli._default_components(cfg, env)
        with patch('ghagent.github_client.GitHubClient', return_value=FakeGh()):
            worker = cli._default_worker(cfg, env)
        worker.fleet_paths = (str(self.config_path), str(self.env_path))
        try:
            worker.startup()
            self.assertTrue(worker._legacy_auth_missing)
            self.assertEqual(worker._drain_jobs(), 0)
            self.assertTrue((self.state / 'health.json').exists())
        finally:
            worker.close()

    def test_missing_destination_and_invalid_capacity_never_start_work(self):
        for changes in ({'output_dir': ''}, {'workers': 11}, {'workers': True},
                        {'deepseek_workers': -1}, {'workers': 0, 'deepseek_workers': 0},
                        {'scope': 'selected', 'repos': []}):
            with self.subTest(changes=changes), self.assertRaises(ApiError):
                self.control.fleet_action({**self.start, **changes})
            self.assertFalse(self.control.fleet_store().snapshot()['fleets'])

    def test_checks_are_saved_from_the_same_view_with_backup_and_refresh(self):
        original_rest = self.gh.rest
        def rest(method, path, **kwargs):
            if path == 'repos/owner/new-project':
                return {'permissions': {'push': True}, 'default_branch': 'main'}
            if path == 'repos/owner/new-project/branches/main':
                return {'name': 'main', 'commit': {'sha': 'a' * 40}}
            return original_rest(method, path, **kwargs)
        self.gh.rest = rest
        before = self.config_path.read_bytes()
        self.control.fleet_action({'action': 'configure_checks', 'repo': 'owner/new-project',
                                   'checks': ['npm run test:ci']})
        policies = {r.full_name: r for r in self.control.config().repos}
        self.assertEqual(policies['owner/new-project'].checks, ['npm run test:ci'])
        self.assertEqual(policies['owner/repo'].checks, ['python3 -m unittest'])
        self.assertTrue(any(p.read_bytes() == before for p in self.root.glob('config.toml.bak-gui-*')))
        self.assertTrue(self.control.fleet_store().internal()['supervisor']['refresh_requested'])
        self.assertFalse(any('restart' in argv for argv, _ in self.runner.calls))

    def test_failed_service_start_keeps_visible_paused_configuration(self):
        self.runner.active = False
        original = self.control.runner
        def run(argv, **kwargs):
            if argv == ['systemctl', '--user', 'start', 'gha-worker.service']:
                return subprocess.CompletedProcess(argv, 1, '', 'example service failure')
            return original(argv, **kwargs)
        self.control.runner = run
        with self.assertRaises(ApiError) as error:
            self.control.fleet_action(self.start)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(self.control.fleet_store().snapshot()['fleets'][0]['status'], 'paused')

    def test_http_dispatch_and_state_obey_origin_boundary_and_preserve_credentials(self):
        import http.client
        import threading
        from ghagent.web import make_server
        web = self.root / 'web'; web.mkdir(); (web / 'index.html').write_text('<title>Fleet fixture</title>')
        server = make_server(self.control, '127.0.0.1', 0, web_root=web)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def request(method, origin=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            try:
                conn.request(method, '/api/fleet', json.dumps(self.start) if method == 'POST' else None, headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        try:
            code, _ = request('POST', 'https://untrusted.example')
            self.assertEqual(code, 403)
            self.assertFalse(self.control.fleet_store().snapshot()['fleets'])
            code, data = request('POST', f'http://127.0.0.1:{server.server_port}')
            self.assertEqual(code, 200, data)
            self.assertEqual(len(data['fleets']), 1)
            code, data = request('GET')
            self.assertEqual(code, 200)
            self.assertEqual(data['fleets'][0]['deepseek_workers'], 10)
            self.assertNotIn('example-broker-test', json.dumps(data))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
