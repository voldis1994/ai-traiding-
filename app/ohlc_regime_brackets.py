"""Regime-aware SL + TP1/TP2 from *real* OHLC structure only.

Capital.com allows one ``profitLevel`` per deal. We therefore:
  • set broker TP = TP2 (far structural target)
  • remember TP1 (near structural) for robot scale-out + BE

Never invent mid ± κ or entry+N×risk as a fake level. If a structural
target is missing, fall back to the next real swing — or leave that rung
empty (TP1==TP2 when only one real target exists).
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
    z = zones if isinstance(zones, dict) else {}
    demand = z.get('buy_entry') if isinstance(z.get('buy_entry'), dict) else None
    supply = z.get('sell_entry') if isinstance(z.get('sell_entry'), dict) else None
    return demand, supply


def _sl_behind_box(action, entry, box, *, buffer, floor):
    if not isinstance(box, dict):
        return None
    if action == 'BUY':
        anchor = _finite(box.get('sl_anchor')) or _finite(box.get('lo'))
        if anchor is None:
            return None
        stop = min(float(anchor) - buffer, float(entry) - floor)
        return stop if stop < entry else None
    anchor = _finite(box.get('sl_anchor')) or _finite(box.get('hi'))
    if anchor is None:
        return None
    stop = max(float(anchor) + buffer, float(entry) + floor)
    return stop if stop > entry else None


def _range_levels(scenario):
    box = (scenario or {}).get('range') if isinstance(scenario, dict) else None
    if not isinstance(box, dict):
        return None, None
    return _finite(box.get('lo')), _finite(box.get('hi'))


def _swing_targets(action, entry, highs, lows, *, floor):
    """Ordered real swing targets beyond entry (nearest → farthest)."""
    out = []
    if action == 'BUY':
        for s in highs or []:
            px = _finite((s or {}).get('price'))
            if px is not None and px >= entry + floor:
                out.append(float(px))
        out.sort()
    else:
        for s in lows or []:
            px = _finite((s or {}).get('price'))
            if px is not None and px <= entry - floor:
                out.append(float(px))
        out.sort(reverse=True)
    # Dedup near-equal.
    clean = []
    for p in out:
        if not clean or abs(p - clean[-1]) > floor * 0.25:
            clean.append(p)
    return clean


def _level_ok(action, entry, level, *, floor):
    lv = _finite(level)
    if lv is None:
        return None
    if action == 'BUY' and lv >= entry + floor:
        return float(lv)
    if action == 'SELL' and lv <= entry - floor:
        return float(lv)
    return None


def _pick_tp_ladder(action, entry, *, candidates, risk, min_rr, floor):
    """Pick TP1/TP2 only from real candidate levels (OHLC / zone / range).

    TP1 = nearest structural target beyond entry.
    TP2 = farther structural target that clears min_rr when possible.
    If only one real level exists → TP1 == TP2.
    If none → None (caller may refuse ready).
    """
    real = []
    for c in candidates or []:
        lv = _level_ok(action, entry, c, floor=floor)
        if lv is not None:
            real.append(lv)
    if action == 'BUY':
        real = sorted(set(real))
    else:
        real = sorted(set(real), reverse=True)
    if not real:
        return None, None, False
    tp1 = real[0]
    need = float(entry) + (risk * min_rr if action == 'BUY' else -risk * min_rr)
    tp2 = tp1
    structural_rr = False
    for lv in real[1:]:
        tp2 = lv
        if action == 'BUY' and lv + 1e-12 >= need:
            structural_rr = True
            break
        if action == 'SELL' and lv - 1e-12 <= need:
            structural_rr = True
            break
    # If farthest still below RR but is a real level — keep it (honest OHLC).
    if action == 'BUY' and tp2 + 1e-12 >= need:
        structural_rr = True
    if action == 'SELL' and tp2 - 1e-12 <= need:
        structural_rr = True
    return float(tp1), float(tp2), structural_rr


def _measured_move(action, entry, r_lo, r_hi):
    """Breakout measured move from real range height — not invented κ."""
    if r_lo is None or r_hi is None or not (r_hi > r_lo):
        return None
    height = float(r_hi) - float(r_lo)
    if action == 'BUY':
        return float(entry) + height
    return float(entry) - height


def regime_brackets(*, candles, entry, action, scenario=None, zones=None,
                    entry_zone=None, trigger=None, tick=0.01, spread=0.,
                    min_distance=0., atr=None, store=None) -> dict:
    """SL + TP1/TP2 from regime. Broker ``profit_level`` = TP2."""
    out = {
        'ready': False, 'reason': 'unready', 'source': 'regime_brackets',
        'stop_distance': None, 'profit_distance': None,
        'stop_level': None, 'profit_level': None,
        'tp1_level': None, 'tp2_level': None,
        'tp1_distance': None, 'tp2_distance': None,
        'swing_hi': None, 'swing_lo': None,
        'regime': None, 'trigger': trigger, 'min_rr': _min_rr(store),
        'zone_sl': False, 'zone_tp': False, 'structural_tp': False,
    }
    action = (action or '').upper()
    if action not in ('BUY', 'SELL'):
        return dict(out, reason='bad_action')
    entry_f = _finite(entry)
    if entry_f is None or entry_f <= 0:
        return dict(out, reason='bad_entry')

    buffer, tick_f, _sp, atr_v = _buf(tick, spread, atr)
    floor = _floor(min_distance, buffer, atr_v)
    min_rr = max(1.5, float(out['min_rr'] or DEFAULT_MIN_RR))
    sc = scenario if isinstance(scenario, dict) else {}
    name = str(sc.get('scenario') or 'UNKNOWN').upper()
    trig = str(trigger or '').lower()
    demand, supply = _zones_pair(zones)
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
    swings = _swing_targets(action, entry_f, highs, lows, floor=floor)
    r_lo, r_hi = _range_levels(sc)

    stop = None
    zone_sl = False
    candidates = []
    reason = 'regime'

    # ----- SL + TP candidate pool per regime (all candidates = real OHLC) -----
    if trig == 'break' or name in ('BREAKOUT_UP', 'BREAKOUT_DOWN'):
        if action == 'BUY':
            br = r_hi
            if br is None and isinstance(supply, dict):
                br = _finite(supply.get('lo'))
            if br is None and highs:
                below = [s for s in highs if float(s['price']) <= entry_f]
                br = float(max(below, key=lambda s: float(s['price']))['price']) if below else None
            if br is not None:
                stop = min(float(br) - buffer, entry_f - floor)
            mm = _measured_move('BUY', entry_f, r_lo, r_hi)
            if mm is not None:
                candidates.append(mm)
            if isinstance(supply, dict):
                candidates.extend([supply.get('lo'), supply.get('hi')])
            candidates.extend(swings)
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
            mm = _measured_move('SELL', entry_f, r_lo, r_hi)
            if mm is not None:
                candidates.append(mm)
            if isinstance(demand, dict):
                candidates.extend([demand.get('hi'), demand.get('lo')])
            candidates.extend(swings)
            reason = 'breakout_sell'

    elif name in ('RANGE', 'COMPRESSION'):
        if action == 'BUY':
            edge = r_lo if r_lo is not None else (
                _finite((demand or {}).get('lo')) if demand else None)
            if edge is not None:
                stop = min(float(edge) - buffer, entry_f - floor)
                zone_sl = True
            if r_lo is not None and r_hi is not None:
                # TP1 = 75% of the real range box (OHLC geometry); TP2 = opposite edge.
                span = float(r_hi) - float(r_lo)
                candidates.append(float(r_lo) + 0.75 * span)
                candidates.append(float(r_hi))
            if isinstance(supply, dict):
                candidates.append(supply.get('lo'))
            candidates.extend(swings)
            reason = 'range_buy_edge'
        else:
            edge = r_hi if r_hi is not None else (
                _finite((supply or {}).get('hi')) if supply else None)
            if edge is not None:
                stop = max(float(edge) + buffer, entry_f + floor)
                zone_sl = True
            if r_lo is not None and r_hi is not None:
                span = float(r_hi) - float(r_lo)
                candidates.append(float(r_hi) - 0.75 * span)
                candidates.append(float(r_lo))
            if isinstance(demand, dict):
                candidates.append(demand.get('hi'))
            candidates.extend(swings)
            reason = 'range_sell_edge'

    elif name in ('RETEST_UP', 'RETEST_DOWN'):
        box = demand if action == 'BUY' else supply
        stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        zone_sl = stop is not None
        # TP = impulse length ≈ prior opposite structure / swings.
        if action == 'BUY' and isinstance(supply, dict):
            candidates.extend([supply.get('lo'), supply.get('hi')])
        if action == 'SELL' and isinstance(demand, dict):
            candidates.extend([demand.get('hi'), demand.get('lo')])
        mm = _measured_move(action, entry_f, r_lo, r_hi)
        if mm is not None:
            candidates.append(mm)
        candidates.extend(swings)
        reason = 'retest_' + action.lower()

    elif name in ('CHOCH_UP', 'CHOCH_DOWN'):
        box = demand if action == 'BUY' else supply
        stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        if stop is None:
            if action == 'BUY' and lows:
                cands = [s for s in lows if float(s['price']) <= entry_f - floor]
                if cands:
                    stop = min(
                        float(max(cands, key=lambda s: float(s['price']))['price']) - buffer,
                        entry_f - floor)
            elif action == 'SELL' and highs:
                cands = [s for s in highs if float(s['price']) >= entry_f + floor]
                if cands:
                    stop = max(
                        float(min(cands, key=lambda s: float(s['price']))['price']) + buffer,
                        entry_f + floor)
        zone_sl = stop is not None and box is not None
        candidates.extend(swings)
        if action == 'BUY' and isinstance(supply, dict):
            candidates.extend([supply.get('lo'), supply.get('hi')])
        if action == 'SELL' and isinstance(demand, dict):
            candidates.extend([demand.get('hi'), demand.get('lo')])
        reason = 'choch_' + action.lower()

    elif name == 'FAKEOUT':
        if action == 'BUY':
            edge = r_lo
            if edge is None and lows:
                edge = float(min(lows, key=lambda s: float(s['price']))['price'])
            if edge is not None:
                stop = min(float(edge) - buffer, entry_f - floor)
            if r_lo is not None and r_hi is not None:
                candidates.append(0.5 * (float(r_lo) + float(r_hi)))
                candidates.append(float(r_hi))
            candidates.extend(swings)
            reason = 'fakeout_fade_buy'
        else:
            edge = r_hi
            if edge is None and highs:
                edge = float(max(highs, key=lambda s: float(s['price']))['price'])
            if edge is not None:
                stop = max(float(edge) + buffer, entry_f + floor)
            if r_lo is not None and r_hi is not None:
                candidates.append(0.5 * (float(r_lo) + float(r_hi)))
                candidates.append(float(r_lo))
            candidates.extend(swings)
            reason = 'fakeout_fade_sell'

    else:
        # TREND / zone pullback — SL behind HL/LH zone; TP prior HH/LL + next.
        box = demand if action == 'BUY' else supply
        stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        zone_sl = stop is not None
        if stop is None:
            if action == 'BUY' and lows:
                cands = [s for s in lows if float(s['price']) <= entry_f - floor]
                if cands:
                    stop = float(max(cands, key=lambda s: float(s['price']))['price']) - buffer
            elif action == 'SELL' and highs:
                cands = [s for s in highs if float(s['price']) >= entry_f + floor]
                if cands:
                    stop = float(min(cands, key=lambda s: float(s['price']))['price']) + buffer
        # Prior HH / next structure for BUY; prior LL for SELL.
        candidates.extend(swings)
        if action == 'BUY' and isinstance(supply, dict):
            candidates.extend([supply.get('lo'), supply.get('hi')])
        if action == 'SELL' and isinstance(demand, dict):
            candidates.extend([demand.get('hi'), demand.get('lo')])
        reason = 'trend_zone_' + action.lower()

    if stop is None:
        # Last resort SL still from a real extreme of recent path — not entry±κ.
        if path:
            if action == 'BUY':
                stop = min(b['low'] for b in path[-12:]) - buffer
                stop = min(stop, entry_f - floor)
            else:
                stop = max(b['high'] for b in path[-12:]) + buffer
                stop = max(stop, entry_f + floor)
        else:
            return dict(out, reason='no_structural_sl', regime=name)

    if action == 'BUY':
        stop = min(float(stop), entry_f - floor)
        stop_d = entry_f - stop
    else:
        stop = max(float(stop), entry_f + floor)
        stop_d = stop - entry_f
    if stop_d < floor:
        return dict(out, reason='regime_sl_below_floor',
                    stop_distance=stop_d, stop_level=float(stop), regime=name)

    tp1, tp2, structural_rr = _pick_tp_ladder(
        action, entry_f, candidates=candidates, risk=stop_d,
        min_rr=min_rr, floor=floor)
    if tp1 is None or tp2 is None:
        return dict(out, reason='no_structural_tp',
                    stop_distance=stop_d, stop_level=float(stop), regime=name)

    if action == 'BUY':
        tp1_d = tp1 - entry_f
        tp2_d = tp2 - entry_f
    else:
        tp1_d = entry_f - tp1
        tp2_d = entry_f - tp2
    if tp2_d < floor:
        return dict(out, reason='regime_tp_below_floor',
                    stop_distance=stop_d, stop_level=float(stop),
                    tp1_level=tp1, tp2_level=tp2, regime=name)

    # Broker uses TP2; scale-out uses TP1.
    return {
        'ready': True,
        'reason': reason,
        'source': 'regime_brackets',
        'stop_distance': float(stop_d),
        'profit_distance': float(tp2_d),
        'stop_level': float(stop),
        'profit_level': float(tp2),
        'tp1_level': float(tp1),
        'tp2_level': float(tp2),
        'tp1_distance': float(tp1_d),
        'tp2_distance': float(tp2_d),
        'swing_hi': float(tp2) if action == 'BUY' else float(stop),
        'swing_lo': float(stop) if action == 'BUY' else float(tp2),
        'kappa': float(tp2_d / stop_d) if stop_d > 1e-12 else min_rr,
        'min_rr': float(min_rr),
        'rr': float(tp2_d / stop_d) if stop_d > 1e-12 else None,
        'rr_tp1': float(tp1_d / stop_d) if stop_d > 1e-12 else None,
        'regime': name,
        'trigger': trig or None,
        'zone_sl': bool(zone_sl),
        'zone_tp': True,
        'structural_tp': True,
        'structural_rr': bool(structural_rr),
        'buffer': float(buffer),
        'atr_used': float(atr_v) if atr_v is not None else None,
        'bos_event': sc.get('event'),
        'bos_bias': sc.get('bias'),
        'scale_out_frac': 0.5,
    }
