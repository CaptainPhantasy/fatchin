"""Fleet provider ids, role pinning, per-provider daily caps, pacing and the halt label."""
from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent import fleet_runner
from ghagent.config_loader import ConfigError, fleet_table_problems, load_config
from ghagent.fleet_discovery import _halted
from ghagent.fleet_providers import model_config, provider_status
from ghagent.fleet_store import FleetStore
from ghagent.interfaces import Config, ModelConfig
from ghagent.model_client import ModelClient
from tests.test_fleet_store import candidate, fleet

BROKER = {'MCP_SECRET_BROKER_ADMIN_TOKEN': 'example-broker-value'}


def glm_cfg(**fields):
    return Config(model=ModelConfig(model_id='glm-5.3-flash'), **fields)


class ProviderIdTests(unittest.TestCase):
    def test_real_model_ids_replace_placeholders(self):
        glm, _ = model_config(glm_cfg(), BROKER, 'glm')
        deepseek, _ = model_config(glm_cfg(), BROKER, 'deepseek')
        self.assertEqual(glm.model.model_id, 'glm-5.3-flash')
        self.assertEqual(deepseek.model.model_id, 'deepseek-flash')
        direct, _ = model_config(glm_cfg(), {'DEEPSEEK_API_KEY': 'example-key'}, 'deepseek')
        self.assertEqual(direct.model.model_id, 'deepseek-flash')
        self.assertEqual({row['id']: row['model_id'] for row in provider_status(glm_cfg(), BROKER)},
                         {'glm': 'glm-5.3-flash', 'deepseek': 'deepseek-flash'})

    def test_glm_pool_still_refuses_a_substituted_model(self):
        for model_id in ('your-model', 'some-other-model'):
            with self.assertRaises(ValueError):
                model_config(Config(model=ModelConfig(model_id=model_id)), BROKER, 'glm')


class DailyCapTests(unittest.TestCase):
    def test_unset_cap_keeps_continuous_fleet_behaviour(self):
        for provider in ('glm', 'deepseek'):
            resolved, _ = model_config(glm_cfg(), BROKER, provider)
            self.assertEqual(resolved.model.max_calls_per_day, sys.maxsize)

    def test_configured_cap_applies_per_provider(self):
        cfg = glm_cfg(fleet_max_calls_per_day={'glm': 4000, 'deepseek': 3000})
        self.assertEqual(model_config(cfg, BROKER, 'glm')[0].model.max_calls_per_day, 4000)
        self.assertEqual(model_config(cfg, BROKER, 'deepseek')[0].model.max_calls_per_day, 3000)
        only = glm_cfg(fleet_max_calls_per_day={'deepseek': 50})
        self.assertEqual(model_config(only, BROKER, 'glm')[0].model.max_calls_per_day, sys.maxsize)

    def test_fleet_runner_passes_the_cap_to_the_provider_ledger(self):
        seen = {}

        class Client:
            def __init__(self, cfg, bearer, **kwargs):
                seen['cfg'], seen['path'] = cfg, kwargs['day_counter_path']

        with tempfile.TemporaryDirectory() as state:
            cfg = dataclasses.replace(glm_cfg(fleet_max_calls_per_day={'deepseek': 75}), state_dir=state)
            with patch.object(fleet_runner, 'ModelClient', Client), \
                    patch.object(fleet_runner, 'make_bearer', lambda *a: (lambda: 'x')):
                with fleet_runner._model(cfg, BROKER, {'id': 'item-1', 'provider': 'deepseek'}, None):
                    pass
        self.assertEqual(seen['cfg'].max_calls_per_day, 75)
        self.assertEqual(seen['cfg'].max_calls_per_job, 100)
        self.assertTrue(seen['path'].endswith('fleet-v1/deepseek-model-call-counts.json'))


