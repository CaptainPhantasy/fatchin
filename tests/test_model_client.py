"""Tests for ghagent.model_client — retry, Retry-After, spend ceilings.

All HTTP goes to a local http.server on an ephemeral 127.0.0.1 port; no real
network is touched. Sleeping is injected, so Retry-After honoring is asserted
without waiting.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ghagent.interfaces import ModelConfig
from ghagent.model_client import ModelClient, ModelError, SpendLimitExceeded


def _openai_ok(content="hello", tool_calls=None):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


class _ScriptedHTTPServer:
    """Turns a responder fn(record, request_number) into a local POST server."""

    def __init__(self, responder):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                record = {
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "raw": raw,
                }
                outer.requests.append(record)
                status, headers, payload = responder(record, len(outer.requests))
                if isinstance(payload, (dict, list)):
                    body = json.dumps(payload).encode("utf-8")
                elif payload is None:
                    body = b""
                else:
                    body = payload
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _SleepRecorder:
    def __init__(self):
        self.sleeps: list[float] = []

    def __call__(self, seconds: float):
        self.sleeps.append(seconds)


class ModelClientTest(unittest.TestCase):
    def setUp(self):
        self._servers: list[_ScriptedHTTPServer] = []

    def tearDown(self):
        for server in self._servers:
            server.close()

    def _serve(self, responder) -> _ScriptedHTTPServer:
        server = _ScriptedHTTPServer(responder)
        self._servers.append(server)
        return server

    def _client(self, base_url, *, max_calls_per_job=10, max_calls_per_day=10,
                day_counter_path=None, backoff_base=0.01) -> tuple[ModelClient, _SleepRecorder]:
        cfg = ModelConfig(
            base_url=base_url,
            model_id="your-model",
            max_calls_per_job=max_calls_per_job,
            max_calls_per_day=max_calls_per_day,
            request_timeout_seconds=10,
            max_tokens=1234,
        )
        sleeper = _SleepRecorder()
        client = ModelClient(
            cfg,
            bearer_provider=lambda: "lease-token",
            job_scope="job-test",
            day_counter_path=day_counter_path,
            backoff_base=backoff_base,
            sleep=sleeper,
        )
        return client, sleeper

    # -- happy path and payload shape ---------------------------------------

    def test_parses_openai_response_and_records_usage(self):
        tool_calls = [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"},
        }]
        server = self._serve(lambda rec, n: (200, {}, _openai_ok(content=None, tool_calls=tool_calls)))
        client, _ = self._client(server.url)
        result = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertIsNone(result["content"])
        self.assertEqual(result["tool_calls"], tool_calls)
        self.assertEqual(result["raw_usage"]["completion_tokens_details"]["reasoning_tokens"], 3)
        self.assertEqual(client.calls_used, 1)

    def test_payload_has_no_temperature_and_bearer_header(self):
        seen = {}

        def responder(record, n):
            seen.update(record)
            return (200, {}, _openai_ok())

        server = self._serve(responder)
        client, _ = self._client(server.url)
        client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "x": 1}])
        payload = json.loads(seen["raw"])
        self.assertEqual(seen["path"], "/chat/completions")
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer lease-token")
        self.assertEqual(payload["model"], "your-model")
        self.assertEqual(payload["max_tokens"], 1234)
        self.assertEqual(payload["tools"], [{"type": "function", "x": 1}])
        self.assertNotIn("temperature", payload)

    # -- retry policy ---------------------------------------------------------

    def test_explicit_single_attempt_probe_never_retries_retryable_error(self):
        server = self._serve(lambda rec, n: (503, {}, {'error': 'temporarily unavailable'}))
        sleeper = _SleepRecorder()
        client = ModelClient(ModelConfig(base_url=server.url), lambda: 'test-token',
                             max_attempts=1, sleep=sleeper)
        with self.assertRaises(ModelError):
            client.chat([{'role': 'user', 'content': 'Connection check'}])
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(sleeper.sleeps, [])

    def test_retry_on_429_honors_retry_after_header(self):
        def responder(record, n):
            if n == 1:
                return (429, {"Retry-After": "1.5"}, {"error": {"message": "slow down"}})
            return (200, {}, _openai_ok(content="second try"))

        server = self._serve(responder)
        client, sleeper = self._client(server.url)
        result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "second try")
        self.assertEqual(client.calls_used, 1)
        self.assertEqual(len(server.requests), 2)
        # Retry-After: 1.5 must be honored exactly, not the backoff curve.
        self.assertEqual(sleeper.sleeps, [1.5])

    def test_no_retry_on_401(self):
        server = self._serve(lambda rec, n: (401, {}, {"error": {"message": "bad token"}}))
        client, sleeper = self._client(server.url)
        with self.assertRaises(ModelError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(sleeper.sleeps, [])
        # Spend is reserved when the request goes out; the failed 401 was sent.
        self.assertEqual(client.calls_used, 1)

    def test_no_retry_on_400_and_422(self):
        for status in (400, 403, 422):
            with self.subTest(status=status):
                server = self._serve(lambda rec, n: (status, {}, {"error": "no"}))
                client, sleeper = self._client(server.url)
                with self.assertRaises(ModelError):
                    client.chat([{"role": "user", "content": "hi"}])
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(sleeper.sleeps, [])

    def test_5xx_retried_three_attempts_then_model_error(self):
        server = self._serve(lambda rec, n: (500, {}, {"error": "boom"}))
        client, sleeper = self._client(server.url, backoff_base=0.01)
        with self.assertRaises(ModelError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("3 attempts", str(ctx.exception))
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(sleeper.sleeps, [0.01, 0.02])

    def test_network_error_retried_then_raises(self):
        # Port 1 on loopback: connection refused immediately, no real network.
        client, sleeper = self._client("http://127.0.0.1:1/nope", backoff_base=0.01)
        with self.assertRaises(ModelError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("3 attempts", str(ctx.exception))
        self.assertEqual(sleeper.sleeps, [0.01, 0.02])

    # -- spend ceilings ---------------------------------------------------------

    def test_job_spend_cap_blocks_before_sending(self):
        server = self._serve(lambda rec, n: (200, {}, _openai_ok()))
        client, _ = self._client(server.url, max_calls_per_job=2)
        client.chat([{"role": "user", "content": "1"}])
        client.chat([{"role": "user", "content": "2"}])
        with self.assertRaises(SpendLimitExceeded) as ctx:
            client.chat([{"role": "user", "content": "3"}])
        self.assertIn("max_calls_per_job", str(ctx.exception))
        # A blocked call costs nothing: still exactly two requests on the wire.
        self.assertEqual(len(server.requests), 2)

    def test_day_counter_persists_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "day-counter.json")
            server = self._serve(lambda rec, n: (200, {}, _openai_ok()))
            client, _ = self._client(server.url, max_calls_per_day=2, day_counter_path=path)

            client.chat([{"role": "user", "content": "1"}])
            client.chat([{"role": "user", "content": "2"}])
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
            today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            self.assertEqual(record["date"], today)
            self.assertEqual(record["count"], 2)
            self.assertEqual(record["job_scope"], "job-test")

            with self.assertRaises(SpendLimitExceeded) as ctx:
                client.chat([{"role": "user", "content": "3"}])
            self.assertIn("max_calls_per_day", str(ctx.exception))
            self.assertEqual(len(server.requests), 2)

    def test_day_counter_rolls_over_on_stale_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "day-counter.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"date": "1999-12-31", "count": 999}, fh)
            server = self._serve(lambda rec, n: (200, {}, _openai_ok()))
            client, _ = self._client(server.url, max_calls_per_day=1, day_counter_path=path)
            client.chat([{"role": "user", "content": "fresh day"}])
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
            self.assertEqual(record["count"], 1)

    def test_corrupt_day_counter_raises_instead_of_uncapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "day-counter.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            server = self._serve(lambda rec, n: (200, {}, _openai_ok()))
            client, _ = self._client(server.url, day_counter_path=path)
            with self.assertRaises(ModelError) as ctx:
                client.chat([{"role": "user", "content": "hi"}])
            self.assertIn("unreadable", str(ctx.exception))
            self.assertEqual(len(server.requests), 0)

    # -- construction guards ------------------------------------------------------

    def test_refuses_empty_base_url_or_model_id(self):
        with self.assertRaises(ValueError):
            ModelClient(ModelConfig(base_url=""), bearer_provider=lambda: "t")
        with self.assertRaises(ValueError):
            ModelClient(ModelConfig(base_url="http://x", model_id=""), bearer_provider=lambda: "t")


if __name__ == "__main__":
    unittest.main()
