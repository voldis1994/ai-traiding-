"""Same-epic market perception shared across per-client engines.

Public OHLC/MTF must not diverge just because two clients poll Capital a few
seconds apart or hold different local candle counts. Personal brains, day-book
phase, lots, and £ learning stay private per client.
"""
from __future__ import annotations

import math
import threading
import time

_lock = threading.RLock()
# epic -> {analysis, bars_m1, last_at, updated_mono, source}
_mtf: dict[str, dict] = {}
# epic -> {side, source, confidence, bias, event, updated_mono}
# Same-market clients must not invent opposite BUY vs SELL on one tape.
_side: dict[str, dict] = {}


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


def publish_side(epic, side, *, source=None, confidence=None,
                 bias=None, event=None) -> dict | None:
    """Publish the market side for this epic (MTF preferred or structure bias)."""
    if not epic:
        return None
    s = str(side or '').upper()
    if s not in ('BUY', 'SELL'):
        return None
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    if not math.isfinite(conf):
        conf = 0.0
    payload = {
        'side': s,
        'source': str(source or 'structure'),
        'confidence': conf,
        'bias': str(bias).upper() if bias else None,
        'event': str(event) if event else None,
        'updated_mono': time.monotonic(),
    }
    with _lock:
        prev = _side.get(str(epic))
        # Keep a stronger/fresher same-side vote; only flip when new conf is clear.
        if prev is not None:
            prev_side = str(prev.get('side') or '').upper()
            prev_conf = float(prev.get('confidence') or 0)
            age = time.monotonic() - float(prev.get('updated_mono') or 0)
            if prev_side == s:
                if conf < prev_conf and age < 30.0:
                    return dict(prev)
            elif conf < max(0.55, prev_conf) and age < 45.0:
                # Weak opposite vote must not flip the fleet mid-tape.
                return dict(prev)
        _side[str(epic)] = payload
        return dict(payload)


def adopt_side(epic, *, max_age_s=90.0) -> dict | None:
    """Shared BUY/SELL for this epic — Admin + soft entry alignment."""
    if not epic:
        return None
    now = time.monotonic()
    with _lock:
        prev = _side.get(str(epic))
        if not prev:
            return None
        age = now - float(prev.get('updated_mono') or 0)
        if age > float(max_age_s):
            return None
        out = dict(prev)
        out['shared'] = True
        out['shared_age_s'] = round(age, 2)
        return out


def clear(epic=None):
    """Test helper — clear one epic or all."""
    with _lock:
        if epic is None:
            _mtf.clear()
            _side.clear()
        else:
            _mtf.pop(str(epic), None)
            _side.pop(str(epic), None)


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
