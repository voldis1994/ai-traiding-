"""Hard OPEN veto from completed OHLC — when NOT to trade.

Five regimes (operator card):
  1. Sideways chop — overlapping candles, no clear structure
  2. Too low volatility — ATR / ranges too small vs costs
  3. Many dojis — indecision bodies
  4. Oversized impulse — last bar ≫ ATR (chase)
  5. Chaotic structure — no consistent HH/HL or LH/LL

These replace “wait forever for M1 candle colour confirm” as the candle-quality
gate. News / fusion / readiness stay elsewhere.
"""
from __future__ import annotations

import math

DEFAULTS = {
    'enabled': 1,
    'lookback': 12,
    'chop_flip_min': 5,          # direction flips in lookback
    'chop_progress_max': 0.28,   # |net| / sum(range) → chop
    'low_atr_frac': 0.00035,     # ATR/mid below this → too quiet (GOLD ~1.3/3700)
    'low_range_vs_spread': 1.2,  # median range < spread * this
    'doji_body_frac': 0.18,      # |c-o|/(h-l)
    'doji_share_min': 0.45,      # fraction of bars that are dojis
    'impulse_atr_mult': 2.2,     # last range > mult * ATR14
    'chaos_swing_min': 4,        # need this many local swings to judge
}


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _params(store=None) -> dict:
    out = dict(DEFAULTS)
    if store is None:
        return out
    try:
        from .self_params import get as sp_get
        out['enabled'] = int(sp_get(store, 'ohlc_regime_gate_enabled', 1))
        out['lookback'] = int(sp_get(store, 'ohlc_regime_lookback', DEFAULTS['lookback']))
        out['chop_flip_min'] = int(sp_get(store, 'ohlc_regime_chop_flips', DEFAULTS['chop_flip_min']))
        out['chop_progress_max'] = float(
            sp_get(store, 'ohlc_regime_chop_progress', DEFAULTS['chop_progress_max']))
        out['low_atr_frac'] = float(
            sp_get(store, 'ohlc_regime_low_atr_frac', DEFAULTS['low_atr_frac']))
        out['doji_share_min'] = float(
            sp_get(store, 'ohlc_regime_doji_share', DEFAULTS['doji_share_min']))
        out['impulse_atr_mult'] = float(
            sp_get(store, 'ohlc_regime_impulse_atr', DEFAULTS['impulse_atr_mult']))
    except Exception:
        pass
    out['lookback'] = max(6, min(40, int(out['lookback'])))
    return out


def _atr14(candles) -> float | None:
    rows = [r for r in (candles or []) if isinstance(r, dict)]
    if len(rows) < 15:
        return None
    trs = []
    prev_c = None
    for r in rows[-(14 + 1):]:
        h = _finite(r.get('high')); l = _finite(r.get('low')); c = _finite(r.get('close'))
        if h is None or l is None:
            continue
        if prev_c is None:
            trs.append(max(0.0, h - l))
        else:
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        if c is not None:
            prev_c = c
    if len(trs) < 14:
        return None
    return float(sum(trs[-14:]) / 14.0)


def _body_dir(row) -> int:
    o = _finite(row.get('open')); c = _finite(row.get('close'))
    if o is None or c is None or abs(c - o) < 1e-12:
        return 0
    return 1 if c > o else -1


def _is_doji(row, body_frac: float) -> bool:
    o = _finite(row.get('open')); c = _finite(row.get('close'))
    h = _finite(row.get('high')); l = _finite(row.get('low'))
    if None in (o, c, h, l) or h <= l:
        return False
    return abs(c - o) / max(h - l, 1e-12) <= float(body_frac)


def _local_swings(candles, n: int = 3):
    """Simple swing highs/lows (n bars each side)."""
    rows = list(candles or [])
    highs, lows = [], []
    for i in range(n, len(rows) - n):
        h = _finite(rows[i].get('high')); l = _finite(rows[i].get('low'))
        if h is None or l is None:
            continue
        window_h = [_finite(rows[j].get('high')) for j in range(i - n, i + n + 1)]
        window_l = [_finite(rows[j].get('low')) for j in range(i - n, i + n + 1)]
        if all(x is not None and h >= x for x in window_h):
            highs.append(h)
        if all(x is not None and l <= x for x in window_l):
            lows.append(l)
    return highs, lows


