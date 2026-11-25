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

    # Fakeout vs held retest (1m guide — do not conflate).
    # Fakeout: pierce (incl. last bar) then close back *inside* prior range.
    # Held retest: recent close beyond level, pullback to level, close holds outside.
    if len(path) >= 3:
        recent = path[-4:]
        pierce_up = any(b['high'] > prior_hi + buf for b in recent)
        pierce_dn = any(b['low'] < prior_lo - buf for b in recent)
        inside = prior_lo <= close <= prior_hi
        if pierce_up and inside and not broke_up:
            out['false_break'] = True
        elif pierce_dn and inside and not broke_down:
            out['false_break'] = True
        else:
            had_break_up = broke_up or any(
                b['close'] > prior_hi + buf for b in path[-6:-1])
            had_break_dn = broke_down or any(
                b['close'] < prior_lo - buf for b in path[-6:-1])
            near_hi = abs(close - prior_hi) <= 0.35 * span
            near_lo = abs(close - prior_lo) <= 0.35 * span
            touched_hi = any(b['low'] <= prior_hi + buf for b in path[-3:])
            touched_lo = any(b['high'] >= prior_lo - buf for b in path[-3:])
            if (had_break_up and close >= prior_hi - buf and near_hi
                    and touched_hi and not out['false_break']):
                out['retest'] = 'UP'
            elif (had_break_dn and close <= prior_lo + buf and near_lo
                    and touched_lo and not out['false_break']):
                out['retest'] = 'DOWN'
    return out


def label_swing_structure(bars, *, left=2, right=2) -> dict:
    """Tag each confirmed swing as HH/LH or HL/LL — real structure, not bar noise.

    Returns labeled pivots plus a structure bias:
      UP   — last swings print HH + HL
      DOWN — last swings print LH + LL
      RANGE — mixed / insufficient pivots
    """
    piv = swing_pivots(bars, left=left, right=right)
    highs = list(piv.get('swing_highs') or [])
    lows = list(piv.get('swing_lows') or [])
    labeled_h = []
    for i, h in enumerate(highs):
        row = dict(h)
        if i == 0:
            row['tag'] = 'SH'
        else:
            row['tag'] = 'HH' if float(h['price']) > float(highs[i - 1]['price']) else 'LH'
        labeled_h.append(row)
    labeled_l = []
    for i, lo in enumerate(lows):
        row = dict(lo)
        if i == 0:
            row['tag'] = 'SL'
        else:
            row['tag'] = 'HL' if float(lo['price']) > float(lows[i - 1]['price']) else 'LL'
        labeled_l.append(row)
    last_h = labeled_h[-1]['tag'] if labeled_h else None
    last_l = labeled_l[-1]['tag'] if labeled_l else None
    if last_h == 'HH' and last_l == 'HL':
        bias = 'UP'
    elif last_h == 'LH' and last_l == 'LL':
        bias = 'DOWN'
    else:
        bias = 'RANGE'
    return {
        'ready': bool(piv.get('ready')),
        'swing_highs': labeled_h,
        'swing_lows': labeled_l,
        'last_high_tag': last_h,
        'last_low_tag': last_l,
        'bias': bias,
        'hh_count': sum(1 for x in labeled_h if x.get('tag') == 'HH'),
        'lh_count': sum(1 for x in labeled_h if x.get('tag') == 'LH'),
        'hl_count': sum(1 for x in labeled_l if x.get('tag') == 'HL'),
        'll_count': sum(1 for x in labeled_l if x.get('tag') == 'LL'),
    }


def _atr_proxy(path, period=14):
    if len(path) < 2:
        return None
    trs = []
    prev_c = path[0]['close']
    for b in path[1:]:
        tr = max(
            b['high'] - b['low'],
            abs(b['high'] - prev_c),
            abs(b['low'] - prev_c),
        )
        trs.append(tr)
        prev_c = b['close']
    if not trs:
        return None
    n = min(max(2, int(period)), len(trs))
    return sum(trs[-n:]) / float(n)


