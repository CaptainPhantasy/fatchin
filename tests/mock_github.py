"""Reusable fake gh runner for gha-worker tests (stdlib only).

Designed to be imported by other components' tests (executor, ops):

    from tests.mock_github import FakeGh, make_client

    client = make_client()                      # GitHubClient wired to a FakeGh
    fake = client.runner                        # the FakeGh instance
    fake.expect_json({"login": "octocat", "id": 123})
    data = client.rest("GET", "/user")

FakeGh is a callable obeying GitHubClient's runner contract:
``fake(argv, input=None) -> subprocess.CompletedProcess``. ``argv`` is the gh
argument list WITHOUT the binary name; ``input`` is gh's stdin (JSON bodies).
Responses are consumed FIFO from the queue; set ``fake.handler`` to a callable
``(argv, input) -> (returncode, stdout, stderr)`` for dynamic routing (e.g.
answer by API path). Every call is recorded in ``fake.calls`` as a GhCall with
.argv and .input; an empty queue raises AssertionError naming the argv, so a
test never silently talks to a stub response.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ghagent.github_client import GitHubClient

Handler = Callable[..., tuple[int, str, str]]


@dataclass
class GhCall:
    argv: list[str]
    input: str | None = None


@dataclass
class _QueuedResponse:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class FakeGh:
    """Records every invocation; returns queued CompletedProcess-shaped
    responses. Never touches the network, never requires gh on disk."""

    def __init__(self) -> None:
        self.calls: list[GhCall] = []
        self._responses: list[_QueuedResponse] = []
        self.handler: Handler | None = None

    # -- queueing canned responses -------------------------------------------

    def expect(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._responses.append(
            _QueuedResponse(stdout=stdout, stderr=stderr, returncode=returncode))

    def expect_json(self, obj: Any, returncode: int = 0) -> None:
        self.expect(stdout=json.dumps(obj), returncode=returncode)

    def expect_error(self, stderr: str, returncode: int = 1) -> None:
        self.expect(stderr=stderr, returncode=returncode)

    # -- the runner protocol ---------------------------------------------------

    def __call__(self, argv: list[str], input: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(GhCall(argv=list(argv), input=input))
        if self.handler is not None:
            returncode, stdout, stderr = self.handler(list(argv), input)
            return subprocess.CompletedProcess(
                args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr)
        if not self._responses:
            raise AssertionError(f"FakeGh: no queued response for argv={list(argv)!r}")
        response = self._responses.pop(0)
        return subprocess.CompletedProcess(
            args=list(argv), returncode=response.returncode,
            stdout=response.stdout, stderr=response.stderr)

    # -- inspection helpers ------------------------------------------------------

    @property
    def last_call(self) -> GhCall | None:
        return self.calls[-1] if self.calls else None

    @property
    def argvs(self) -> list[list[str]]:
        return [call.argv for call in self.calls]

    def reset(self) -> None:
        """Forget recorded calls and queued responses."""
        self.calls.clear()
        self._responses.clear()


def make_client(**kwargs: Any) -> GitHubClient:
    """GitHubClient wired to a FakeGh runner. The fake is reachable as
    ``client.runner``. Extra kwargs go to GitHubClient (host, api_version,
    logger)."""
    fake = FakeGh()
    return GitHubClient(runner=fake, **kwargs)
