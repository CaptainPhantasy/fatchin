"""OpenAI-compatible chat client for the gha-worker agent.

Stdlib only. Talks to the company secret-broker's credential-injecting proxy
(OpenAI-compatible POST {base_url}/chat/completions) with a broker-lease
bearer token supplied by the caller.

Never logs or echoes token material. No fallback provider: on auth or
validation errors it raises ModelError immediately.
"""
from __future__ import annotations

import datetime
import contextlib
import email.utils
import http.client
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from ghagent.interfaces import ModelConfig
from ghagent.control_store import ControlStore


class ModelError(Exception):
    """Unrecoverable model-client failure (auth, validation, exhausted retries)."""


class SpendLimitExceeded(ModelError):
    """A spend ceiling (per job or per UTC day) would be exceeded by this call."""


_MAX_ATTEMPTS = 3  # total attempts for retryable failures (429 / 5xx / network)
_BODY_EXCERPT_CHARS = 500


class ModelClient:
    """Thin, spend-bounded client for one OpenAI-compatible endpoint."""

    def __init__(
        self,
        cfg: ModelConfig,
        bearer_provider: Callable[[], str],
        job_scope: str = "",
        day_counter_path: str | None = None,
        *,
        backoff_base: float = 0.5,
        max_backoff: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        if not cfg.base_url:
            raise ValueError("ModelConfig.base_url must not be empty")
        if not cfg.model_id:
            raise ValueError("ModelConfig.model_id must not be empty")
        self._cfg = cfg
        self._bearer_provider = bearer_provider
        self._job_scope = job_scope
        self._day_counter_path = day_counter_path
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._urlopen = urlopen
        if not 1 <= max_attempts <= _MAX_ATTEMPTS:
            raise ValueError('max_attempts must be between 1 and 3')
        self._max_attempts = max_attempts
        self._calls_used = 0
        self._counter_lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    @property
    def calls_used(self) -> int:
        return self._calls_used

    def begin_job(self, scope: str) -> None:
        """Bind the next sequential task to its own persistent call allowance."""
        with self._counter_lock:
            self._job_scope = scope
            self._calls_used = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One chat completion. Returns the assistant message dict with
        {"content", "tool_calls", "raw_usage"}.

        Retryable (429 / 5xx / network errors): up to _MAX_ATTEMPTS attempts
        with exponential backoff, honoring a Retry-After header when present.
        401/403/400/422 (and any other non-retryable status): ModelError at
        once. Spend ceilings are checked before any request is sent.
        """
        self._reserve_spend()
        payload: dict[str, Any] = {
            "model": self._cfg.model_id,
            "messages": messages,
            "max_tokens": self._cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if getattr(self._cfg, "disable_reasoning", False):
            payload["thinking"] = {"type": "disabled"}
        # Deliberately no "temperature": reasoning models reject it.

        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._send_once(payload)
            except _RetryableError as exc:
                last_error = exc
                if attempt + 1 >= self._max_attempts:
                    break
                self._sleep(self._retry_delay(attempt, exc.retry_after))
        error = ModelError(f"model request failed after {self._max_attempts} attempts: {last_error}")
        # Keep retry metadata for the continuous fleet's provider cooldown.
        # The ordinary CLI still receives the same ModelError contract.
        error.retry_after = getattr(last_error, 'retry_after', None)
        status = re.search(r'\bHTTP\s+(\d{3})\b', str(last_error))
        error.http_status = int(status[1]) if status else None
        raise error

    # -- spend ceilings ------------------------------------------------------

    def _reserve_spend(self) -> None:
        """Check both ceilings and count this call before anything is sent.

        A blocked call costs nothing: the counters move only when the request
        is actually going out.
        """
        file_lock = ControlStore(self._day_counter_path).lock() if self._day_counter_path else contextlib.nullcontext()
        with self._counter_lock, file_lock:
            record = self._read_day_record()
            job_counts = record.get('jobs', {})
            if self._job_scope:
                self._calls_used = max(self._calls_used, job_counts.get(self._job_scope, 0))
            if self._calls_used + 1 > self._cfg.max_calls_per_job:
                raise SpendLimitExceeded(
                    f"job spend ceiling reached: {self._calls_used} calls used, "
                    f"max_calls_per_job={self._cfg.max_calls_per_job}"
                )
            today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
            day_count = record.get('count', 0) if record.get('date') == today else 0
            if day_count + 1 > self._cfg.max_calls_per_day:
                raise SpendLimitExceeded(
                    f"daily spend ceiling reached: {day_count} calls today "
                    f"(UTC), max_calls_per_day={self._cfg.max_calls_per_day}"
                )
            self._calls_used += 1
            if self._job_scope:
                job_counts[self._job_scope] = self._calls_used
            self._write_day_count(day_count + 1, job_counts)

    def _read_day_record(self) -> dict:
        path = self._day_counter_path
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Preserve the read error instead of inventing empty state: a
            # corrupt counter must never silently uncap daily spend.
            raise ModelError(f"day counter at {path} is unreadable: {exc}") from exc
        if not isinstance(record, dict) or type(record.get("count")) is not int or record['count'] < 0:
            raise ModelError(f"day counter at {path} has an unexpected shape")
        jobs = record.get('jobs', {})
        if not isinstance(jobs, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in jobs.items()):
            raise ModelError(f'day counter at {path} has invalid job counts')
        try:
            datetime.date.fromisoformat(record['date'])
        except (KeyError, TypeError, ValueError):
            raise ModelError(f'day counter at {path} has an invalid date') from None
        return record

    def _write_day_count(self, count: int, job_counts=None) -> None:
        path = self._day_counter_path
        if not path:
            return
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        record = {
            "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
            "count": count,
            "job_scope": self._job_scope,
            "jobs": job_counts or {},
        }
        fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".model-day-counter-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- HTTP ----------------------------------------------------------------

    def _send_once(self, payload: dict[str, Any]) -> dict:
        body = json.dumps(payload).encode("utf-8")
        token = self._bearer_provider()
        if not token:
            raise ModelError("bearer provider returned an empty token")
        req = urllib.request.Request(
            self._cfg.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=self._cfg.request_timeout_seconds) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                excerpt = _safe_body_excerpt(exc, token)
                retry_after = _parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
            finally:
                exc.close()
            if exc.code == 429 or exc.code >= 500:
                raise _RetryableError(
                    f"HTTP {exc.code}: {excerpt}", retry_after=retry_after
                ) from None
            raise ModelError(f"HTTP {exc.code}: {excerpt}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise _RetryableError(f"network error: {str(exc).replace(token, '[redacted]')}") from None

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ModelError(f"model returned invalid JSON: {exc}") from exc
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError('message must contain named fields')
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"model response missing choices[0].message: {excerpt_of(data, token)}") from None
        result = {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
            "raw_usage": data.get("usage"),
        }
        # DeepSeek thinking-mode tool calls require this opaque field on the
        # next request. It stays inside that task's conversation, never in UI
        # narration, reports, or the next worker's context.
        if isinstance(message.get('reasoning_content'), str):
            result['reasoning_content'] = message['reasoning_content']
        return result

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._max_backoff)
        return min(self._backoff_base * (2 ** attempt), self._max_backoff)


class _RetryableError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _safe_body_excerpt(exc: urllib.error.HTTPError, token='') -> str:
    try:
        raw = exc.read(_BODY_EXCERPT_CHARS + len(token))
    except Exception:
        return "<unreadable body>"
    text = raw.decode('utf-8', errors='replace')
    return (text.replace(token, '[redacted]') if token else text)[:_BODY_EXCERPT_CHARS]


def excerpt_of(data: Any, token='') -> str:
    text = json.dumps(data, default=str)
    if token:
        text = text.replace(token, '[redacted]')
    return text[:_BODY_EXCERPT_CHARS]


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
