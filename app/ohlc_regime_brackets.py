"""Regime-aware SL/TP from OHLC zones + scenario (BUY and SELL symmetric).

Guide 1:1:
  • Zone / trend pullback → SL behind entry zone; TP at opposite zone (min R:R)
  • Retest → SL behind retested level; TP measured / opposite structure
  • Breakout → SL behind broken level (not a distant old pullback box)
  • Range edge → SL behind near edge; TP opposite edge
  • CHOCH → SL behind new character swing; TP prior opposite structure

Never invent mid ± κ. Distances always meet broker floor + ATR buffer.
"""
from __future__ import annotations

import math

from . import structure_memory

DEFAULT_MIN_RR = 2.0


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _buf(tick=0.01, spread=0., atr=None):
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    try:
        sp = max(float(spread or 0), 0.)
    except (TypeError, ValueError):
        sp = 0.
    b = max(2.0 * tick_f, 2.0 * sp)
    atr_v = _finite(atr)
    if atr_v is not None and atr_v > 0:
        b = max(b, 0.15 * atr_v)
    return b, tick_f, sp, atr_v


def _floor(min_distance, buffer, atr_v, atr_frac=0.5):
    try:
        mn = max(float(min_distance or 0), 0.)
    except (TypeError, ValueError):
        mn = 0.
    floor = mn + buffer if (mn + buffer) > 0 else buffer
    if atr_v is not None and atr_v > 0:
        floor = max(floor, float(atr_frac) * float(atr_v))
    return floor


def _min_rr(store=None):
    if store is None:
        return DEFAULT_MIN_RR
    try:
        from . import bracket_learn
        return float(bracket_learn._min_rr(store))
    except Exception:
        return DEFAULT_MIN_RR


def _zones_pair(zones):
    """Demand (BUY box) + Supply (SELL box) from compute_entry_zones output."""
    z = zones if isinstance(zones, dict) else {}
    demand = z.get('buy_entry') if isinstance(z.get('buy_entry'), dict) else None
    supply = z.get('sell_entry') if isinstance(z.get('sell_entry'), dict) else None
    return demand, supply


def _sl_behind_box(action, entry, box, *, buffer, floor):
    if not isinstance(box, dict):
        return None
    if action == 'BUY':
        anchor = _finite(box.get('sl_anchor'))
        if anchor is None:
            anchor = _finite(box.get('lo'))
        if anchor is None:
            return None
        stop = min(float(anchor) - buffer, float(entry) - floor)
        return stop if stop < entry else None
    anchor = _finite(box.get('sl_anchor'))
    if anchor is None:
        anchor = _finite(box.get('hi'))
    if anchor is None:
        return None
    stop = max(float(anchor) + buffer, float(entry) + floor)
    return stop if stop > entry else None


def _tp_opposite_box(action, entry, opp, *, buffer, risk, min_rr):
    """TP at opposite Demand/Supply box; stretch to min_rr if needed."""
    need = max(float(risk) * float(min_rr), float(risk) * 1.0)
    if action == 'BUY':
        if isinstance(opp, dict):
            # Target near opposite supply low (first touch of premium).
            tgt = _finite(opp.get('lo'))
            if tgt is not None and tgt > entry + buffer:
                return max(float(tgt), entry + need)
        return entry + need
    if isinstance(opp, dict):
        tgt = _finite(opp.get('hi'))
        if tgt is not None and tgt < entry - buffer:
            return min(float(tgt), entry - need)
    return entry - need


def _range_levels(scenario):
    box = (scenario or {}).get('range') if isinstance(scenario, dict) else None
    if not isinstance(box, dict):
        return None, None
    return _finite(box.get('lo')), _finite(box.get('hi'))


def _swing_fallback_tp(action, entry, highs, lows, *, floor, risk, min_rr):
    need = max(float(risk) * float(min_rr), float(floor))
    if action == 'BUY':
        cands = [s for s in (highs or []) if _finite(s.get('price')) is not None
                 and float(s['price']) >= entry + floor]
        if cands:
            return float(min(cands, key=lambda s: float(s['price']))['price'])
        return entry + need
    cands = [s for s in (lows or []) if _finite(s.get('price')) is not None
             and float(s['price']) <= entry - floor]
    if cands:
        return float(max(cands, key=lambda s: float(s['price']))['price'])
    return entry - need


