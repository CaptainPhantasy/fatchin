"""Test package marker.

Required by `python -m unittest discover -s tests -t .`: Python 3.11+
discovery refuses a start directory that is not an importable package. Also
makes `from tests.mock_github import make_client` work for other components'
tests.
"""
from __future__ import annotations

import functools
import shutil
import subprocess


@functools.lru_cache(maxsize=1)
def sandbox_available() -> bool:
    """True only when bubblewrap is installed *and* can create a sandbox.

    Some CI runners ship bwrap but block unprivileged user namespaces, so the
    binary exists and every invocation fails. Tests that exercise isolated
    execution must skip in that case rather than pretending the sandbox works.
    """
    executable = shutil.which('bwrap')
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, '--die-with-parent', '--unshare-all',
             '--ro-bind', '/', '/', '--', '/bin/true'],
            capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
