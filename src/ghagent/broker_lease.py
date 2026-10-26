"""Broker lease manager for the gha-worker agent.

Mints short-lived credential leases from the company secret broker's admin
API, keeps the lease token in memory only, re-mints before expiry, and
revokes stale leases. If renewal ultimately fails there is no fallback of
any kind: bearer() raises BrokerLeaseError.

Admin and lease tokens are never logged or echoed.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

_MINT_ATTEMPTS = 3  # bounded retries for transient (5xx/network) mint failures


class BrokerLeaseError(Exception):
    """No usable lease token is available; no fallback source exists."""


class _BrokerRetryable(Exception):
    """Transient broker failure (5xx, network, malformed response)."""


class BrokerLeaseManager:
    """Holds exactly one live lease from the secret broker.

    start() mints a lease and starts a daemon thread that re-mints when only
    a third of the TTL remains, swapping the token under a lock and revoking
    the previous leaseId. stop()/close() revokes the current lease with
    reason "worker-shutdown" and joins the thread; it is idempotent and
    atexit-safe (never raises).
    """

    def __init__(
        self,
        broker_base_url: str,
        consumer_id: str,
        server_id: str,
        ttl_seconds: int,
        admin_token_provider: Callable[[], str],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not broker_base_url:
            raise ValueError("broker_base_url must not be empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._base_url = broker_base_url.rstrip("/")
        self._consumer_id = consumer_id
        self._server_id = server_id
        self._ttl = ttl_seconds
        self._admin_token_provider = admin_token_provider
        self._clock = clock
        self._sleep = sleep
        self._urlopen = urlopen

        self._lock = threading.Lock()
        self._token: str | None = None
        self._lease_id: str | None = None
        self._expires_at: str = ""
        self._renew_error: str = ""
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._renew_at: float = 0.0
        # Test hook: set after each successful renew/swap so tests can wait
        # on a real event instead of sleeping.
        self._renewed_event = threading.Event()

    # -- public API ----------------------------------------------------------

    def start(self) -> str:
        """Mint the first lease and start the renewal thread. Returns the token."""
        token = self.mint_and_get()
        self._thread = threading.Thread(
            target=self._renew_loop, name="broker-lease-renew", daemon=True
        )
        self._thread.start()
        return token

    def mint_and_get(self) -> str:
        """Mint a fresh lease synchronously and make it current. Returns the token."""
        lease = self._mint_with_retries()
        with self._lock:
            self._token = lease["token"]
            self._lease_id = lease["leaseId"]
            self._expires_at = lease.get("expiresAt", "")
            self._renew_at = self._clock() + self._ttl * 2.0 / 3.0
        return lease["token"]

    def bearer(self) -> str:
        """The current lease token. Raises BrokerLeaseError when none is usable."""
        with self._lock:
            if self._renew_error:
                raise BrokerLeaseError(
                    f"lease renewal failed, no fallback source exists: {self._renew_error}"
                )
            if self._stopped or not self._token:
                raise BrokerLeaseError("no live lease: manager is stopped or not started")
            return self._token

    def stop(self, reason: str = "worker-shutdown") -> None:
        """Revoke the current lease and stop the renewal thread. Never raises."""
        with self._lock:
            if self._stopped:
                already_stopped = True
            else:
                already_stopped = False
                self._stopped = True
            lease_id = self._lease_id
            token = self._token
            self._token = None
            self._lease_id = None
        if not already_stopped:
            if lease_id and token:
                try:
                    self._revoke(lease_id, reason)
                except Exception:
                    pass  # atexit-safe: broker unreachable must not crash shutdown
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    # atexit-safe alias
    close = stop

    @property
    def expires_at(self) -> str:
        with self._lock:
            return self._expires_at

    # -- renewal thread ------------------------------------------------------

    def _renew_loop(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    return
                deadline = self._renew_at
            now = self._clock()
            if now < deadline:
                self._sleep(min(deadline - now, 0.5))
                continue
            try:
                self._renew_once()
                self._renewed_event.set()
            except Exception as exc:
                with self._lock:
                    if not self._token:
                        pass
                    self._renew_error = str(exc)
                return  # bearer() now raises; no fallback source exists

    def _renew_once(self) -> None:
        lease = self._mint_with_retries()
        with self._lock:
            old_lease_id = self._lease_id
            old_token = self._token
            self._token = lease["token"]
            self._lease_id = lease["leaseId"]
            self._expires_at = lease.get("expiresAt", "")
            self._renew_at = self._clock() + self._ttl * 2.0 / 3.0
        if old_lease_id and old_token:
            try:
                self._revoke(old_lease_id, "lease-rotation")
            except Exception:
                pass  # the old lease expires on its own; never block the swap

    def _mint_with_retries(self) -> dict:
        last_error: Exception | None = None
        for attempt in range(_MINT_ATTEMPTS):
            try:
                return self._mint()
            except _BrokerRetryable as exc:
                last_error = exc
                if attempt + 1 >= _MINT_ATTEMPTS:
                    break
                self._sleep(0.2 * (2 ** attempt))
            except BrokerLeaseError:
                raise
        raise BrokerLeaseError(
            f"lease mint failed after {_MINT_ATTEMPTS} attempts: {last_error}"
        )

    def _mint(self) -> dict:
        body = json.dumps(
            {
                "consumerId": self._consumer_id,
                "serverId": self._server_id,
                "ttlSeconds": self._ttl,
            }
        ).encode("utf-8")
        admin_token = self._admin_token_provider()
        if not admin_token:
            raise BrokerLeaseError("admin token provider returned an empty token")
        req = urllib.request.Request(
            self._base_url + "/v1/leases",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_token}",
            },
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                excerpt = exc.read(500).decode("utf-8", errors="replace")
            finally:
                exc.close()
            if exc.code == 429 or exc.code >= 500:
                raise _BrokerRetryable(f"HTTP {exc.code}: {excerpt}") from exc
            raise BrokerLeaseError(f"lease mint rejected: HTTP {exc.code}: {excerpt}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise _BrokerRetryable(f"network error: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _BrokerRetryable(f"broker returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict) or not data.get("leaseId") or not data.get("token"):
            raise _BrokerRetryable("broker lease response missing leaseId/token")
        return data

    def _revoke(self, lease_id: str, reason: str) -> None:
        body = json.dumps({"leaseId": lease_id, "reason": reason}).encode("utf-8")
        admin_token = self._admin_token_provider()
        req = urllib.request.Request(
            self._base_url + "/v1/revoke",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_token}",
            },
            method="POST",
        )
        with self._urlopen(req, timeout=30) as resp:
            resp.read()
