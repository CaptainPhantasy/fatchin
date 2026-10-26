import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ghagent.fleet_store import FleetStore
from ghagent.fleet_supervisor import FleetSupervisor, owned_process, process_identity
from ghagent.interfaces import Config
from tests.test_fleet_store import fleet, candidate


class FakeChild:
    def __init__(self, pid=999999):
        self.pid = pid
        self.code = None

    def poll(self):
        return self.code


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FleetStore(Path(self.temp.name) / 'fleet')
        self.fleet = fleet(self.store, workers=1)
        self.cfg = Config(state_dir=self.temp.name, worktree_root=self.temp.name)
        self.launches = []
        self.children = []

        def launcher(argv, **kwargs):
            child = FakeChild(999990 + len(self.launches))
            self.launches.append((argv, kwargs))
            self.children.append(child)
            return child
        self.supervisor = FleetSupervisor(self.cfg, {}, self.store, config_path='/no/config', env_path='/no/env', launcher=launcher)

    def test_child_command_is_single_task_fresh_process_and_slot_rotates(self):
        self.store.upsert(self.fleet['id'], [candidate(1), candidate(2)])
        with patch('ghagent.fleet_supervisor.process_identity', return_value=None), patch('ghagent.fleet_supervisor.group_members', return_value=[]):
            self.supervisor.tick()
            self.assertEqual(len(self.launches), 1)
            first = next(i for i in self.store.internal()['items'].values() if i['status'] == 'running')
            argv, kwargs = self.launches[0]
            self.assertEqual(argv[:3], [sys.executable, '-m', 'ghagent.fleet_runner'])
            self.assertTrue(kwargs['start_new_session'])
            self.assertIn(first['lease'], argv)
            self.store.finish(first['id'], first['lease'], {'status': 'done'})
            self.children[0].code = 0
            self.supervisor.tick()
            self.assertEqual(len(self.launches), 2)
            second = next(i for i in self.store.internal()['items'].values() if i['status'] == 'running')
            self.assertNotEqual(first['lease'], second['lease'])
            self.assertNotEqual(first['id'], second['id'])
            self.assertNotEqual(first['slot'], second['slot'])

    def test_crashed_child_recovers_with_stable_intents_and_other_work_continues(self):
        self.store.upsert(self.fleet['id'], [candidate(1), candidate(2)])
        with patch('ghagent.fleet_supervisor.process_identity', return_value=None), patch('ghagent.fleet_supervisor.group_members', return_value=[]):
            self.supervisor.tick()
            first = next(i for i in self.store.internal()['items'].values() if i['status'] == 'running')
            self.store.intent(first['id'], first['lease'], 'comment', {'body_digest': 'test-digest'})
            self.children[0].code = 137
            self.supervisor.tick()
            recovered = self.store.internal()['items'][first['id']]
            self.assertEqual(recovered['status'], 'retrying')
            self.assertEqual(len(recovered['mutation_intents']), 1)
            self.assertEqual(len(self.launches), 2)

    def test_timeout_fences_admission_before_kill_and_never_recovers_live_group(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        with patch('ghagent.fleet_supervisor.process_identity', return_value=None), patch('ghagent.fleet_supervisor.group_members', return_value=[]):
            self.supervisor.tick()
            item = next(i for i in self.store.internal()['items'].values() if i['status'] == 'running')
            self.supervisor.children[item['id']]['started'] = -999999
            with patch.object(self.supervisor, '_stop_owned', return_value=True) as kill:
                self.supervisor.tick()
                self.assertEqual(kill.call_count, 1)
                self.assertFalse(self.store.alive(item['id'], item['lease']))
                self.assertEqual(self.store.internal()['items'][item['id']]['status'], 'running')

    def test_restart_recovers_proven_absent_process_and_preserves_attempt_count(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        item = self.store.claim(1)
        with patch.object(self.supervisor, '_discover_orphan', return_value=[]):
            self.supervisor.tick()
        saved = self.store.internal()['items'][item['id']]
        self.assertEqual(saved['status'], 'retrying')
        self.assertEqual(saved['attempts'], 1)

    def test_process_identity_rejects_wrong_start_and_never_signals_unrelated_pid(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.addCleanup(lambda: child.wait(timeout=5) if child.poll() is not None else None)
        record = process_identity(child.pid)
        self.assertIsNotNone(record)
        self.assertFalse(owned_process(record, 'some-item', 'some-lease'))
        with patch('ghagent.fleet_supervisor.os.killpg') as kill:
            self.assertFalse(self.supervisor._stop_owned(record, {'id': 'some-item', 'lease': 'some-lease'}))
            kill.assert_not_called()
        changed = {**record, 'start': 'different'}
        self.assertFalse(owned_process(changed, 'some-item', 'some-lease'))
        child.terminate()
        child.wait(timeout=5)

    def test_real_owned_process_group_can_be_stopped_then_recovered(self):
        self.store.upsert(self.fleet['id'], [candidate(1)])
        item = self.store.claim(1)
        # Arguments contain the same fixed runner/item/lease identity; script itself only waits.
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)',
                                  'ghagent.fleet_runner', '--item', item['id'], '--lease', item['lease']], start_new_session=True)
        try:
            record = process_identity(child.pid)
            deadline = time.monotonic() + 1
            while not owned_process(record, item['id'], item['lease']) and time.monotonic() < deadline:
                time.sleep(0.01)
                record = process_identity(child.pid)
            self.assertTrue(owned_process(record, item['id'], item['lease']))
            self.store.heartbeat(item['id'], item['lease'], 'Working', process=record)
            self.store.control('stop', self.fleet['id'])
            self.supervisor._adopt(self.store.internal()['items'][item['id']])
            self.supervisor.tick()
            child.wait(timeout=5)
            self.supervisor.tick()
            self.assertEqual(self.store.internal()['items'][item['id']]['status'], 'blocked')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_network_scan_does_not_hold_claim_lock_or_block_supervisor(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        class Discovery:
            def scan(self):
                entered.set()
                release.wait(5)
        supervisor = FleetSupervisor(self.cfg, {}, self.store, config_path='/no/config', env_path='/no/env', discovery=Discovery())
        thread = threading.Thread(target=supervisor._scan_loop, args=(stop,))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.store.upsert(self.fleet['id'], [candidate(1)])
            self.assertIsNotNone(self.store.claim(1))
        finally:
            stop.set()
            release.set()
            supervisor._scan_wake.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_default_discovery_is_wired_and_can_drive_first_claim(self):
        from tests.test_fleet_discovery import GitHub
        github = GitHub()
        github._env = {}
        with patch('ghagent.github_client.GitHubClient', return_value=github):
            supervisor = FleetSupervisor(self.cfg, {}, self.store, config_path='/no/config', env_path='/no/env', launcher=self.supervisor.launcher)
        supervisor.discovery.scan()
        with patch('ghagent.fleet_supervisor.process_identity', return_value=None), patch('ghagent.fleet_supervisor.group_members', return_value=[]):
            supervisor.tick()
        self.assertEqual(len(self.launches), 1)
        running = next(i for i in self.store.internal()['items'].values() if i['status'] == 'running')
        self.assertEqual(running['kind'], 'review_pr')
