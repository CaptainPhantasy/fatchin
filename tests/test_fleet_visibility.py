"""Workspace views share real fleet coverage instead of manual worker settings."""
import json
import unittest

from tests.test_web_control import GuiFixture
from tests.test_fleet_store import candidate


class FleetVisibilityTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.store = self.control.fleet_store()
        self.fleet = self.store.configure({'account': 'alice', 'github_id': 42,
            'github_login': 'test-member', 'output_dir': str(self.output),
            'member_home': str(self.home), 'workers': 10, 'deepseek_workers': 10,
            'enabled_providers': ['glm', 'deepseek'], 'scope': 'all'})
        self.catalog = [{'repo': 'owner/repo', 'id': 42, 'private': True}] + [
            {'repo': f'owner/project-{i:03d}', 'id': 1000+i, 'private': False} for i in range(255)]
        self.store.upsert(self.fleet['id'], [candidate(i, repo=self.catalog[i % 5]['repo'], repo_id=self.catalog[i % 5]['id']) for i in range(1, 21)], repositories=self.catalog)

    def test_state_reports_all_256_projects_and_both_ten_agent_pools(self):
        claims = [self.store.claim(slot) for slot in range(1, 21)]
        self.assertTrue(all(claims))
        self.store.intent(claims[0]['id'], claims[0]['lease'], 'comment', {'body': 'example-private-intent'})
        before = self.store.path.stat().st_mtime_ns
        state = self.control.state()
        overview = state['fleet_summary']
        self.assertEqual(len(state['repos']), 1, 'Saved verification policies remain distinct from watch coverage.')
        self.assertEqual(overview['repository_count'], 256)
        self.assertEqual(len(overview['repositories']), 256)
        self.assertEqual(overview['active_agents'], {'glm': 10, 'deepseek': 10})
        self.assertEqual(overview['configured_agents'], {'glm': 10, 'deepseek': 10})
        self.assertEqual(overview['task_counts']['running'], 20)
        self.assertNotIn('example-private-intent', json.dumps(overview))
        self.assertNotIn(claims[0]['lease'], json.dumps(overview))
        self.assertEqual(self.store.path.stat().st_mtime_ns, before)

    def test_updates_and_publishing_checks_include_fleet_projects_without_false_check_claims(self):
        self.control.watches().put('owner/repo', '*', {'scope': 'all'})
        digest = self.control.digest()
        self.assertEqual(len({r['repo'] for r in digest['watched']}), 256)
        fence = self.control.fence()
        by_name = {r['repo']: r for r in fence['repos']}
        self.assertEqual(len(by_name), 256)
        self.assertTrue(by_name['owner/repo']['fleet_managed'])
        self.assertEqual(by_name['owner/project-254']['protection'], 'automatic')
        self.assertEqual(by_name['owner/project-254']['checks_configured'], 0)

    def test_paused_and_stopped_fleets_are_distinguished_without_changing_saved_manual_watches(self):
        self.control.watches().put('owner/repo', '*', {'scope': 'all'})
        before = self.control.watches().read()
        self.store.control('pause', self.fleet['id'])
        self.assertTrue(all(r['status'] == 'paused' for r in self.control.state()['fleet_summary']['repositories']))
        self.store.control('stop', self.fleet['id'])
        self.assertEqual(self.control.state()['fleet_summary']['repositories'], [])
        self.assertEqual(self.control.watches().read(), before)
