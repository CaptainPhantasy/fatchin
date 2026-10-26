"""Tests for ghagent.broker_lease — mint/renew-swap/revoke lifecycle.

All broker traffic goes to a local http.server on an ephemeral 127.0.0.1
port. Time is a gated fake clock: the renewal thread advances fake time only
while the gate is open, so tests are deterministic with no wall-clock sleeps.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ghagent.broker_lease import BrokerLeaseError, BrokerLeaseManager


class _GateClock:
    """Controllable clock: fake time advances only while the gate is open."""

    def __init__(self):
        self.t = 0.0
        self._open = threading.Event()
        self._open.set()

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        # Small real ticks keep the thread responsive to stop(); fake time
        # moves only while the gate is open.
        self._open.wait(0.005)
        if self._open.is_set():
            self.t += seconds

    def close_gate(self) -> None:
        self._open.clear()

    def open_gate(self) -> None:
        self._open.set()

    def wait_real(self, seconds: float) -> None:
        self._open.wait(seconds)


class _BrokerFake:
    """Scripted secret broker: counts mints, records revocations."""

    def __init__(self, fail_mints=0):
        self.fail_mints = fail_mints
        self.mint_count = 0
        self.mint_bearers: list[str] = []
        self.revoked: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                auth = self.headers.get("Authorization", "")
                with outer.lock:
                    if self.path == "/v1/leases":
                        outer.mint_count += 1
                        outer.mint_bearers.append(auth)
                        if outer.mint_count <= outer.fail_mints:
                            body = json.dumps({"error": "broker unavailable"}).encode()
                            self._send(500, body)
                            return
                        payload = {
                            "leaseId": f"lease-{outer.mint_count}",
                            "token": f"tok-{outer.mint_count}",
                            "expiresAt": "2026-10-17T12:00:00Z",
                            "envKeys": ["ZAI_API_KEY"],
                            "sources": ["test"],
                        }
                        self._send(201, json.dumps(payload).encode())
                    elif self.path == "/v1/revoke":
                        data = json.loads(raw)
                        outer.revoked.append(data)
                        self._send(200, json.dumps({"leaseId": data.get("leaseId"), "revoked": True}).encode())
                    else:
                        self._send(404, b"{}")

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
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


class BrokerLeaseManagerTest(unittest.TestCase):
    def setUp(self):
        self.clock = _GateClock()

    def _manager(self, broker, ttl=300):
        return BrokerLeaseManager(
            broker.url,
            "fatchin",
            "your-provider",
            ttl,
            lambda: "admin-token",
            clock=self.clock.now,
            sleep=self.clock.sleep,
        )

    def test_start_mints_and_bearer_returns_token(self):
        broker = _BrokerFake()
        self.addCleanup(broker.close)
        mgr = self._manager(broker)
        self.addCleanup(mgr.stop)
        # Freeze fake time first: an open gate lets the renewal thread cross
        # the renewal deadline under load and mint a second lease mid-test.
        self.clock.close_gate()
        token = mgr.start()
        self.assertEqual(token, "tok-1")
        self.assertEqual(mgr.bearer(), "tok-1")
        self.assertEqual(broker.mint_count, 1)
        self.assertEqual(broker.mint_bearers[0], "Bearer admin-token")
        self.assertEqual(mgr.expires_at, "2026-10-17T12:00:00Z")
        mgr.stop()
        self.assertEqual(
            broker.revoked,
            [{"leaseId": "lease-1", "reason": "worker-shutdown"}],
        )
        self.assertFalse(mgr._thread.is_alive())

    def test_renewal_swaps_token_and_revokes_old_lease(self):
        broker = _BrokerFake()
        self.addCleanup(broker.close)
        mgr = self._manager(broker, ttl=300)
        self.addCleanup(mgr.stop)
        mgr.start()
        self.assertEqual(mgr.bearer(), "tok-1")

        # Close the gate first: the renewal thread will notice the advanced
        # clock on its next tick but fake time freezes again right after the
        # swap, so no further renewal can race the assertions below.
        self.clock.close_gate()
        self.clock.t = 201.0
        self.assertTrue(mgr._renewed_event.wait(timeout=5.0))

        self.assertEqual(mgr.bearer(), "tok-2")
        self.assertEqual(broker.mint_count, 2)
        self.assertEqual(broker.revoked, [{"leaseId": "lease-1", "reason": "lease-rotation"}])

        mgr.stop()
        self.assertIn({"leaseId": "lease-2", "reason": "worker-shutdown"}, broker.revoked)

    def test_renewal_uses_renew_once_swap_semantics(self):
        broker = _BrokerFake()
        self.addCleanup(broker.close)
        mgr = self._manager(broker, ttl=300)
        self.addCleanup(mgr.stop)
        # Park the live renewal thread (no fake-time advances) so the direct
        # _renew_once call below is the only renewal in flight.
        self.clock.close_gate()
        mgr.start()
        mgr._renew_once()
        self.assertEqual(mgr.bearer(), "tok-2")
        self.assertEqual(broker.revoked, [{"leaseId": "lease-1", "reason": "lease-rotation"}])
        mgr.stop()

    def test_renewal_failure_bounded_retries_then_bearer_raises(self):
        broker = _BrokerFake()
        self.addCleanup(broker.close)
        mgr = self._manager(broker, ttl=300)
        self.addCleanup(mgr.stop)
        mgr.start()
        broker.fail_mints = 10_000  # every subsequent mint fails with 5xx

        self.clock.close_gate()
        self.clock.t = 201.0
        gave_up = threading.Event()
        for _ in range(500):  # wait until the renewal thread terminated
            if not mgr._thread.is_alive():
                gave_up.set()
                break
            self.clock.wait_real(0.01)
        self.assertTrue(gave_up.is_set(), "renewal thread did not terminate")

        with self.assertRaises(BrokerLeaseError) as ctx:
            mgr.bearer()
        self.assertIn("fallback", str(ctx.exception))
        # 1 initial mint + 3 bounded retry attempts, nothing more.
        self.assertEqual(broker.mint_count, 4)

    def test_mint_retries_transient_5xx_then_succeeds(self):
        broker = _BrokerFake(fail_mints=2)
        self.addCleanup(broker.close)
        mgr = self._manager(broker)
        self.addCleanup(mgr.stop)
        # Exercise startup retries without racing a background renewal. The
        # retry loop still runs; this fake clock need not advance on sleeps.
        self.clock.close_gate()
        token = mgr.start()
        self.assertEqual(token, "tok-3")
        self.assertEqual(broker.mint_count, 3)
        mgr.stop()

    def test_admin_auth_rejection_raises_immediately(self):
        # A non-retryable broker status (here: 404 on a broken path) must fail
        # the mint at once — bounded retries apply only to 5xx/network errors.
        broker = _BrokerFake()
        self.addCleanup(broker.close)

        def deny():
            return "wrong-admin-token"

        mgr = BrokerLeaseManager(
            broker.url + "/extra", "c", "s", 300, deny,
            clock=self.clock.now, sleep=self.clock.sleep,
        )
        with self.assertRaises(BrokerLeaseError):
            mgr.start()

    def test_bearer_before_start_raises(self):
        broker = _BrokerFake()
        self.addCleanup(broker.close)
        mgr = self._manager(broker)
        with self.assertRaises(BrokerLeaseError):
            mgr.bearer()

    def test_stop_is_idempotent_and_atexit_safe(self):
        broker = _BrokerFake()
        self.addCleanup(broker.close)
        mgr = self._manager(broker)
        mgr.start()
        mgr.stop()
        mgr.stop()  # second call must not raise
        mgr.close()  # alias must not raise either
        with self.assertRaises(BrokerLeaseError):
            mgr.bearer()
        self.assertEqual(len([r for r in broker.revoked if r["reason"] == "worker-shutdown"]), 1)

    def test_constructor_guards(self):
        with self.assertRaises(ValueError):
            BrokerLeaseManager("", "c", "s", 300, lambda: "t")
        with self.assertRaises(ValueError):
            BrokerLeaseManager("http://x", "c", "s", 0, lambda: "t")


if __name__ == "__main__":
    unittest.main()
