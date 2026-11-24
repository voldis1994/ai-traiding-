"""Centralized Capital.com REST manager (audit P0-01, P1-16).

One budget per broker identity: shared rate limit, 429 backoff, single-flight
token refresh, request coalescing for GET, metrics without secrets.
Orders (POST/PUT/DELETE) are never blindly retried on ambiguous outcomes.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Callable, Awaitable


# Capital docs: 10 requests / second per user (Help Center 28.08.2026).
DEFAULT_RPS = 8.0  # headroom under hard 10/s
DEFAULT_BURST = 10
SESSION_POST_MIN_INTERVAL = 1.05  # POST /session ≤ 1/s per API key


class CapitalRestManager:
    """Wraps an async request callable with identity-scoped controls."""

    def __init__(
        self,
        *,
        identity: str,
        rps: float = DEFAULT_RPS,
        burst: int = DEFAULT_BURST,
        clock: Callable[[], float] | None = None,
    ):
        self.identity = str(identity or 'default')
        self.rps = max(0.5, float(rps))
        self.burst = max(1, int(burst))
        self._clock = clock or time.monotonic
        self._tokens = float(self.burst)
        self._last_refill = self._clock()
        self._lock = asyncio.Lock()
        self._relogin = asyncio.Lock()
        self._token_generation = 0
        self._last_session_post = 0.0
        self._inflight_get: dict[str, asyncio.Future] = {}
        self._recent: deque[float] = deque(maxlen=200)
        self.stats: dict[str, Any] = defaultdict(int)
        self.latency_ms: deque[float] = deque(maxlen=200)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rps)

    async def _acquire_token(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._recent.append(self._clock())
                    self.stats['acquired'] += 1
                    return
                need = (1.0 - self._tokens) / self.rps
            await asyncio.sleep(max(0.01, need))

    def calls_per_second(self, window: float = 1.0) -> float:
        now = self._clock()
        return float(sum(1 for t in self._recent if now - t <= window))

    async def wait_session_slot(self) -> None:
        """Enforce ≤1 POST /session per second for this identity."""
        while True:
            async with self._lock:
                now = self._clock()
                wait = self._last_session_post + SESSION_POST_MIN_INTERVAL - now
                if wait <= 0:
                    self._last_session_post = now
                    return
            await asyncio.sleep(wait)

    async def single_flight_relogin(
        self,
        connect_fn: Callable[[], Awaitable[Any]],
        *,
        observed_generation: int | None = None,
    ) -> int:
        """Refresh tokens once; waiters share the result (P1-16).

        ``connect_fn`` must perform the session POST only (no nested wait/bump).
        This method owns the session rate slot and generation increment.
        """
        async with self._relogin:
            if (
                observed_generation is not None
                and self._token_generation > int(observed_generation)
            ):
                self.stats['relogin_shared'] += 1
                return self._token_generation
            await self.wait_session_slot()
            t0 = self._clock()
            await connect_fn()
            self._token_generation += 1
            self.stats['relogin'] += 1
            self.latency_ms.append((self._clock() - t0) * 1000.0)
            return self._token_generation
    @property
    def token_generation(self) -> int:
        return self._token_generation

    def note_status(self, status: int) -> None:
        self.stats[f'http_{int(status)}'] += 1
        if int(status) == 429:
            self.stats['http_429'] += 1
        if int(status) == 401:
            self.stats['http_401'] += 1

    async def request(
        self,
        method: str,
        path: str,
        *,
        do_request: Callable[[], Awaitable[Any]],
        coalesce_key: str | None = None,
        allow_retry_get: bool = True,
        max_429_retries: int = 4,
    ) -> Any:
        """Run one Capital call under the shared budget.

        GET may coalesce identical in-flight keys and retry on 429 with
        exponential backoff. Non-GET never auto-retries after send ambiguity.
        """
        method_u = str(method or 'GET').upper()
        key = None
        if method_u == 'GET' and coalesce_key:
            key = f'{method_u}:{coalesce_key}'
            existing = self._inflight_get.get(key)
            if existing is not None and not existing.done():
                self.stats['coalesced'] += 1
                return await existing
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._inflight_get[key] = fut

        async def _run():
            delay = 0.25
            attempt = 0
            while True:
                await self._acquire_token()
                t0 = self._clock()
                try:
                    response = await do_request()
                except Exception:
                    self.stats['errors'] += 1
                    raise
                finally:
                    self.latency_ms.append((self._clock() - t0) * 1000.0)
                status = getattr(response, 'status_code', None)
                if status is not None:
                    self.note_status(int(status))
                if status == 429 and method_u == 'GET' and allow_retry_get:
                    attempt += 1
                    if attempt > max_429_retries:
                        return response
                    self.stats['retry_429'] += 1
                    await asyncio.sleep(delay)
                    delay = min(8.0, delay * 2.0)
                    continue
                return response

        try:
            result = await _run()
            if key is not None:
                fut = self._inflight_get.get(key)
                if fut is not None and not fut.done():
                    fut.set_result(result)
            return result
        except Exception as exc:
            if key is not None:
                fut = self._inflight_get.get(key)
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
            raise
        finally:
            if key is not None:
                self._inflight_get.pop(key, None)

    def snapshot_metrics(self) -> dict[str, Any]:
        lat = list(self.latency_ms)
        lat_sorted = sorted(lat)
        def pct(p: float) -> float | None:
            if not lat_sorted:
                return None
            i = min(len(lat_sorted) - 1, max(0, int(round(p * (len(lat_sorted) - 1)))))
            return round(lat_sorted[i], 2)
        return {
            'identity': self.identity,
            'calls_per_s': round(self.calls_per_second(), 3),
            'token_generation': self._token_generation,
            'stats': dict(self.stats),
            'latency_ms': {'p50': pct(0.50), 'p95': pct(0.95), 'p99': pct(0.99)},
        }


_MANAGERS: dict[str, CapitalRestManager] = {}
_MANAGERS_LOCK = asyncio.Lock()


def manager_for(identity: str, **kwargs) -> CapitalRestManager:
    key = str(identity or 'default')
    m = _MANAGERS.get(key)
    if m is None:
        m = CapitalRestManager(identity=key, **kwargs)
        _MANAGERS[key] = m
    return m
