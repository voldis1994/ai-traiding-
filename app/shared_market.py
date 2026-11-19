"""Same-epic market perception shared across per-client engines.

Public OHLC/MTF must not diverge just because two clients poll Capital a few
seconds apart or hold different local candle counts. Personal brains, day-book
phase, lots, and £ learning stay private per client.
"""
from __future__ import annotations

import threading
import time

_lock = threading.RLock()
# epic -> {analysis, bars_m1, last_at, updated_mono, source}
_mtf: dict[str, dict] = {}


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float('inf') else None


def fingerprint(candles) -> tuple:
    """Identify the completed tape head used for MTF (count + last bar)."""
    rows = list(candles or [])
    if not rows:
        return (0, None, None)
    last = rows[-1] if isinstance(rows[-1], dict) else {}
    return (
        len(rows),
        str(last.get('at') or '') or None,
        _finite(last.get('close')),
    )


def publish_mtf(epic, analysis, *, bars_m1=None, last_at=None, source=None) -> dict | None:
    """Publish a ready MTF snapshot for this epic (keeps the richest tape)."""
    if not epic or not isinstance(analysis, dict) or not analysis.get('ready'):
        return None
    epic = str(epic)
    bars = int(bars_m1 if bars_m1 is not None else (analysis.get('bars_m1') or 0) or 0)
    at = last_at if last_at is not None else None
    if at is None:
        # Prefer explicit last completed minute from caller; else leave blank.
        at = analysis.get('last_at')
    payload = {
        'analysis': dict(analysis),
        'bars_m1': bars,
        'last_at': str(at) if at else None,
        'updated_mono': time.monotonic(),
        'source': str(source or 'engine'),
    }
    with _lock:
        prev = _mtf.get(epic)
        if prev is not None:
            prev_bars = int(prev.get('bars_m1') or 0)
            prev_at = prev.get('last_at')
            # Prefer more bars; if equal bars, prefer same/newer last_at.
            if prev_bars > bars:
                return dict(prev['analysis'], shared=True, shared_kept='richer_peer')
            if prev_bars == bars and prev_at and at and str(prev_at) > str(at):
                return dict(prev['analysis'], shared=True, shared_kept='newer_peer')
        _mtf[epic] = payload
        out = dict(payload['analysis'])
        out['shared'] = True
        out['shared_source'] = payload['source']
        return out


def adopt_mtf(epic, *, local_bars=0, local_last_at=None, max_age_s=90.0) -> dict | None:
    """Return shared ready MTF when it matches/beats this client's tape."""
    if not epic:
        return None
    epic = str(epic)
    now = time.monotonic()
    with _lock:
        prev = _mtf.get(epic)
        if not prev or not isinstance(prev.get('analysis'), dict):
            return None
        if not prev['analysis'].get('ready'):
            return None
        age = now - float(prev.get('updated_mono') or 0)
        if age > float(max_age_s):
            return None
        bars = int(prev.get('bars_m1') or 0)
        at = prev.get('last_at')
        local_n = int(local_bars or 0)
        # Adopt when peer has at least as many bars and not an older head.
        if bars < local_n:
            return None
        if local_last_at and at and str(at) < str(local_last_at):
            return None
        out = dict(prev['analysis'])
        out['shared'] = True
        out['shared_age_s'] = round(age, 2)
        out['shared_source'] = prev.get('source')
        return out


def clear(epic=None):
    """Test helper — clear one epic or all."""
    with _lock:
        if epic is None:
            _mtf.clear()
        else:
            _mtf.pop(str(epic), None)


def mirror_historical_candles(peers, epic, bars) -> int:
    """Copy HISTORICAL M1 bars into peer stores on the same epic.

    ``peers`` is an iterable of engine-like objects with ``store.candle`` and
    optional ``mandate``. Returns how many bar-writes were attempted.
    """
    if not epic or not bars:
        return 0
    epic = str(epic)
    wrote = 0
    for eng in peers or []:
        if eng is None:
            continue
        mandate = getattr(eng, 'mandate', None) or {}
        peer_epic = mandate.get('epic') if isinstance(mandate, dict) else None
        if peer_epic and str(peer_epic) != epic:
            continue
        store = getattr(eng, 'store', None)
        if store is None or not hasattr(store, 'candle'):
            continue
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            try:
                store.candle(epic, bar, source='HISTORICAL')
                wrote += 1
            except Exception:
                continue
    return wrote
