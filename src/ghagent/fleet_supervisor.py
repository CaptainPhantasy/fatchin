"""One coordinator, bounded single-task child processes, and independent discovery."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .fleet_store import MAX_WORKERS, now


def process_identity(pid):
    """Linux boot/start identity prevents recycled PIDs being treated as our children."""
    try:
        root = Path('/proc') / str(int(pid))
        stat = (root / 'stat').read_text()
        fields = stat[stat.rindex(')') + 2:].split()
        return {'pid': int(pid), 'start': fields[19], 'pgrp': int(fields[2]), 'session': int(fields[3]),
                'uid': root.stat().st_uid, 'state': fields[0],
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'argv': (root / 'cmdline').read_bytes().decode(errors='replace').split('\0')[:-1]}
    except (OSError, ValueError, IndexError):
        return None


def _matches(record, actual):
    return bool(actual and all(actual.get(key) == record.get(key) for key in ('pid', 'start', 'pgrp', 'session', 'uid', 'boot')))


def owned_process(record, item_id, lease):
    actual = process_identity(record.get('pid'))
    if not _matches(record, actual) or actual['uid'] != os.getuid():
        return False
    argv = actual['argv']
    return (actual['pgrp'] == actual['pid'] and actual['session'] == actual['pid']
            and 'ghagent.fleet_runner' in argv and '--item' in argv and '--lease' in argv
            and argv[argv.index('--item') + 1:argv.index('--item') + 2] == [item_id]
            and argv[argv.index('--lease') + 1:argv.index('--lease') + 2] == [lease])


def group_members(record):
    """Only the original session can retain this group after the child leader exits."""
    current_leader = process_identity(record.get('pid'))
    if current_leader and not _matches(record, current_leader):
        return []  # The old session ended; a recycled PID belongs to somebody else.
    members = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        actual = process_identity(path.name)
        if (actual and actual['uid'] == record.get('uid') and actual['boot'] == record.get('boot')
                and actual['pgrp'] == record.get('pgrp') and actual['session'] == record.get('session')
                and actual['state'] != 'Z'):
            members.append(actual)
    return members


class FleetSupervisor:
    def __init__(self, cfg, env, store, *, config_path, env_path, discovery=None, launcher=None):
        self.cfg, self.env, self.store = cfg, env, store
        self.config_path, self.env_path = str(config_path), str(env_path)
        if discovery is None:
            from .fleet_discovery import FleetDiscovery
            from .github_client import GitHubClient
            gh = GitHubClient(host=cfg.github_host, api_version=cfg.rest_api_version)
            for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
                if env.get(key):
                    gh._env[key] = env[key]
            discovery = FleetDiscovery(gh, cfg, store)
        self.discovery = discovery
        self.launcher = launcher or subprocess.Popen
        self.children = {}
        self._cursor = 1
        self._scan_wake = threading.Event()
        self._scanner = None
        self._scanner_stop = threading.Event()
        self._clock = time.monotonic
        self.heartbeat_timeout = max(300, int(getattr(cfg.model, 'request_timeout_seconds', 180)) * 2)
        self.task_timeout = max(900, int(getattr(cfg, 'run_command_timeout_seconds', 600)) * 3)

    def _scan_loop(self, stop_event):
        interval = max(15, int(getattr(self.cfg, 'poll_interval_seconds', 120)))
        while not stop_event.is_set() and not self._scanner_stop.is_set():
            try:
                if self.discovery:
                    # Settings changes are picked up without restarting the worker.
                    if Path(self.config_path).is_file():
                        from .config_loader import load_config
                        self.discovery.cfg = load_config(self.config_path)
                    self.discovery.scan()
                self.store.supervisor_status(last_scan_at=now())
            except Exception:
                self.store.supervisor_status(discovery_error='Discovery failed. The original state is retained and will be retried.')
            retry_at = self.store.internal()['supervisor'].get('discovery_retry_at')
            delay = interval
            if retry_at:
                delay = max(delay, -self._age(retry_at))
            self._scan_wake.wait(min(86400, delay))
            self._scan_wake.clear()

    def _discover_orphan(self, item):
        # Covers a supervisor crash between Popen and saving its process identity.
        matches = []
        for path in Path('/proc').iterdir():
            if path.name.isdigit():
                actual = process_identity(path.name)
                if actual and owned_process(actual, item['id'], item['lease']):
                    matches.append(actual)
        return matches

    def _stop_owned(self, record, item, *, force=False):
        leader = process_identity(record.get('pid'))
        leader_owned = owned_process(record, item['id'], item['lease'])
        # A session ID remains reserved while descendants survive its leader.
        orphan_owned = (leader is None and record.get('boot') and record.get('start')
                        and record.get('uid') == os.getuid() and bool(group_members(record)))
        if not leader_owned and not orphan_owned:
            return False
        try:
            os.killpg(record['pgrp'], signal.SIGKILL if force else signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False

    def _adopt(self, item):
        record = item.get('process')
        if not record:
            matches = self._discover_orphan(item)
            if len(matches) > 1:
                self.store.supervisor_status(error='Multiple processes claim one item; automatic reset is waiting for unambiguous ownership.')
                return
            if matches:
                record = matches[0]
                self.store.heartbeat(item['id'], item['lease'], item.get('stage', 'Recovered worker'), process=record)
        if record and owned_process(record, item['id'], item['lease']):
            self.children[item['id']] = {'process': None, 'record': record, 'lease': item['lease'],
                                         'started': self._clock(), 'stop_at': None}
        elif record and group_members(record):
            self.store.fence(item['id'], item['lease'], 'The previous leader exited with subprocesses still running.')
            self.children[item['id']] = {'process': None, 'record': record, 'lease': item['lease'],
                                         'started': self._clock(), 'stop_at': self._clock()}
            self._stop_owned(record, item)
        else:
            self.store.recover(item['id'], item['lease'], 'The previous task process exited; the next attempt will reconcile saved writes.')

    def _launch(self, item):
        argv = [sys.executable, '-m', 'ghagent.fleet_runner', '--config', self.config_path,
                '--env-file', self.env_path, '--item', item['id'], '--lease', item['lease']]
        try:
            child = self.launcher(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            record = process_identity(child.pid)
            if record is None:
                # Child can finish before /proc is read. Its own durable finish decides success.
                record = {'pid': child.pid, 'start': '', 'uid': os.getuid(), 'pgrp': child.pid,
                          'session': child.pid, 'boot': '', 'argv': argv}
            self.children[item['id']] = {'process': child, 'record': record, 'lease': item['lease'],
                                         'started': self._clock(), 'stop_at': None}
            self.store.heartbeat(item['id'], item['lease'], 'Starting a fresh task', process=record)
        except Exception:
            # A launcher exception before a returned PID has no known dispatched child.
            matches = self._discover_orphan(item)
            if matches:
                self.children[item['id']] = {'process': None, 'record': matches[0], 'lease': item['lease'],
                                             'started': self._clock(), 'stop_at': None}
            else:
                self.store.recover(item['id'], item['lease'], 'Could not start the task process.')

    @staticmethod
    def _age(stamp):
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
        except (ValueError, TypeError):
            return float('inf')

    def tick(self):
        data = self.store.internal()
        for item in list(data['items'].values()):
            if item['status'] == 'running' and item['id'] not in self.children:
                self._adopt(item)
        for item_id, child in list(self.children.items()):
            item = data['items'].get(item_id)
            proc, record = child['process'], child['record']
            exited = proc.poll() is not None if proc is not None else not owned_process(record, item_id, child['lease'])
            if exited and not group_members(record):
                self.children.pop(item_id, None)
                if item and item['status'] == 'running':
                    self.store.recover(item_id, child['lease'], 'Task exited without a completion receipt; saved writes will be reconciled.')
                continue
            if not item:
                continue
            stop = (item.get('fenced') or data['fleets'][item['fleet_id']]['status'] == 'stopped'
                    or self._clock() - child['started'] > self.task_timeout
                    or self._age(item.get('heartbeat_at')) > self.heartbeat_timeout)
            if stop:
                if child['stop_at'] is None:
                    self.store.fence(item_id, child['lease'], 'The agent stopped responding or was stopped.')
                    self._stop_owned(record, item)
                    child['stop_at'] = self._clock()
                elif self._clock() - child['stop_at'] > 5:
                    self._stop_owned(record, item, force=True)
        # Each completion frees a new process. Rotating logical slots preserves independent review at concurrency one.
        busy_slots = {data['items'][item_id].get('slot') for item_id in self.children if item_id in data['items']}
        last_claimed = None
        for _ in range(MAX_WORKERS):
            if len(self.children) >= MAX_WORKERS:
                break
            slot = self._cursor
            self._cursor = slot % MAX_WORKERS + 1
            if slot in busy_slots:
                continue
            claimed = self.store.claim(slot)
            if claimed:
                self._launch(claimed)
                last_claimed = slot
        if last_claimed is not None:
            self._cursor = last_claimed % MAX_WORKERS + 1
        for fleet in data['fleets'].values():
            if fleet.get('status') in {'running', 'paused'}:
                self.store.evaluate_drain(fleet['id'])
        self.store.supervisor_status(status='running', pid=os.getpid(), active=len(self.children))

    def run(self, stop_event):
        self.store.root.mkdir(parents=True, exist_ok=True)
        with (self.store.root / 'supervisor.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._scanner_stop.clear()
            self._scanner = threading.Thread(target=self._scan_loop, args=(stop_event,), daemon=True, name='fleet-discovery')
            self._scanner.start()
            last_refresh = None
            try:
                while not stop_event.is_set():
                    self.tick()
                    refresh = self.store.internal()['supervisor'].get('refresh_requested')
                    if refresh != last_refresh:
                        self._scan_wake.set()
                        last_refresh = refresh
                    stop_event.wait(0.5)
            finally:
                self._scanner_stop.set()
                self._scan_wake.set()
                # Signal only verified owned groups; next supervisor adopts/reconciles any surviving task.
                data = self.store.internal()
                for item_id, child in self.children.items():
                    item = data['items'].get(item_id)
                    if item:
                        self._stop_owned(child['record'], item)
                if self._scanner:
                    self._scanner.join(timeout=1)
                self.store.supervisor_status(status='stopped')
                fcntl.flock(lock, fcntl.LOCK_UN)
