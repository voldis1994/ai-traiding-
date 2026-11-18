"""Post-trade OHLC structure memory — compare open/close vs now.

Keeps one NeuralCore: this module only describes whether price left the prior
trade's range / printed a new swing BOS·CHoCH·retest. Same-range → HOLD bias
for re-entry; new structure → allow OPEN to proceed.
"""
from __future__ import annotations

import math

STORE_KEY = 'structure_memory_last_v1'


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _ohlc(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    o = _finite(raw.get('open'))
    h = _finite(raw.get('high'))
    l = _finite(raw.get('low'))
    c = _finite(raw.get('close'))
    if c is None:
        c = _finite(raw.get('mid'))
    if None in (o, h, l, c) or min(o, h, l, c) <= 0:
        return None
    return {
        'open': float(o), 'high': float(max(o, h, l, c)),
        'low': float(min(o, h, l, c)), 'close': float(c),
        'at': raw.get('at'),
    }


def normalise(bars, *, limit=80) -> list:
    out = []
    for raw in bars or []:
        b = _ohlc(raw)
        if b is not None:
            out.append(b)
    if limit and len(out) > int(limit):
        out = out[-int(limit):]
    return out


def swing_pivots(bars, *, left=2, right=2) -> dict:
    """True swing highs/lows — not successive candle H/L compares.

    A swing high needs ``left`` bars with lower highs before and ``right``
    after (same for lows). Returns pivot lists and last confirmed sides.
    """
    path = normalise(bars)
    left = max(1, int(left))
    right = max(1, int(right))
    highs, lows = [], []
    n = len(path)
    if n < left + right + 1:
        return {
            'ready': False, 'swing_highs': [], 'swing_lows': [],
            'last_sh': None, 'last_sl': None, 'bars': n,
        }
    for i in range(left, n - right):
        hi = path[i]['high']
        lo = path[i]['low']
        if all(hi >= path[i - j]['high'] for j in range(1, left + 1)) and \
           all(hi > path[i + j]['high'] for j in range(1, right + 1)):
            highs.append({'i': i, 'price': hi, 'at': path[i].get('at')})
        if all(lo <= path[i - j]['low'] for j in range(1, left + 1)) and \
           all(lo < path[i + j]['low'] for j in range(1, right + 1)):
            lows.append({'i': i, 'price': lo, 'at': path[i].get('at')})
    return {
        'ready': True,
        'swing_highs': highs,
        'swing_lows': lows,
        'last_sh': highs[-1] if highs else None,
        'last_sl': lows[-1] if lows else None,
        'bars': n,
    }


def bos_choch(bars, *, left=2, right=2) -> dict:
    """Break of Structure / Change of Character from swing pivots.

    BOS  — break of last swing in the prevailing swing trend direction.
    CHoCH — first break against that swing trend (character change).
    Also flags a simple retest (close back inside prior swing after pierce).
    """
    path = normalise(bars)
    piv = swing_pivots(path, left=left, right=right)
    out = {
        'ready': bool(piv.get('ready')),
        'event': None,          # BOS_UP / BOS_DOWN / CHOCH_UP / CHOCH_DOWN
        'bias': None,           # UP / DOWN / RANGE
        'retest': None,         # UP / DOWN after pierce
        'false_break': False,
        'range_hi': None,
        'range_lo': None,
        'last_close': path[-1]['close'] if path else None,
        'swing': piv,
    }
    if not path or not piv.get('ready'):
        return out
    highs = piv['swing_highs']
    lows = piv['swing_lows']
    if len(highs) < 1 or len(lows) < 1:
        out['bias'] = 'RANGE'
        out['range_hi'] = max(b['high'] for b in path)
        out['range_lo'] = min(b['low'] for b in path)
        return out

    # Prevailing swing bias from last two opposite pivots order.
    last_sh = highs[-1]
    last_sl = lows[-1]
    if len(highs) >= 2 and highs[-1]['price'] > highs[-2]['price'] and \
       len(lows) >= 2 and lows[-1]['price'] > lows[-2]['price']:
        bias = 'UP'
    elif len(highs) >= 2 and highs[-1]['price'] < highs[-2]['price'] and \
         len(lows) >= 2 and lows[-1]['price'] < lows[-2]['price']:
        bias = 'DOWN'
    elif last_sh['i'] > last_sl['i']:
        bias = 'UP'
    elif last_sl['i'] > last_sh['i']:
        bias = 'DOWN'
    else:
        bias = 'RANGE'
    out['bias'] = bias
    out['range_hi'] = float(last_sh['price'])
    out['range_lo'] = float(last_sl['price'])

    close = path[-1]['close']
    prior_hi = float(last_sh['price'])
    prior_lo = float(last_sl['price'])
    # Need a thin buffer so one tick wick is not a structure event.
    span = max(prior_hi - prior_lo, 1e-9)
    buf = 0.05 * span

    broke_up = close > prior_hi + buf
    broke_down = close < prior_lo - buf
    if broke_up:
        if bias == 'UP':
            out['event'] = 'BOS_UP'
        elif bias == 'DOWN':
            out['event'] = 'CHOCH_UP'
        else:
            out['event'] = 'BOS_UP'
    elif broke_down:
        if bias == 'DOWN':
            out['event'] = 'BOS_DOWN'
        elif bias == 'UP':
            out['event'] = 'CHOCH_DOWN'
        else:
            out['event'] = 'BOS_DOWN'

    # Retest: after pierce, close returns into prior swing range.
    if len(path) >= 3:
        pierce_up = any(b['high'] > prior_hi + buf for b in path[-4:-1])
        pierce_dn = any(b['low'] < prior_lo - buf for b in path[-4:-1])
        if pierce_up and prior_lo <= close <= prior_hi:
            out['retest'] = 'UP'
            if not broke_up:
                out['false_break'] = True
        elif pierce_dn and prior_lo <= close <= prior_hi:
            out['retest'] = 'DOWN'
            if not broke_down:
                out['false_break'] = True
    return out


def snapshot_from_frame(frame, *, mid=None, bos=None) -> dict:
    """Compact structure snapshot stored on entry_brain / last_trade_outcome."""
    frame = frame if isinstance(frame, dict) else {}
    bos = bos if isinstance(bos, dict) else {}
    # Prefer swing range when available.
    range_hi = _finite(bos.get('range_hi')) or _finite(frame.get('swing_range_hi'))
    range_lo = _finite(bos.get('range_lo')) or _finite(frame.get('swing_range_lo'))
    if range_hi is None or range_lo is None:
        # Fallback: path span around mid (frame['range'] = high-low span).
        span = _finite(frame.get('range')) or 0.0
        m = _finite(mid) or _finite(frame.get('exit')) or _finite(frame.get('entry'))
        if m is not None and span > 0:
            range_hi = m + 0.5 * span
            range_lo = m - 0.5 * span
    # Last resort: tiny ATR-less band around mid so memory always has a box.
    if (range_hi is None or range_lo is None) and _finite(mid) is not None:
        m = float(mid)
        band = max(abs(m) * 1e-4, 0.05)
        range_hi = m + band
        range_lo = m - band
    return {
        'structure': str(frame.get('structure') or 'CHOP').upper(),
        'net_over_range': _finite(frame.get('net_over_range')),
        'hh_hl': int(frame.get('hh_hl') or 0),
        'lh_ll': int(frame.get('lh_ll') or 0),
        'breakout': bool(frame.get('breakout')),
        'event': bos.get('event'),
        'bias': bos.get('bias'),
        'retest': bos.get('retest'),
        'false_break': bool(bos.get('false_break')),
        'range_hi': range_hi,
        'range_lo': range_lo,
        'mid': _finite(mid),
        'bars': int(frame.get('bars') or 0),
    }


def snapshot_live(*, candles=None, market_context=None, mid=None) -> dict:
    """Live structure snapshot for OPEN-time comparison."""
    from . import structure_shadow
    path = normalise(candles, limit=structure_shadow.ADVISE_PATH_BARS)
    pre_n = int(getattr(structure_shadow, 'ADVISE_PRE_BARS', 40))
    raw = list(candles or [])
    pre = normalise(raw[:-len(path)] if len(raw) > len(path) else [], limit=pre_n)
    frame = structure_shadow.frame_from_ohlc(path, entry=mid, pre_bars=pre)
    bos = bos_choch(path)
    ctx_mid = mid
    if ctx_mid is None and isinstance(market_context, dict):
        ctx_mid = _finite(market_context.get('mid') or market_context.get('close'))
    if ctx_mid is None and path:
        ctx_mid = path[-1]['close']
    snap = snapshot_from_frame(frame, mid=ctx_mid, bos=bos)
    snap['live_event'] = bos.get('event')
    snap['live_retest'] = bos.get('retest')
    snap['live_bias'] = bos.get('bias')
    return snap


def same_range(prior: dict, live: dict, *, atr=None) -> bool:
    """True when live mid still inside prior trade swing/path range."""
    if not isinstance(prior, dict) or not isinstance(live, dict):
        return False
    lo = _finite(prior.get('range_lo'))
    hi = _finite(prior.get('range_hi'))
    mid = _finite(live.get('mid'))
    if None in (lo, hi, mid) or hi <= lo:
        return False
    span = hi - lo
    pad = 0.05 * span
    atr_v = _finite(atr)
    if atr_v is not None and atr_v > 0:
        pad = max(pad, 0.15 * atr_v)
    return (lo - pad) <= mid <= (hi + pad)


def developed_since(prior: dict, live: dict, *, action=None, atr=None) -> dict:
    """Has structure moved on since the prior trade close?

    New confirmed breakout / BOS / CHoCH / successful retest → ok.
    Still in same range without new event → not developed (prefer HOLD).
    """
    prior = prior if isinstance(prior, dict) else {}
    live = live if isinstance(live, dict) else {}
    if not prior:
        return {'needed': False, 'ok': True, 'reason': 'no_prior_structure'}
    event = str(live.get('live_event') or live.get('event') or '')
    retest = live.get('live_retest') or live.get('retest')
    false_br = bool(live.get('false_break'))
    action = action if action in ('BUY', 'SELL') else None

    bull_events = {'BOS_UP', 'CHOCH_UP', 'BREAKOUT_UP'}
    bear_events = {'BOS_DOWN', 'CHOCH_DOWN', 'BREAKOUT_DOWN'}
    if action == 'BUY' and event in bull_events:
        return {'needed': True, 'ok': True, 'reason': 'new_' + event.lower(),
                'event': event}
    if action == 'SELL' and event in bear_events:
        return {'needed': True, 'ok': True, 'reason': 'new_' + event.lower(),
                'event': event}
    if action is None and event in bull_events | bear_events:
        return {'needed': True, 'ok': True, 'reason': 'new_' + event.lower(),
                'event': event}
    if action == 'BUY' and retest == 'UP' and not false_br:
        return {'needed': True, 'ok': True, 'reason': 'retest_up'}
    if action == 'SELL' and retest == 'DOWN' and not false_br:
        return {'needed': True, 'ok': True, 'reason': 'retest_down'}

    # Structure label change (e.g. RANGE → TREND_UP) counts as development.
    prev_st = str(prior.get('structure') or '').upper()
    live_st = str(live.get('structure') or '').upper()
    if prev_st and live_st and prev_st != live_st:
        if action == 'BUY' and live_st in ('TREND_UP', 'BREAKOUT_UP'):
            return {'needed': True, 'ok': True, 'reason': 'structure_label_up'}
        if action == 'SELL' and live_st in ('TREND_DOWN', 'BREAKOUT_DOWN'):
            return {'needed': True, 'ok': True, 'reason': 'structure_label_down'}
        if action is None and live_st not in ('CHOP', 'RANGE', prev_st):
            return {'needed': True, 'ok': True, 'reason': 'structure_label_changed'}

    prior_lo = _finite(prior.get('range_lo'))
    prior_hi = _finite(prior.get('range_hi'))
    if prior_lo is None or prior_hi is None or prior_hi <= prior_lo:
        # Incomplete memory must not silently allow same-dir re-entry.
        return {'needed': True, 'ok': False, 'reason': 'need_structure_range',
                'mid': live.get('mid')}
    if same_range(prior, live, atr=atr):
        return {'needed': True, 'ok': False, 'reason': 'same_range_no_new_structure',
                'range_hi': prior.get('range_hi'), 'range_lo': prior.get('range_lo'),
                'mid': live.get('mid')}
    # Left the range without a clean event — still count as development.
    return {'needed': True, 'ok': True, 'reason': 'left_prior_range'}


def extension_chase(action, *, candles=None, market_context=None, atr=None,
                    lookback=30, top_frac=0.85, min_atr_mult=2.0) -> dict:
    """Block BUY/SELL that chase a vertical impulse into the extreme.

    Classic failure: huge M1 rally (or dump), neuron still BUY, entry near the
    local high on a weak bounce — then mean-reversion. Structure memory after a
    prior trade does not catch this on a fresh OPEN.
    """
    if action not in ('BUY', 'SELL'):
        return {'needed': False, 'ok': True, 'reason': 'not_directional'}
    path = normalise(candles, limit=max(10, int(lookback)))
    if len(path) < 8:
        return {'needed': False, 'ok': True, 'reason': 'need_more_bars'}
    hi = max(b['high'] for b in path)
    lo = min(b['low'] for b in path)
    span = hi - lo
    if span <= 1e-12:
        return {'needed': False, 'ok': True, 'reason': 'flat_path'}
    mid = None
    if isinstance(market_context, dict):
        mid = _finite(market_context.get('mid') or market_context.get('close'))
    if mid is None:
        mid = path[-1]['close']
    atr_v = _finite(atr)
    # Impulse must be large vs ATR (or absolute span if ATR unknown).
    if atr_v is not None and atr_v > 0:
        if span < float(min_atr_mult) * atr_v:
            return {'needed': False, 'ok': True, 'reason': 'impulse_not_large',
                    'span': span, 'atr': atr_v}
    pos = (mid - lo) / span  # 0=bottom, 1=top of lookback range
    # Recent micro-turn against the chase (rollover / bounce failure).
    last3 = path[-3:]
    reds = sum(1 for b in last3 if b['close'] < b['open'])
    greens = sum(1 for b in last3 if b['close'] > b['open'])
    frac = float(top_frac)
    if action == 'BUY' and pos >= frac:
        # At top of a large impulse — refuse chase, especially if already rolling.
        return {
            'needed': True, 'ok': False, 'reason': 'chase_extension_high',
            'pos': round(pos, 3), 'span': span, 'mid': mid,
            'range_hi': hi, 'range_lo': lo, 'rolling': reds >= 2,
        }
    if action == 'SELL' and pos <= (1.0 - frac):
        return {
            'needed': True, 'ok': False, 'reason': 'chase_extension_low',
            'pos': round(pos, 3), 'span': span, 'mid': mid,
            'range_hi': hi, 'range_lo': lo, 'rolling': greens >= 2,
        }
    return {'needed': False, 'ok': True, 'reason': 'not_extended',
            'pos': round(pos, 3), 'span': span}


def gate_reentry(*, action, last_outcome=None, candles=None,
                 market_context=None, atr=None) -> dict:
    """OPEN gate: after a close, require structure development vs that trade."""
    last = last_outcome if isinstance(last_outcome, dict) else {}
    prior = last.get('structure_at_close') or last.get('structure_at_open')
    if not isinstance(prior, dict) or not prior:
        return {'needed': False, 'ok': True, 'reason': 'no_structure_memory'}
    # Only enforce for same-direction re-entry (the case in the critique).
    last_dir = last.get('direction')
    if last_dir in ('BUY', 'SELL') and action in ('BUY', 'SELL') and last_dir != action:
        return {'needed': False, 'ok': True, 'reason': 'opposite_side_ok'}
    live = snapshot_live(candles=candles, market_context=market_context,
                         mid=_finite((market_context or {}).get('mid')
                                     or (market_context or {}).get('close')))
    developed = developed_since(prior, live, action=action, atr=atr)
    developed['live'] = {
        'structure': live.get('structure'),
        'event': live.get('live_event'),
        'retest': live.get('live_retest'),
        'mid': live.get('mid'),
        'range_hi': live.get('range_hi'),
        'range_lo': live.get('range_lo'),
    }
    developed['prior'] = {
        'structure': prior.get('structure'),
        'event': prior.get('event'),
        'range_hi': prior.get('range_hi'),
        'range_lo': prior.get('range_lo'),
    }
    return developed


def persist_last(store, snapshot: dict) -> None:
    if store is None or not isinstance(snapshot, dict):
        return
    store.set(STORE_KEY, dict(snapshot))