def detect_base_impulse_zones(bars, *, atr=None, limit=80, max_zones=6) -> dict:
    """1m Supply/Demand: base (consolidation) immediately before impulse.

    Guide 1:1:
      • Impulse UP after base  → Demand (BUY) zone = base High→Low
      • Impulse DOWN after base → Supply (SELL) zone = base High→Low
      • Quality: strong impulse, clean/narrow base, prefer fresher first touch
    """
    path = normalise(bars, limit=limit)
    out = {
        'ready': False, 'reason': 'too_few_bars',
        'demand': [], 'supply': [], 'zones': [],
        'atr': None, 'bars': len(path),
    }
    if len(path) < 16:
        return out
    atr_v = _finite(atr) or _atr_proxy(path, 14)
    if atr_v is None or atr_v <= 0:
        # Fallback from median bar range.
        spans = sorted(max(1e-9, b['high'] - b['low']) for b in path)
        atr_v = spans[len(spans) // 2]
    out['atr'] = float(atr_v)
    found = []
    # Impulse ends at index i; base sits immediately before impulse start.
    for i in range(10, len(path)):
        # 1–2 bar dumps/spikes count (US Tech flag→one red impulse); longer
        # impulses keep the majority-body rule.
        for impulse_len in (1, 2, 3, 4, 5, 6, 7, 8):
            imp_start = i - impulse_len + 1
            if imp_start < 3:
                continue
            imp = path[imp_start:i + 1]
            move = imp[-1]['close'] - imp[0]['open']
            imp_hi = max(b['high'] for b in imp)
            imp_lo = min(b['low'] for b in imp)
            imp_span = max(imp_hi - imp_lo, 1e-12)
            if abs(move) < 1.35 * atr_v:
                continue
            if abs(move) / imp_span < 0.55:
                continue
            # Mostly one-way bodies (majority; single-bar needs 1).
            up_bodies = sum(1 for b in imp if b['close'] > b['open'])
            dn_bodies = sum(1 for b in imp if b['close'] < b['open'])
            need_bodies = max(1, (impulse_len + 1) // 2)
            if move > 0 and up_bodies < need_bodies:
                continue
            if move < 0 and dn_bodies < need_bodies:
                continue
            direction = 'UP' if move > 0 else 'DOWN'
            for base_len in (2, 3, 4, 5, 6):
                b0 = imp_start - base_len
                if b0 < 0:
                    continue
                base = path[b0:imp_start]
                if len(base) < 2:
                    continue
                base_hi = max(b['high'] for b in base)
                base_lo = min(b['low'] for b in base)
                base_span = base_hi - base_lo
                # Flag bases can be ~0.6× a single dump bar (US Tech circle).
                if base_span <= 0 or base_span > 0.70 * abs(move):
                    continue
                # Clean base: bodies overlap / small relative to impulse.
                body_mids = [0.5 * (b['open'] + b['close']) for b in base]
                mid_span = max(body_mids) - min(body_mids)
                if mid_span > 0.70 * base_span + 1e-12:
                    continue
                if base_span > 1.15 * atr_v and base_len > 4:
                    continue
                # Touches after impulse (freshness).
                touches = 0
                for b in path[i + 1:]:
                    if b['low'] <= base_hi and b['high'] >= base_lo:
                        touches += 1
                # Quality: strong move, tight base, fewer post-touches, recent.
                # Just-formed flag→impulse must beat older higher boxes — otherwise
                # LIVE keeps "seeing" stale Supply while the circled break is ignored.
                q = (abs(move) / atr_v) * (abs(move) / max(base_span, 1e-9))
                q *= 1.0 / (1.0 + 0.65 * touches)
                age = len(path) - 1 - i
                q *= 1.0 / (1.0 + 0.22 * age)
                q *= 1.0 + 0.35 / float(max(1, impulse_len))
                if age <= 1:
                    q *= 2.2
                elif age <= 3:
                    q *= 1.45
                side = 'BUY' if direction == 'UP' else 'SELL'
                found.append({
                    'side': side,
                    'kind': 'demand' if side == 'BUY' else 'supply',
                    'lo': float(base_lo),
                    'hi': float(base_hi),
                    'impulse_dir': direction,
                    'impulse_move': float(move),
                    'impulse_len': int(impulse_len),
                    'base_len': int(base_len),
                    'base_i0': int(b0),
                    'base_i1': int(imp_start - 1),
                    'impulse_i0': int(imp_start),
                    'impulse_i1': int(i),
                    'touches': int(touches),
                    'quality': float(q),
                    'at': base[0].get('at'),
                })
    if not found:
        out['reason'] = 'no_base_impulse'
        return out
    # Deduplicate overlapping same-side zones — keep highest quality.
    found.sort(key=lambda z: -float(z['quality']))
    picked = []
    for z in found:
        overlap = False
        for p in picked:
            if p['side'] != z['side']:
                continue
            lo = max(p['lo'], z['lo'])
            hi = min(p['hi'], z['hi'])
            if hi >= lo and (hi - lo) >= 0.45 * min(p['hi'] - p['lo'], z['hi'] - z['lo']):
                overlap = True
                break
        if not overlap:
            picked.append(z)
        if len(picked) >= int(max_zones):
            break
    demand = [z for z in picked if z['side'] == 'BUY']
    supply = [z for z in picked if z['side'] == 'SELL']
    out['demand'] = demand
    out['supply'] = supply
    out['zones'] = picked
    out['ready'] = bool(picked)
    out['reason'] = 'base_impulse' if picked else 'empty'
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
    """Block only exhaustion chases — not clear trend continuation.

    Block:
      • BUY at the top of a large impulse while M1 is already rolling (reds)
      • SELL at the bottom while M1 is bouncing (greens)

    Allow:
      • SELL still printing reds at the low (the live dump that became HOLD)
      • BUY still printing greens at the high (continuation, not a weak bounce)

    Earlier “always block at extreme” turned every sustained move into HOLD.
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
    last3 = path[-3:]
    reds = sum(1 for b in last3 if b['close'] < b['open'])
    greens = sum(1 for b in last3 if b['close'] > b['open'])
    # Same-dir M1 streak = clear candle pressure — do not HOLD for “weak body”.
    same_dir = 0
    for b in reversed(path[-8:]):
        if action == 'SELL' and b['close'] < b['open']:
            same_dir += 1
        elif action == 'BUY' and b['close'] > b['open']:
            same_dir += 1
        else:
            break
    frac = float(top_frac)
    if action == 'BUY' and pos >= frac:
        if same_dir >= 2:
            return {
                'needed': False, 'ok': True, 'reason': 'continuation_at_high',
                'pos': round(pos, 3), 'span': span, 'mid': mid,
                'same_dir_streak': same_dir,
            }
        if reds >= 2:
            return {
                'needed': True, 'ok': False, 'reason': 'chase_extension_high',
                'pos': round(pos, 3), 'span': span, 'mid': mid,
                'range_hi': hi, 'range_lo': lo, 'rolling': True,
            }
        return {
            'needed': False, 'ok': True, 'reason': 'top_without_rollover',
            'pos': round(pos, 3), 'same_dir_streak': same_dir, 'reds': reds,
        }
    if action == 'SELL' and pos <= (1.0 - frac):
        if same_dir >= 2:
            # Clear dump with consecutive red M1 — OPEN allowed (user screenshot).
            return {
                'needed': False, 'ok': True, 'reason': 'continuation_at_low',
                'pos': round(pos, 3), 'span': span, 'mid': mid,
                'same_dir_streak': same_dir,
            }
        if greens >= 2:
            return {
                'needed': True, 'ok': False, 'reason': 'chase_extension_low',
                'pos': round(pos, 3), 'span': span, 'mid': mid,
                'range_hi': hi, 'range_lo': lo, 'rolling': True,
            }
        return {
            'needed': False, 'ok': True, 'reason': 'low_without_bounce',
            'pos': round(pos, 3), 'same_dir_streak': same_dir, 'greens': greens,
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