def evaluate(*, candles, mid=None, spread=None, atr=None, structure=None,
             store=None) -> dict:
    """Return ``allowed`` False + reason when OHLC says do not OPEN."""
    p = _params(store)
    meta = {
        'enabled': bool(p.get('enabled')),
        'allowed': True,
        'reason': 'pass',
        'flags': {},
    }
    if not p.get('enabled'):
        meta['reason'] = 'disabled'
        return meta
    rows = [r for r in (candles or []) if isinstance(r, dict)]
    lb = int(p['lookback'])
    if len(rows) < max(8, lb // 2):
        meta['reason'] = 'need_more_candles'
        # Soft — do not block when history thin (bootstrap).
        return meta
    window = rows[-lb:]
    mid_v = _finite(mid) or _finite(window[-1].get('close')) or _finite(window[-1].get('mid'))
    atr_v = _finite(atr) or _atr14(rows)
    spread_v = _finite(spread) or 0.0
    meta['atr'] = atr_v
    meta['mid'] = mid_v

    # --- 4. Oversized impulse (check first — clear single-bar signal) ---
    last = window[-1]
    last_range = None
    h = _finite(last.get('high')); l = _finite(last.get('low'))
    if h is not None and l is not None:
        last_range = h - l
    if (atr_v is not None and atr_v > 0 and last_range is not None
            and last_range >= float(p['impulse_atr_mult']) * atr_v):
        return dict(meta, allowed=False, reason='no_trade_impulse',
                    flags={'last_range': last_range, 'atr': atr_v,
                           'mult': float(p['impulse_atr_mult'])})

    # --- 2. Low volatility ---
    ranges = []
    for r in window:
        hh = _finite(r.get('high')); ll = _finite(r.get('low'))
        if hh is not None and ll is not None and hh >= ll:
            ranges.append(hh - ll)
    med_range = sorted(ranges)[len(ranges) // 2] if ranges else None
    if mid_v and mid_v > 0 and atr_v is not None:
        if atr_v / mid_v < float(p['low_atr_frac']):
            return dict(meta, allowed=False, reason='no_trade_low_vol',
                        flags={'atr_frac': atr_v / mid_v, 'atr': atr_v})
    if (med_range is not None and spread_v > 0
            and med_range < float(p['low_range_vs_spread']) * spread_v):
        return dict(meta, allowed=False, reason='no_trade_low_vol',
                    flags={'median_range': med_range, 'spread': spread_v})

    # --- 3. Many dojis ---
    dojis = sum(1 for r in window if _is_doji(r, float(p['doji_body_frac'])))
    doji_share = dojis / max(1, len(window))
    meta['flags']['doji_share'] = doji_share
    if doji_share >= float(p['doji_share_min']):
        return dict(meta, allowed=False, reason='no_trade_doji',
                    flags={'doji_share': doji_share, 'n': dojis})

    # --- 1. Sideways chop ---
    flips = 0
    prev = 0
    for r in window:
        d = _body_dir(r)
        if d == 0:
            continue
        if prev and d != prev:
            flips += 1
        prev = d
    first_c = _finite(window[0].get('close'))
    last_c = _finite(window[-1].get('close'))
    sum_range = sum(ranges) if ranges else 0.0
    progress = (abs(last_c - first_c) / sum_range) if (
            first_c is not None and last_c is not None and sum_range > 1e-12) else 1.0
    meta['flags']['chop_flips'] = flips
    meta['flags']['chop_progress'] = progress
    if flips >= int(p['chop_flip_min']) and progress <= float(p['chop_progress_max']):
        return dict(meta, allowed=False, reason='no_trade_chop',
                    flags={'flips': flips, 'progress': progress})

    # --- 5. Chaotic structure ---
    struct = str(structure or '').upper()
    if struct in ('CHOP', 'RANGE_CHOP', 'CHAOS'):
        return dict(meta, allowed=False, reason='no_trade_chaos',
                    flags={'structure': struct})
    highs, lows = _local_swings(window, n=2)
    if len(highs) >= int(p['chaos_swing_min']) and len(lows) >= int(p['chaos_swing_min']):
        # Consistent up: HH + HL; down: LH + LL. Else chaos.
        hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
        lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
        hl = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
        ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1])
        up_ok = hh >= max(1, len(highs) // 2) and hl >= max(1, len(lows) // 2)
        down_ok = lh >= max(1, len(highs) // 2) and ll >= max(1, len(lows) // 2)
        meta['flags']['swings'] = {'hh': hh, 'lh': lh, 'hl': hl, 'll': ll}
        if not up_ok and not down_ok:
            return dict(meta, allowed=False, reason='no_trade_chaos',
                        flags=meta['flags'])

    return meta


def playbook_overrides_regime(regime: dict | None, zone_gate: dict | None) -> bool:
    """True when playbook break may OPEN despite impulse/chop candle veto.

    Oversized dump bar is often the flag→impulse the playbook just confirmed.
    Blocking that bar with ``no_trade_impulse`` made LIVE blind to the circled
    consolidation breaks. Soft chop during the break bar is the same class.
    """
    rg = regime if isinstance(regime, dict) else {}
    zg = zone_gate if isinstance(zone_gate, dict) else {}
    if rg.get('allowed', True):
        return True
    if not zg.get('allowed'):
        return False
    trigger = str(zg.get('trigger') or '').lower()
    if trigger not in ('break', 'compression_break'):
        return False
    reason = str(rg.get('reason') or '')
    return reason in ('no_trade_impulse', 'no_trade_chop')
