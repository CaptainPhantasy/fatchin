"""Process locks shared by legacy jobs, manual draft posts and fleet tasks.

Fleet tasks share the repository lock and exclusively own their destination.
Legacy tasks exclusively own the repository while executing or posting.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
from pathlib import Path


class FleetBusy(BlockingIOError):
    """Another task currently holds the required repository or branch."""


@contextlib.contextmanager
def _lock(state_dir, name, *, exclusive, blocking):
    folder = Path(state_dir) / 'fleet-v1' / 'locks'
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()
    fd = os.open(folder / (digest + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flag |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flag)
        except BlockingIOError:
            raise FleetBusy('Another agent is working on this repository or branch.') from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def repo_lock(state_dir, repo, *, exclusive=False, blocking=True):
    return _lock(state_dir, 'repo:' + str(repo).casefold(), exclusive=exclusive, blocking=blocking)


def target_lock(state_dir, repo, target, *, blocking=True):
    return _lock(state_dir, 'target:' + str(repo).casefold() + ':' + str(target),
                 exclusive=True, blocking=blocking)
