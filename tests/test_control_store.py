"""Shared GUI/worker records: durable updates, writer coordination, no data loss."""
from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from ghagent.control_store import ControlStore, WatchStore


def _increment_record(path, count):
    store = ControlStore(path)
    for _ in range(count):
        store.change(lambda value: value.update(count=value.get('count', 0) + 1))


class ControlStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'records.json'

    def test_changes_survive_a_new_instance_and_keep_other_fields(self):
        first = ControlStore(self.path)
        first.change(lambda value: value.update(polling_paused=True, other='retained'))
        second = ControlStore(self.path)
        second.change(lambda value: value.update(polling_paused=False))
        self.assertEqual(first.read(), {'polling_paused': False, 'other': 'retained'})

    def test_processes_do_not_lose_one_anothers_updates(self):
        processes = [multiprocessing.Process(target=_increment_record, args=(self.path, 20)) for _ in range(3)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(2)
                self.fail('Concurrent record writer did not finish.')
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(ControlStore(self.path).read()['count'], 60)

    def test_corrupt_or_wrong_shape_state_is_preserved(self):
        for content in ('{broken', '[1, 2]'):
            with self.subTest(content=content):
                self.path.write_text(content)
                store = ControlStore(self.path)
                with self.assertRaises(RuntimeError):
                    store.read()
                with self.assertRaises(RuntimeError):
                    store.change(lambda value: value.update(replacement=True))
                self.assertEqual(self.path.read_text(), content)

    def test_failed_change_does_not_publish_partial_state(self):
        store = ControlStore(self.path)
        store.change(lambda value: value.update(original=True))
        before = self.path.read_bytes()
        def fail(value):
            value['partial'] = True
            raise ValueError('write aborted')
        with self.assertRaises(ValueError):
            store.change(fail)
        self.assertEqual(self.path.read_bytes(), before)

    def test_removing_all_scope_retains_individually_selected_watches(self):
        store = WatchStore(self.path)
        store.put('owner/repo', '*', {'scope': 'all', 'member': 'alice'})
        store.put('owner/repo', 1, {'scope': 'all', 'last_review_comment_id': 10})
        store.put('owner/repo', 2, {'scope': 'selected', 'last_review_comment_id': 12})
        store.remove('owner/repo', '*')
        self.assertEqual(store.read(), {'owner/repo': {'2': {'scope': 'selected', 'last_review_comment_id': 12}}})

    def test_stale_worker_cannot_recreate_a_removed_watch(self):
        store = WatchStore(self.path)
        store.put('owner/repo', 1, {'scope': 'selected', 'last_review_comment_id': 4})
        stale_meta = store.read()['owner/repo']['1']
        store.remove('owner/repo', 1)
        stale_meta['last_review_comment_id'] = 9
        self.assertFalse(store.put('owner/repo', 1, stale_meta, only_existing=True))
        self.assertNotIn('1', store.read().get('owner/repo', {}))

    def test_new_watch_cannot_reset_an_existing_cursor(self):
        store = WatchStore(self.path)
        store.put('owner/repo', 1, {'last_review_comment_id': 20})
        self.assertFalse(store.put('owner/repo', 1, {'last_review_comment_id': 0}, only_new=True))
        self.assertEqual(json.loads(self.path.read_text())['owner/repo']['1']['last_review_comment_id'], 20)
