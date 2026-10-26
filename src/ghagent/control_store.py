"""Small shared control records for the CLI worker and graphical interface.

Each read/modify/write holds a process lock. Invalid state is an error, never
an empty replacement. Backups are retained before configuration replacement.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_bytes(path: Path, content: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def restore_optional(path: Path, previous: bytes | None):
    if previous is None:
        path.unlink(missing_ok=True)  # Only a newly created file is removed.
    else:
        atomic_bytes(path, previous)


class ControlStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + '.lock', 'a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise RuntimeError(f'{self.path.name} cannot be read; existing state was preserved.') from error
        if not isinstance(value, dict):
            raise RuntimeError(f'{self.path.name} has an invalid structure; existing state was preserved.')
        return value

    def read(self):
        with self.lock():
            return self._read()

    def change(self, action):
        with self.lock():
            value = self._read()
            result = action(value)
            atomic_json(self.path, value)
            return result


class WatchStore(ControlStore):
    def put(self, repo, number, meta, *, only_existing=False, only_new=False):
        key = str(number)
        def change(value):
            entries = value.setdefault(repo, {})
            if not isinstance(entries, dict):
                raise RuntimeError('Review watch state has an invalid structure.')
            if only_existing and key not in entries:
                return False
            if only_new and key in entries:
                return False
            entries.setdefault(key, {}).update(meta)
            return True
        return self.change(change)

    def remove(self, repo, number):
        def change(value):
            entries = value.get(repo, {})
            entries.pop(str(number), None)
            if str(number) == '*':
                for key, meta in list(entries.items()):
                    if meta.get('scope') == 'all':
                        entries.pop(key)
            if not entries:
                value.pop(repo, None)
        self.change(change)


class AuthorityStore(ControlStore):
    """Who may post in whose name, per repository.

    Record shape: {"<owner>/<name>": {"post_authority": "ask"|"auto",
    "updated_at": iso, "updated_by": "gui"|"cli"}}. Missing means "ask".
    High-stakes actions never consult this store: they always ask.
    """

    MODES = {'ask', 'auto'}

    def mode(self, repo):
        entry = self.read().get(repo) or {}
        mode = entry.get('post_authority', 'ask')
        return mode if mode in self.MODES else 'ask'

    def set_mode(self, repo, mode, *, updated_by='gui'):
        if mode not in self.MODES:
            raise ValueError('Choose Ask me every time or Post directly for this repository.')
        def change(value):
            value[repo] = {'post_authority': mode,
                           'updated_at': stamp(), 'updated_by': updated_by}
            return value[repo]
        return self.change(change)

    def grants(self):
        """Every non-default grant, newest first, for the safety fence."""
        items = []
        for repo, entry in self.read().items():
            if isinstance(entry, dict) and entry.get('post_authority') == 'auto':
                items.append({'repo': repo, **entry})
        return sorted(items, key=lambda e: e.get('updated_at', ''), reverse=True)


def backup(path: Path):
    if path.exists():
        target = path.with_name(f'{path.name}.bak-gui-{stamp()}')
        shutil.copy2(path, target)
        return target
    return None