def regime_brackets(*, candles, entry, action, scenario=None, zones=None,
                    entry_zone=None, trigger=None, tick=0.01, spread=0.,
                    min_distance=0., atr=None, store=None) -> dict:
    """Compute SL/TP for the live scenario — BUY and SELL use the same geometry.

    Returns same shape as swing_anchor_brackets (ready/distances/levels/source).
    """
    out = {
        'ready': False, 'reason': 'unready', 'source': 'regime_brackets',
        'stop_distance': None, 'profit_distance': None,
        'stop_level': None, 'profit_level': None,
        'swing_hi': None, 'swing_lo': None,
        'regime': None, 'trigger': trigger, 'min_rr': _min_rr(store),
        'zone_sl': False, 'zone_tp': False,
    }
    action = (action or '').upper()
    if action not in ('BUY', 'SELL'):
        return dict(out, reason='bad_action')
    entry_f = _finite(entry)
    if entry_f is None or entry_f <= 0:
        return dict(out, reason='bad_entry')

    buffer, tick_f, _sp, atr_v = _buf(tick, spread, atr)
    floor = _floor(min_distance, buffer, atr_v)
    min_rr = max(1.0, float(out['min_rr'] or DEFAULT_MIN_RR))
    sc = scenario if isinstance(scenario, dict) else {}
    name = str(sc.get('scenario') or 'UNKNOWN').upper()
    trig = str(trigger or '').lower()
    demand, supply = _zones_pair(zones)
    # Prefer explicit entry_zone (the box we actually entered).
    if isinstance(entry_zone, dict) and entry_zone.get('side') == 'BUY':
        demand = entry_zone
    elif isinstance(entry_zone, dict) and entry_zone.get('side') == 'SELL':
        supply = entry_zone
    elif isinstance(entry_zone, dict):
        if action == 'BUY':
            demand = entry_zone
        else:
            supply = entry_zone

    path = structure_memory.normalise(candles, limit=80)
    piv = structure_memory.swing_pivots(path, left=2, right=2) if path else {}
    highs = list(piv.get('swing_highs') or [])
    lows = list(piv.get('swing_lows') or [])
    r_lo, r_hi = _range_levels(sc)

    stop = profit = None
    reason = 'regime'
    zone_sl = zone_tp = False

    # --- BREAKOUT / blind break: SL behind broken level, not old pullback ---
    if trig == 'break' or name in ('BREAKOUT_UP', 'BREAKOUT_DOWN'):
        if action == 'BUY':
            # Broken resistance ≈ prior range_hi / supply.lo / last swing high below entry.
            br = r_hi
            if br is None and isinstance(supply, dict):
                br = _finite(supply.get('lo'))
            if br is None and highs:
                below = [s for s in highs if float(s['price']) <= entry_f]
                br = float(max(below, key=lambda s: float(s['price']))['price']) if below else None
            if br is not None:
                stop = min(float(br) - buffer, entry_f - floor)
            if isinstance(supply, dict) and _finite(supply.get('hi')) is not None:
                # Next premium / extension: opposite supply high or 2R.
                risk0 = entry_f - (stop if stop is not None else entry_f - floor)
                profit = _tp_opposite_box(
                    'BUY', entry_f, supply, buffer=buffer, risk=max(risk0, floor),
                    min_rr=min_rr)
                zone_tp = True
            reason = 'breakout_buy'
        else:
            br = r_lo
            if br is None and isinstance(demand, dict):
                br = _finite(demand.get('hi'))
            if br is None and lows:
                above = [s for s in lows if float(s['price']) >= entry_f]
                br = float(min(above, key=lambda s: float(s['price']))['price']) if above else None
            if br is not None:
                stop = max(float(br) + buffer, entry_f + floor)
            if isinstance(demand, dict):
                risk0 = (stop if stop is not None else entry_f + floor) - entry_f
                profit = _tp_opposite_box(
                    'SELL', entry_f, demand, buffer=buffer, risk=max(risk0, floor),
                    min_rr=min_rr)
                zone_tp = True
            reason = 'breakout_sell'

    # --- RANGE / COMPRESSION: edge to edge ---
    elif name in ('RANGE', 'COMPRESSION'):
        if action == 'BUY':
            edge = r_lo
            if edge is None and isinstance(demand, dict):
                edge = _finite(demand.get('lo'))
            if edge is not None:
                stop = min(float(edge) - buffer, entry_f - floor)
                zone_sl = True
            opp = r_hi
            if opp is None and isinstance(supply, dict):
                opp = _finite(supply.get('lo'))
            risk0 = entry_f - (stop if stop is not None else entry_f - floor)
            if opp is not None and opp > entry_f + buffer:
                profit = max(float(opp), entry_f + max(risk0, floor) * min_rr)
                zone_tp = True
            reason = 'range_buy_edge'
        else:
            edge = r_hi
            if edge is None and isinstance(supply, dict):
                edge = _finite(supply.get('hi'))
            if edge is not None:
                stop = max(float(edge) + buffer, entry_f + floor)
                zone_sl = True
            opp = r_lo
            if opp is None and isinstance(demand, dict):
                opp = _finite(demand.get('hi'))
            risk0 = (stop if stop is not None else entry_f + floor) - entry_f
            if opp is not None and opp < entry_f - buffer:
                profit = min(float(opp), entry_f - max(risk0, floor) * min_rr)
                zone_tp = True
            reason = 'range_sell_edge'

    # --- RETEST ---
    elif name in ('RETEST_UP', 'RETEST_DOWN'):
        box = demand if action == 'BUY' else supply
        stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        zone_sl = stop is not None
        risk0 = abs(entry_f - (stop if stop is not None else entry_f)) or floor
        opp = supply if action == 'BUY' else demand
        profit = _tp_opposite_box(
            action, entry_f, opp, buffer=buffer, risk=max(risk0, floor), min_rr=min_rr)
        zone_tp = isinstance(opp, dict)
        reason = 'retest_' + action.lower()

    # --- CHOCH ---
    elif name in ('CHOCH_UP', 'CHOCH_DOWN'):
        box = demand if action == 'BUY' else supply
        stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        if stop is None:
            # Fall back to last swing on protective side.
            if action == 'BUY' and lows:
                cands = [s for s in lows if float(s['price']) <= entry_f - floor]
                if cands:
                    sl_p = float(max(cands, key=lambda s: float(s['price']))['price'])
                    stop = min(sl_p - buffer, entry_f - floor)
            elif action == 'SELL' and highs:
                cands = [s for s in highs if float(s['price']) >= entry_f + floor]
                if cands:
                    sl_p = float(min(cands, key=lambda s: float(s['price']))['price'])
                    stop = max(sl_p + buffer, entry_f + floor)
        zone_sl = stop is not None and box is not None
        risk0 = abs(entry_f - (stop if stop is not None else entry_f)) or floor
        opp = supply if action == 'BUY' else demand
        profit = _tp_opposite_box(
            action, entry_f, opp, buffer=buffer, risk=max(risk0, floor), min_rr=min_rr)
        if profit is None or (action == 'BUY' and profit <= entry_f) or (
                action == 'SELL' and profit >= entry_f):
            profit = _swing_fallback_tp(
                action, entry_f, highs, lows, floor=floor, risk=max(risk0, floor),
                min_rr=min_rr)
        zone_tp = isinstance(opp, dict)
        reason = 'choch_' + action.lower()

    # --- FAKEOUT fade ---
    elif name == 'FAKEOUT':
        if action == 'BUY':
            # Sweep low then fade up — SL below sweep / range lo.
            edge = r_lo
            if edge is None and lows:
                edge = float(min(lows, key=lambda s: float(s['price']))['price'])
            if edge is not None:
                stop = min(float(edge) - buffer, entry_f - floor)
            risk0 = entry_f - (stop if stop is not None else entry_f - floor)
            mid_r = 0.5 * ((r_lo or entry_f) + (r_hi or entry_f)) if r_lo and r_hi else None
            profit = max(mid_r or (entry_f + max(risk0, floor) * min_rr),
                         entry_f + max(risk0, floor) * min_rr)
            reason = 'fakeout_fade_buy'
        else:
            edge = r_hi
            if edge is None and highs:
                edge = float(max(highs, key=lambda s: float(s['price']))['price'])
            if edge is not None:
                stop = max(float(edge) + buffer, entry_f + floor)
            risk0 = (stop if stop is not None else entry_f + floor) - entry_f
            mid_r = 0.5 * ((r_lo or entry_f) + (r_hi or entry_f)) if r_lo and r_hi else None
            profit = min(mid_r or (entry_f - max(risk0, floor) * min_rr),
                         entry_f - max(risk0, floor) * min_rr)
            reason = 'fakeout_fade_sell'

    # --- TREND / zone pullback (default structural) ---
    else:
        box = demand if action == 'BUY' else supply
        stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        zone_sl = stop is not None
        if stop is None:
            # Nearest protective swing.
            if action == 'BUY' and lows:
                cands = [s for s in lows if float(s['price']) <= entry_f - floor]
                if cands:
                    stop = float(max(cands, key=lambda s: float(s['price']))['price']) - buffer
            elif action == 'SELL' and highs:
                cands = [s for s in highs if float(s['price']) >= entry_f + floor]
                if cands:
                    stop = float(min(cands, key=lambda s: float(s['price']))['price']) + buffer
        risk0 = abs(entry_f - (stop if stop is not None else entry_f)) or floor
        opp = supply if action == 'BUY' else demand
        profit = _tp_opposite_box(
            action, entry_f, opp, buffer=buffer, risk=max(risk0, floor), min_rr=min_rr)
        zone_tp = isinstance(opp, dict)
        if (action == 'BUY' and (profit is None or profit <= entry_f + floor * 0.5)) or (
                action == 'SELL' and (profit is None or profit >= entry_f - floor * 0.5)):
            profit = _swing_fallback_tp(
                action, entry_f, highs, lows, floor=floor, risk=max(risk0, floor),
                min_rr=min_rr)
            zone_tp = False
        reason = 'trend_zone_' + action.lower()

    # Ensure stop/profit exist and meet floor + sides.
    if stop is None:
        stop = entry_f - floor if action == 'BUY' else entry_f + floor
    if profit is None:
        risk = abs(entry_f - stop) or floor
        profit = (entry_f + risk * min_rr) if action == 'BUY' else (entry_f - risk * min_rr)

    if action == 'BUY':
        stop = min(float(stop), entry_f - floor)
        stop_d = entry_f - stop
        profit_d = float(profit) - entry_f
        if profit_d < floor:
            profit = entry_f + max(floor, stop_d * min_rr)
            profit_d = profit - entry_f
        if profit_d / stop_d + 1e-12 < min_rr and stop_d > 1e-12:
            profit = entry_f + stop_d * min_rr
            profit_d = profit - entry_f
    else:
        stop = max(float(stop), entry_f + floor)
        stop_d = stop - entry_f
        profit_d = entry_f - float(profit)
        if profit_d < floor:
            profit = entry_f - max(floor, stop_d * min_rr)
            profit_d = entry_f - profit
        if profit_d / stop_d + 1e-12 < min_rr and stop_d > 1e-12:
            profit = entry_f - stop_d * min_rr
            profit_d = entry_f - profit

    if stop_d < floor or profit_d < floor:
        return dict(out, reason='regime_distances_below_floor',
                    stop_distance=stop_d, profit_distance=profit_d,
                    stop_level=float(stop), profit_level=float(profit),
                    regime=name)

    return {
        'ready': True,
        'reason': reason,
        'source': 'regime_brackets',
        'stop_distance': float(stop_d),
        'profit_distance': float(profit_d),
        'stop_level': float(stop),
        'profit_level': float(profit),
        'swing_hi': float(profit) if action == 'BUY' else float(stop),
        'swing_lo': float(stop) if action == 'BUY' else float(profit),
        'kappa': float(profit_d / stop_d) if stop_d > 1e-12 else min_rr,
        'min_rr': float(min_rr),
        'rr': float(profit_d / stop_d) if stop_d > 1e-12 else None,
        'regime': name,
        'trigger': trig or None,
        'zone_sl': bool(zone_sl),
        'zone_tp': bool(zone_tp),
        'buffer': float(buffer),
        'atr_used': float(atr_v) if atr_v is not None else None,
        'bos_event': sc.get('event'),
        'bos_bias': sc.get('bias'),
    }