class RolePinningTests(unittest.TestCase):
    ROLES = {'review_pr': 'deepseek', 'fix_comment': 'glm'}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = FleetStore(Path(temp.name) / 'fleet-v1')

    def test_review_goes_to_deepseek_and_fix_to_glm(self):
        f = fleet(self.store, workers=2, deepseek_workers=2)
        self.store.upsert(f['id'], [candidate(1), candidate(2, 'fix_comment', comment_id='1', body='Fix it')])
        glm = self.store.claim(1, self.ROLES)
        self.assertEqual(glm['kind'], 'fix_comment')
        self.assertIsNone(self.store.claim(2, self.ROLES))
        deepseek = self.store.claim(11, self.ROLES)
        self.assertEqual((deepseek['kind'], deepseek['provider']), ('review_pr', 'deepseek'))

    def test_item_already_bound_to_a_provider_keeps_it(self):
        f = fleet(self.store, workers=2, deepseek_workers=2)
        self.store.upsert(f['id'], [candidate(1, 'fix_comment', comment_id='1', body='Fix it', provider='deepseek')])
        self.assertIsNone(self.store.claim(1, self.ROLES))
        self.assertEqual(self.store.claim(11, self.ROLES)['provider'], 'deepseek')

    def test_default_stays_slot_based(self):
        f = fleet(self.store, workers=2, deepseek_workers=2)
        self.store.upsert(f['id'], [candidate(1)])
        self.assertEqual(self.store.claim(1)['kind'], 'review_pr')

    def test_role_for_an_empty_pool_falls_back_to_slot_based(self):
        f = fleet(self.store, workers=2, deepseek_workers=0)
        self.store.upsert(f['id'], [candidate(1)])
        self.assertEqual(self.store.claim(1, self.ROLES)['kind'], 'review_pr')


class PacingTests(unittest.TestCase):
    def client(self, gap, path=None, now=1000.0):
        clock = {'t': now}
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock['t'] += seconds

        cfg = ModelConfig(base_url='http://127.0.0.1:9/v1', min_seconds_between_calls=gap)
        return ModelClient(cfg, lambda: 'x', day_counter_path=path, sleep=sleep, clock=lambda: clock['t']), sleeps, clock

    def test_default_zero_never_waits(self):
        client, sleeps, _ = self.client(0)
        client._pace()
        client._pace()
        self.assertEqual(sleeps, [])

    def test_in_process_gap(self):
        client, sleeps, clock = self.client(1.5)
        client._pace()
        clock['t'] += 0.5
        client._pace()
        self.assertEqual(sleeps, [1.0])

    def test_gap_is_shared_through_the_ledger_pace_file(self):
        with tempfile.TemporaryDirectory() as state:
            path = str(Path(state) / 'glm-model-call-counts.json')
            first, _, _ = self.client(2.0, path, now=1000.0)
            second, sleeps, _ = self.client(2.0, path, now=1000.5)
            first._pace()
            second._pace()
            self.assertEqual(sleeps, [1.5])
            self.assertTrue(Path(path + '.pace').exists())

    def test_chat_paces_after_the_spend_check(self):
        client, _, _ = self.client(0)
        with patch.object(client, '_pace', side_effect=RuntimeError('paced')) as pace:
            with self.assertRaisesRegex(RuntimeError, 'paced'):
                client.chat([{'role': 'user', 'content': 'hi'}])
        pace.assert_called_once()


class HaltLabelTests(unittest.TestCase):
    def test_halted_label_is_detected(self):
        self.assertTrue(_halted({'labels': [{'name': 'bug'}, {'name': 'fatchin-halted'}]}))
        self.assertFalse(_halted({'labels': [{'name': 'ready-to-merge'}]}))
        self.assertFalse(_halted({}))


class ConfigTableTests(unittest.TestCase):
    def test_valid_tables_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.toml'
            path.write_text(f'state_dir = "{d}/s"\nworktree_root = "{d}/w"\n'
                            '[model]\nmodel_id = "glm-5.3-flash"\nmin_seconds_between_calls = 1.5\n'
                            '[fleet_roles]\nreview_pr = "deepseek"\nfix_comment = "glm"\n'
                            '[fleet_max_calls_per_day]\nglm = 4000\ndeepseek = 3000\n')
            cfg = load_config(path)
        self.assertEqual(cfg.fleet_roles['review_pr'], 'deepseek')
        self.assertEqual(cfg.fleet_max_calls_per_day['glm'], 4000)
        self.assertEqual(cfg.model.min_seconds_between_calls, 1.5)

    def test_invalid_tables_are_rejected(self):
        bad = glm_cfg(fleet_roles={'review_pr': 'gpt', 'merge': 'glm'},
                      fleet_max_calls_per_day={'glm': 0, 'other': 5})
        bad.model.min_seconds_between_calls = -1
        self.assertEqual(len(fleet_table_problems(bad)), 5)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.toml'
            path.write_text(f'state_dir = "{d}/s"\nworktree_root = "{d}/w"\n[fleet_roles]\nreview_pr = "gpt"\n')
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
