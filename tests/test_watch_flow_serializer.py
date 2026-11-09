"""GUI configuration writes must preserve policy tables and prior settings."""
from datetime import date, datetime, time, timezone
import tomllib
import unittest
from unittest.mock import patch

from ghagent.control_store import ControlStore
from ghagent.web_control import ApiError, toml_document
from tests.test_web_control import GuiFixture


class NestedConfigSerializerTests(unittest.TestCase):
    def test_nested_tables_and_quoted_keys_roundtrip_without_changing_values(self):
        raw = {'poll_repos':True, 'repos':[{'owner':'owner', 'name':'repo',
                   'future.repo.setting':{'names':['one', 'two'], 'enabled':False}}],
               'model':{'future model setting':{'threshold':0.5, 'empty':{}}},
               'fleet_policy':{'personal_default':'include', 'org_default':'exclude',
                   'include':['owner/repo'], 'exclude':[], 'allow_workflows':False,
                   'by_repo':{'owner/repo':{'steps':[{'name':'Verify', 'command':'printf "hello"\n'}]}}},
               'secret_gate':{'allow':[{'kind':'safe-example', 'location':'docs/example.txt'}]}}
        self.assertEqual(tomllib.loads(toml_document(raw)), raw)

    def test_toml_date_and_time_policy_values_are_preserved(self):
        raw = {'repos':[], 'model':{}, 'fleet_policy':{
            'reviewed_at':datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc),
            'reviewed_on':date(2026, 11, 9), 'review_time':time(7, 0, 1)}}
        self.assertEqual(tomllib.loads(toml_document(raw)), raw)

    def test_unsupported_values_are_refused_instead_of_silently_dropped(self):
        with self.assertRaisesRegex(ApiError, 'unsupported configuration value'):
            toml_document({'fleet_policy':{'include':{'owner/repo'}}, 'repos':[], 'model':{}})


class GuiPolicyWriteTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        with self.config_path.open('a') as handle:
            handle.write('\n[fleet_policy]\npersonal_default = "include"\norg_default = "exclude"\n'
                         'include = ["owner/repo"]\nexclude = ["owner/old"]\nallow_workflows = false\n'
                         '[fleet_policy.overrides."owner/repo"]\nreviewers = ["member"]\n'
                         'rules = [{ name = "Keep receipts", enabled = true }]\n'
                         '[secret_gate]\nallow = [{ kind = "safe-example", path = "docs/example.txt" }]\n')
        self.original = self.control.raw_config()

    def repo_data(self, **extra):
        return {'owner':'owner', 'name':'new-repo', 'base_branch':'main',
                'checks':['python3 -m unittest'], 'revision':self.control.revision(), **extra}

    def assert_policy_preserved(self):
        raw = self.control.raw_config()
        self.assertEqual(raw['fleet_policy'], self.original['fleet_policy'])
        self.assertEqual(raw['secret_gate'], self.original['secret_gate'])
        return raw

    def test_gui_add_edit_remove_and_settings_keep_existing_policy_and_model(self):
        reply = self.control.save_repo(self.repo_data())
        self.assertTrue(reply['ok'])
        self.assertEqual(len(self.assert_policy_preserved()['repos']), 2)
        self.assertEqual(self.control.raw_config()['model'], self.original['model'])
        self.control.save_repo(self.repo_data(checks=['python3 -m unittest discover'],
                                              allow_workflows=True, original_full_name='owner/new-repo'))
        self.assertEqual(self.assert_policy_preserved()['repos'][1]['checks'], ['python3 -m unittest discover'])
        self.control.save_settings({'revision':self.control.revision(),
                                    'settings':{'poll_interval_seconds':90, 'model':{'max_tokens':2048}}})
        raw = self.assert_policy_preserved()
        self.assertEqual(raw['poll_interval_seconds'], 90)
        self.assertEqual(raw['model']['max_tokens'], 2048)
        self.assertEqual(raw['model']['broker_consumer_id'], self.original['model']['broker_consumer_id'])
        self.control.remove_repo({'repo':'owner/new-repo', 'revision':self.control.revision()})
        self.assertEqual(self.assert_policy_preserved()['repos'], self.original['repos'])
        self.assertTrue(self.control.controls().read().get('config_revision'))
        self.assertFalse(any(call[0] != 'GET' for call in self.gh.calls))

    def test_failed_restart_record_restores_exact_previous_config_and_policy(self):
        before = self.config_path.read_bytes()
        control_path = self.control.controls().path
        change = ControlStore.change
        def fail_restart_record(store, action):
            if store.path == control_path:
                raise OSError('simulated restart record failure')
            return change(store, action)
        with patch.object(ControlStore, 'change', fail_restart_record):
            with self.assertRaisesRegex(ApiError, 'previous configuration was restored'):
                self.control.save_settings({'revision':self.control.revision(),
                                            'settings':{'poll_interval_seconds':90}})
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assert_policy_preserved()

    def test_invalid_policy_value_cannot_replace_existing_config(self):
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ApiError, 'unsupported configuration value'):
            self.control.save_settings({'revision':self.control.revision(),
                'settings':{'fleet_policy':{'include':{'owner/repo'}}}})
        self.assertEqual(self.config_path.read_bytes(), before)
