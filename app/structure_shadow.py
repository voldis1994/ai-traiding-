"""Shadow learns market *structure* from bad (SL) trade OHLC paths.

Idea (operator): every losing SL trade freezes a snapshot — entry, exit, and
every candle while the position was open. Shadow frames that path as a market
event (RANGE / TREND_UP|DOWN / BREAKOUT_UP|DOWN / CHOP) from OHLC geometry
alone. Later, before LIVE OPEN, shadow matches the *current* candle layout to
those bad frames and returns precise advice (HOLD / avoid that side) with a
confidence rooted in how often that structure killed the same direction.

No TP/SL hardcode. Structure is read from candles; LIVE still decides size and
brackets from OHLC zone. This module only remembers *when* a side was wrong.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'structure_shadow_library_v1'
SNAPSHOT_KEY = 'structure_shadow_snapshots_v1'
LAST_ADVICE_KEY = 'structure_shadow_last_advice_v1'

MAX_LIBRARY = 48          # keyed buckets
MAX_SNAPSHOTS = 120       # raw loss paths kept for audit / re-frame
MAX_BARS_PER_SNAP = 60
MIN_BARS = 3
MIN_ADVICE_SAMPLES = 3
# Soft: only warn/hold when same structure+side has mostly been SL losses.
ADVICE_SL_RATE = 0.67

STRUCTURES = frozenset({
    'TREND_UP', 'TREND_DOWN', 'RANGE',
    'BREAKOUT_UP', 'BREAKOUT_DOWN', 'CHOP',
})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _bar_ohlc(raw) -> dict | None:
    """Normalise candle dict → open/high/low/close/at (accepts mid fallback)."""
    if not isinstance(raw, dict):
        return None
    o = _finite(raw.get('open'))
    h = _finite(raw.get('high'))
    l = _finite(raw.get('low'))
    c = _finite(raw.get('close'))
    if c is None:
        c = _finite(raw.get('mid'))
    if h is None:
        h = c
    if l is None:
        l = c
    if o is None:
        o = c
    if c is None or h is None or l is None or o is None:
        return None
    if min(o, h, l, c) <= 0:
        return None
    hi = max(o, h, l, c)
    lo = min(o, h, l, c)
    return {
        'open': float(o), 'high': float(hi), 'low': float(lo),
        'close': float(c), 'at': raw.get('at'),
    }


def normalise_bars(rows, *, limit=MAX_BARS_PER_SNAP) -> list:
    out = []
    for raw in rows or []:
        b = _bar_ohlc(raw)
        if b is not None:
            out.append(b)
    if limit and len(out) > int(limit):
        out = out[-int(limit):]
    return out


def frame_from_ohlc(bars, *, entry=None, exit_level=None,
                    pre_bars=None) -> dict:
    """Classify market event from OHLC layout — ratios only, no £ / TP / SL rules.

    Uses the in-trade path first. Optional ``pre_bars`` (candles before entry)
    detect breakouts beyond the prior range.
    """
    path = normalise_bars(bars)
    if len(path) < MIN_BARS:
        return {
            'structure': 'CHOP', 'ready': False, 'reason': 'too_few_bars',
            'bars': len(path), 'net': None, 'range': None,
            'hh_hl': 0, 'lh_ll': 0, 'breakout': False,
        }
    highs = [b['high'] for b in path]
    lows = [b['low'] for b in path]
    closes = [b['close'] for b in path]
    path_hi = max(highs)
    path_lo = min(lows)
    span = path_hi - path_lo
    if span <= 1e-12:
        return {
            'structure': 'RANGE', 'ready': True, 'reason': 'flat_path',
            'bars': len(path), 'net': 0.0, 'range': 0.0,
            'hh_hl': 0, 'lh_ll': 0, 'breakout': False,
        }
    net = closes[-1] - closes[0]
    net_r = net / span
    # Swing structure: successive close pivots vs prior extremes.
    hh_hl = lh_ll = 0
    for i in range(1, len(path)):
        if highs[i] >= highs[i - 1] and lows[i] >= lows[i - 1]:
            hh_hl += 1
        elif highs[i] <= highs[i - 1] and lows[i] <= lows[i - 1]:
            lh_ll += 1
    # Prior range (pre-entry) → breakout if exit pierces it.
    pre = normalise_bars(pre_bars or [], limit=40)
    breakout = False
    breakout_side = None
    exit_px = _finite(exit_level) or closes[-1]
    if len(pre) >= MIN_BARS:
        pre_hi = max(b['high'] for b in pre)
        pre_lo = min(b['low'] for b in pre)
        pre_span = pre_hi - pre_lo
        if pre_span > 1e-12:
            if exit_px > pre_hi + 0.05 * pre_span:
                breakout, breakout_side = True, 'UP'
            elif exit_px < pre_lo - 0.05 * pre_span:
                breakout, breakout_side = True, 'DOWN'
    if breakout and breakout_side == 'UP':
        structure = 'BREAKOUT_UP'
        reason = 'exit_above_pre_range'
    elif breakout and breakout_side == 'DOWN':
        structure = 'BREAKOUT_DOWN'
        reason = 'exit_below_pre_range'
    elif abs(net_r) < 0.25 and max(hh_hl, lh_ll) < max(2, len(path) // 3):
        structure = 'RANGE'
        reason = 'contained_net'
    elif net_r >= 0.25 and hh_hl >= lh_ll:
        structure = 'TREND_UP'
        reason = 'net_up_hh_hl'
    elif net_r <= -0.25 and lh_ll >= hh_hl:
        structure = 'TREND_DOWN'
        reason = 'net_down_lh_ll'
    elif abs(net_r) >= 0.25:
        structure = 'TREND_UP' if net_r > 0 else 'TREND_DOWN'
        reason = 'net_dominant'
    else:
        structure = 'CHOP'
        reason = 'mixed_swings'
    return {
        'structure': structure,
        'ready': True,
        'reason': reason,
        'bars': len(path),
        'net': float(net),
        'range': float(span),
        'net_over_range': float(net_r),
        'hh_hl': int(hh_hl),
        'lh_ll': int(lh_ll),
        'breakout': bool(breakout),
        'entry': _finite(entry),
        'exit': float(exit_px),
    }


def frame_key(*, epic, structure, direction) -> str:
    ep = str(epic or 'UNKNOWN').strip() or 'UNKNOWN'
    st = str(structure or 'CHOP').upper()
    if st not in STRUCTURES:
        st = 'CHOP'
    side = str(direction or 'HOLD').upper()
    if side not in ('BUY', 'SELL'):
        side = 'HOLD'
    return f'{ep}|{st}|{side}'


def load_library(store) -> dict:
    raw = store.get(STORE_KEY) if store is not None else None
    if not isinstance(raw, dict):
        return {'buckets': {}, 'updated': None}
    buckets = raw.get('buckets')
    if not isinstance(buckets, dict):
        buckets = {}
    clean = {}
    for k, v in buckets.items():
        if isinstance(v, dict):
            clean[str(k)] = dict(v)
    return {'buckets': clean, 'updated': raw.get('updated')}


def save_library(store, library: dict) -> dict:
    buckets = library.get('buckets') if isinstance(library, dict) else {}
    if not isinstance(buckets, dict):
        buckets = {}
    # Cap by weakest evidence (fewest SL samples) when oversized.
    items = sorted(
        buckets.items(),
        key=lambda kv: int((kv[1] or {}).get('sl_count') or 0),
        reverse=True,
    )
    trimmed = {k: dict(v) for k, v in items[:MAX_LIBRARY] if isinstance(v, dict)}
    out = {'buckets': trimmed, 'updated': _now()}
    if store is not None:
        store.set(STORE_KEY, out)
    return out


def _append_snapshot(store, snap: dict):
    hist = list(store.get(SNAPSHOT_KEY) or []) if store is not None else []
    if not isinstance(hist, list):
        hist = []
    hist.append(snap)
    if store is not None:
        store.set(SNAPSHOT_KEY, hist[-MAX_SNAPSHOTS:])


def build_snapshot(*, epic, direction, entry, exit_level, exit_reason,
                   broker_pnl=None, bars=None, pre_bars=None,
                   deal_id=None, spread=None) -> dict:
    """Freeze the bad-trade path + OHLC frame for shadow memory."""
    path = normalise_bars(bars)
    frame = frame_from_ohlc(path, entry=entry, exit_level=exit_level,
                            pre_bars=pre_bars)
    return {
        'at': _now(),
        'deal_id': deal_id,
        'epic': str(epic or ''),
        'direction': direction,
        'entry': _finite(entry),
        'exit': _finite(exit_level),
        'exit_reason': str(exit_reason or 'unknown'),
        'broker_pnl': _finite(broker_pnl),
        'spread': _finite(spread),
        'bars': path,
        'pre_bars': normalise_bars(pre_bars or [], limit=40),
        'frame': frame,
        'key': frame_key(
            epic=epic, structure=frame.get('structure'), direction=direction),
    }


def record_loss(store, *, epic, direction, entry, exit_level, exit_reason,
                broker_pnl=None, bars=None, pre_bars=None, deal_id=None,
                spread=None) -> dict | None:
    """On SL (or cash-losing) close: snapshot path, frame structure, update library.

    Shadow memory grows only from bad outcomes — wins are not required here.
    """
    if direction not in ('BUY', 'SELL'):
        return None
    reason = str(exit_reason or '').lower()
    pnl = _finite(broker_pnl)
    # Prefer explicit SL; also accept any cash loss with a path (vanish/trail).
    is_sl = reason == 'sl'
    is_loss = pnl is not None and pnl < 0
    if not is_sl and not is_loss:
        return None
    snap = build_snapshot(
        epic=epic, direction=direction, entry=entry, exit_level=exit_level,
        exit_reason=exit_reason, broker_pnl=broker_pnl, bars=bars,
        pre_bars=pre_bars, deal_id=deal_id, spread=spread)
    if not snap['frame'].get('ready') and len(snap['bars']) < MIN_BARS:
        return None
    _append_snapshot(store, {
        k: snap[k] for k in (
            'at', 'deal_id', 'epic', 'direction', 'entry', 'exit',
            'exit_reason', 'broker_pnl', 'spread', 'frame', 'key')
    } | {'bar_count': len(snap['bars']),
         # Keep a compact OHLC trail for later re-frame / audit.
         'ohlc': snap['bars'][-MAX_BARS_PER_SNAP:]})
    lib = load_library(store)
    key = snap['key']
    bucket = dict(lib['buckets'].get(key) or {
        'key': key, 'epic': str(epic or ''), 'structure': snap['frame']['structure'],
        'direction': direction, 'sl_count': 0, 'loss_count': 0,
        'total_pnl': 0.0, 'last_at': None, 'last_deal': None,
        'avg_net_over_range': None, 'samples': 0,
    })
    bucket['sl_count'] = int(bucket.get('sl_count') or 0) + (1 if is_sl else 0)
    bucket['loss_count'] = int(bucket.get('loss_count') or 0) + 1
    bucket['samples'] = int(bucket.get('samples') or 0) + 1
    if pnl is not None:
        bucket['total_pnl'] = float(bucket.get('total_pnl') or 0) + float(pnl)
    nor = snap['frame'].get('net_over_range')
    if nor is not None:
        prev = _finite(bucket.get('avg_net_over_range'))
        n = int(bucket['samples'])
        if prev is None:
            bucket['avg_net_over_range'] = float(nor)
        else:
            bucket['avg_net_over_range'] = (prev * (n - 1) + float(nor)) / n
    bucket['last_at'] = snap['at']
    bucket['last_deal'] = deal_id
    bucket['structure'] = snap['frame']['structure']
    lib['buckets'][key] = bucket
    save_library(store, lib)
    return {'snapshot': snap, 'bucket': bucket, 'key': key}


def advise(store, *, epic, proposal, bars=None, pre_bars=None,
           min_samples=MIN_ADVICE_SAMPLES, sl_rate=ADVICE_SL_RATE) -> dict:
    """Match current OHLC layout to bad frames; tell LIVE what not to do.

    Returns action advice for the *proposal* side only — never invents a flip
    OPEN. Strong match → HOLD with structure evidence for the operator / gate.
    """
    path = normalise_bars(bars)
    frame = frame_from_ohlc(path, pre_bars=pre_bars)
    structure = frame.get('structure') or 'CHOP'
    proposal = proposal if proposal in ('BUY', 'SELL') else None
    out = {
        'ready': bool(frame.get('ready')),
        'structure': structure,
        'frame': frame,
        'proposal': proposal,
        'action': None,          # None = no override
        'confidence': 0.0,
        'reason': 'no_structure_match',
        'bucket': None,
        'key': None,
    }
    if proposal is None or not frame.get('ready'):
        out['reason'] = 'no_proposal_or_frame'
        return out
    key = frame_key(epic=epic, structure=structure, direction=proposal)
    lib = load_library(store)
    bucket = lib['buckets'].get(key)
    out['key'] = key
    out['bucket'] = bucket
    if not isinstance(bucket, dict):
        out['reason'] = 'no_loss_memory_for_frame'
        return out
    samples = int(bucket.get('samples') or 0)
    sl_count = int(bucket.get('sl_count') or 0)
    if samples < int(min_samples):
        out['reason'] = f'need_{min_samples}_samples_have_{samples}'
        out['confidence'] = samples / max(1, int(min_samples))
        return out
    rate = sl_count / samples if samples else 0.0
    # Also treat all-loss buckets (trail/vanish) as hostile.
    loss_count = int(bucket.get('loss_count') or 0)
    loss_rate = loss_count / samples if samples else 0.0
    hostile = max(rate, loss_rate)
    out['confidence'] = float(hostile)
    if hostile >= float(sl_rate):
        out['action'] = 'HOLD'
        out['reason'] = (
            f'structure_shadow: {structure} killed {proposal} '
            f'{loss_count}/{samples} (sl={sl_count}) — HOLD this frame')
    else:
        out['reason'] = (
            f'structure_memory_weak: {structure}/{proposal} '
            f'loss_rate={hostile:.0%} below gate')
    if store is not None:
        store.set(LAST_ADVICE_KEY, {
            'at': _now(), 'key': key, 'action': out['action'],
            'confidence': out['confidence'], 'reason': out['reason'],
            'structure': structure, 'proposal': proposal,
        })
    return out


def summary(store) -> dict:
    lib = load_library(store)
    snaps = store.get(SNAPSHOT_KEY) if store is not None else None
    last = store.get(LAST_ADVICE_KEY) if store is not None else None
    return {
        'buckets': len(lib.get('buckets') or {}),
        'snapshots': len(snaps) if isinstance(snaps, list) else 0,
        'updated': lib.get('updated'),
        'last_advice': last if isinstance(last, dict) else None,
        'top': sorted(
            (lib.get('buckets') or {}).values(),
            key=lambda b: int(b.get('sl_count') or 0),
            reverse=True,
        )[:8],
    }
