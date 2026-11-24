"""Runtime telemetry for audit acceptance (item 15).

Tracks stage durations, API calls/s, cycle completion, event-loop lag proxies.
No secrets. Persist snapshots on Store for Desk/Admin.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any


STORE_KEY = 'telemetry_v1'


class Telemetry:
    def __init__(self, *, maxlen: int = 500):
        self._stage_ms: dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self._api_calls: deque = deque(maxlen=maxlen)
        self._cycle_complete = 0
        self._cycle_incomplete = 0
        self._loop_lag_ms: deque = deque(maxlen=maxlen)
        self._started = time.monotonic()
        self._last_tick = self._started

    def note_stage(self, stage: str, duration_ms: float | None) -> None:
        if duration_ms is None:
            return
        try:
            self._stage_ms[str(stage)].append(float(duration_ms))
        except (TypeError, ValueError):
            pass

    def note_api_call(self) -> None:
        self._api_calls.append(time.monotonic())

    def note_cycle(self, *, complete: bool) -> None:
        if complete:
            self._cycle_complete += 1
        else:
            self._cycle_incomplete += 1

    def note_loop_tick(self) -> None:
        now = time.monotonic()
        lag = max(0.0, (now - self._last_tick) * 1000.0 - 50.0)  # vs 50ms target
        self._loop_lag_ms.append(lag)
        self._last_tick = now

    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return round(s[i], 2)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        api_1s = sum(1 for t in self._api_calls if now - t <= 1.0)
        api_60s = sum(1 for t in self._api_calls if now - t <= 60.0)
        stages = {}
        for name, q in self._stage_ms.items():
            vals = list(q)
            stages[name] = {
                'n': len(vals),
                'p50_ms': self._pct(vals, 0.50),
                'p95_ms': self._pct(vals, 0.95),
                'last_ms': round(vals[-1], 2) if vals else None,
            }
        lag = list(self._loop_lag_ms)
        total = self._cycle_complete + self._cycle_incomplete
        return {
            'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'uptime_s': round(now - self._started, 1),
            'api_calls_per_s': api_1s,
            'api_calls_60s': api_60s,
            'stages': stages,
            'cycle_complete': self._cycle_complete,
            'cycle_incomplete': self._cycle_incomplete,
            'cycle_completion_rate': (
                round(self._cycle_complete / total, 4) if total else None),
            'loop_lag_ms': {
                'p50': self._pct(lag, 0.50),
                'p95': self._pct(lag, 0.95),
            },
        }

    def persist(self, store) -> dict[str, Any]:
        snap = self.snapshot()
        try:
            store.set(STORE_KEY, snap)
        except Exception:
            pass
        return snap


_GLOBAL: Telemetry | None = None


def get_telemetry() -> Telemetry:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = Telemetry()
    return _GLOBAL
