"""Explicit provider choices must select the connection the person was offered."""
import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent.config_loader import load_env_file
from ghagent.control_store import ControlStore
from ghagent.fleet_providers import model_config, provider_status
from ghagent.web_control import ApiError
from tests.test_web_control import GuiFixture


class ExplicitDeepSeekSourceTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.build_gui()

    def both_saved(self):
        with self.env_path.open('a') as handle:
            handle.write('MCP_SECRET_BROKER_ADMIN_TOKEN=fake-existing-broker\nDEEPSEEK_API_KEY=fake-dedicated-key\n')

    def resolved(self):
        return model_config(self.control.config(), load_env_file(self.env_path), 'deepseek')

    def test_legacy_broker_preference_remains_until_person_selects_a_source(self):
        self.both_saved()
        cfg, values = self.resolved()
        self.assertEqual(cfg.model.auth_mode, 'broker-lease')
        self.assertNotEqual(values['GHA_MODEL_API_KEY'], 'fake-dedicated-key')
        rows = provider_status(self.control.config(), values)
        row = next(row for row in rows if row['id'] == 'deepseek')
        self.assertEqual(row['credential_source'], 'broker-lease')
        self.assertIn('Company broker', row['credential_source_label'])
        self.assertNotIn('fake-', json.dumps(rows))

    def test_select_existing_dedicated_key_uses_it_even_with_saved_broker(self):
        self.both_saved()
        with patch('ghagent.model_client.ModelClient') as Client:
            reply = self.control.set_provider_source({'provider':'deepseek', 'source':'env'})
            Client.assert_not_called()
        cfg, values = self.resolved()
        self.assertEqual(cfg.model.auth_mode, 'env')
        self.assertEqual(cfg.model.base_url, 'https://api.deepseek.com')
        self.assertEqual(values['GHA_MODEL_API_KEY'], 'fake-dedicated-key')
        self.assertEqual(values['MCP_SECRET_BROKER_ADMIN_TOKEN'], 'fake-existing-broker')
        self.assertTrue(reply['restart_required'])
        self.assertEqual(reply['provider']['credential_source'], 'env')
        self.assertEqual(self.control.controls().read()['config_revision'], self.control.revision())
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        history = ControlStore(self.state / 'provider-source-history.json').read()['deepseek']
        self.assertEqual(history[-1]['selected_source'], 'env')
        self.assertFalse(history[-1]['credential_changed'])
        self.assertNotIn('fake-', json.dumps(reply) + json.dumps(history))

    def test_selecting_existing_broker_does_not_replace_its_provenance(self):
        self.both_saved()
        self.control.credential({'kind':'broker','value':'fake-existing-broker','supplied_by':'Original supplier'})
        records = self.env_path.parent / 'credential-records.json'
        before = records.read_bytes()
        self.control.set_provider_source({'provider':'deepseek', 'source':'broker-lease'})
        self.assertEqual(records.read_bytes(), before)
        self.assertEqual(self.resolved()[0].model.auth_mode, 'broker-lease')

    def test_missing_selected_key_does_not_fall_back_to_existing_broker(self):
        with self.env_path.open('a') as handle:
            handle.write('MCP_SECRET_BROKER_ADMIN_TOKEN=fake-existing-broker\n')
        before = self.env_path.read_bytes()
        with self.assertRaisesRegex(ApiError, 'no DeepSeek key is saved'):
            self.control.set_provider_source({'provider':'deepseek', 'source':'env'})
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertFalse((self.state / 'provider-source-history.json').exists())

    def test_missing_selected_broker_does_not_fall_back_to_existing_key(self):
        with self.env_path.open('a') as handle:
            handle.write('DEEPSEEK_API_KEY=fake-dedicated-key\n')
        before = self.env_path.read_bytes()
        with self.assertRaisesRegex(ApiError, 'credential is missing'):
            self.control.set_provider_source({'provider':'deepseek', 'source':'broker-lease'})
        self.assertEqual(self.env_path.read_bytes(), before)

    def test_source_validation_refuses_unrecognized_provider_or_choice(self):
        for data in ({'provider':'glm','source':'env'}, {'provider':'deepseek','source':'automatic'}, {}):
            with self.subTest(data=data), self.assertRaises(ApiError):
                self.control.set_provider_source(data)

    def test_new_dedicated_key_can_explicitly_select_its_connection(self):
        self.both_saved()
        reply = self.control.credential({'kind':'deepseek','value':'fake-replacement-key',
            'supplied_by':'A person','provider_source':'env'})
        cfg, values = self.resolved()
        self.assertEqual(cfg.model.auth_mode, 'env')
        self.assertEqual(values['GHA_MODEL_API_KEY'], 'fake-replacement-key')
        self.assertEqual(reply['record']['selected_deepseek_source'], 'env')
        self.assertNotIn('fake-', json.dumps(reply))

    def test_credential_save_without_selection_preserves_effective_legacy_source(self):
        self.both_saved()
        self.control.credential({'kind':'deepseek','value':'fake-new-key','supplied_by':'A person'})
        self.assertEqual(self.resolved()[0].model.auth_mode, 'broker-lease')
        self.assertNotIn('FATCHIN_DEEPSEEK_AUTH_MODE', load_env_file(self.env_path))

    def test_broker_alias_can_explicitly_select_company_connection(self):
        self.both_saved()
        self.control.set_provider_source({'provider':'deepseek','source':'env'})
        reply = self.control.credential({'kind':'deepseek-broker','value':'fake-new-broker',
            'supplied_by':'A person','provider_source':'broker-lease'})
        self.assertEqual(self.resolved()[0].model.auth_mode, 'broker-lease')
        self.assertEqual(load_env_file(self.env_path)['MCP_SECRET_BROKER_ADMIN_TOKEN'], 'fake-new-broker')
        self.assertEqual(reply['record']['selected_deepseek_source'], 'broker-lease')

    def test_incompatible_credential_selection_does_not_replace_saved_material(self):
        self.both_saved()
        before = self.env_path.read_bytes()
        for kind, source in (('deepseek','broker-lease'),('broker','env'),('model','env')):
            with self.subTest(kind=kind), self.assertRaises(ApiError):
                self.control.credential({'kind':kind,'value':'fake-wrong-key','supplied_by':'A person','provider_source':source})
            self.assertEqual(self.env_path.read_bytes(), before)

    def test_failed_choice_history_write_restores_old_connection_and_controls(self):
        self.both_saved()
        before = self.env_path.read_bytes()
        original = ControlStore.change
        def fail_history(store, action):
            if store.path.name == 'provider-source-history.json':
                raise OSError('simulated storage failure')
            return original(store, action)
        with patch.object(ControlStore, 'change', fail_history):
            with self.assertRaisesRegex(ApiError, 'previous choice and credentials were restored'):
                self.control.set_provider_source({'provider':'deepseek','source':'env'})
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertFalse(self.control.controls().path.exists())

    def test_connection_test_uses_the_selected_source_and_sends_only_once(self):
        self.both_saved()
        for source in ('env', 'broker-lease'):
            with self.subTest(source=source):
                self.control.set_provider_source({'provider':'deepseek', 'source':source})
                with patch('ghagent.wiring.make_bearer') as bearer, patch('ghagent.model_client.ModelClient') as Client:
                    reply = self.control.check_fleet_provider('deepseek', {'confirmed':True})
                self.assertTrue(reply['ok'])
                cfg, values = bearer.call_args.args
                self.assertEqual(cfg.model.auth_mode, source)
                self.assertEqual(cfg.model.max_calls_per_job, 1)
                if source == 'env':
                    self.assertEqual(values['GHA_MODEL_API_KEY'], 'fake-dedicated-key')
                else:
                    self.assertEqual(values['MCP_SECRET_BROKER_ADMIN_TOKEN'], 'fake-existing-broker')
                self.assertEqual(Client.call_args.kwargs['max_attempts'], 1)
                Client.return_value.chat.assert_called_once()
                bearer.return_value.close.assert_called_once()

    def test_selected_missing_source_reports_an_actionable_status(self):
        env = {'DEEPSEEK_API_KEY':'fake-dedicated-key','FATCHIN_DEEPSEEK_AUTH_MODE':'broker-lease'}
        row = next(row for row in provider_status(self.control.config(), env) if row['id']=='deepseek')
        self.assertFalse(row['available'])
        self.assertEqual(row['credential_source'], 'broker-lease')
        self.assertIn('credential is missing', row['error'])
        self.assertIn('Settings', row['next_step'])
