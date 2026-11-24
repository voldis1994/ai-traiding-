"""Regime-aware SL + TP from *real* OHLC structure only.

TP1/TP2 ladder + scale-out is **BUY (long) only**:
  • Capital allows one ``profitLevel`` → broker TP = TP2
  • TP1 (near structural) → robot scale-out ~½ + SL→BE

SELL (short) keeps a single structural ``profit_level`` (no TP1 rung,
no scale-out). Still never invent mid ± κ — short TP is a real swing/
zone/range level.

V5 B01/B04/T09: never soft-cap structural SL past invalidation to hit RR,
and never invent HOLD_RR — RR is reported as context only.
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
    """Broker tick/spread buffer only — ATR must not invent SL/TP distance."""
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    try:
        sp = max(float(spread or 0), 0.)
    except (TypeError, ValueError):
        sp = 0.
    b = max(2.0 * tick_f, 2.0 * sp)
    atr_v = _finite(atr)  # audited only; never widens buffer/floor
    return b, tick_f, sp, atr_v


def _floor(min_distance, buffer=None, atr_v=None, atr_frac=None):
    """Capital min stop/profit distance only. Buffer sits on the candle edge;
    ATR never belongs here."""
    try:
        return max(float(min_distance or 0), 0.)
    except (TypeError, ValueError):
        return 0.


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


def _ohlc_stop(action, anchor, *, entry, buffer):
    """SL on real OHLC anchor ± buffer only — never invent entry ± floor."""
    ax = _finite(anchor)
    if ax is None:
        return None
    if action == 'BUY':
        stop = float(ax) - float(buffer)
        return stop if stop < float(entry) else None
    stop = float(ax) + float(buffer)
    return stop if stop > float(entry) else None


def _sl_behind_box(action, entry, box, *, buffer, floor=None):
    """SL behind demand/supply box. ``floor`` is ignored (filter at caller)."""
    if not isinstance(box, dict):
        return None
    if action == 'BUY':
        anchor = _finite(box.get('sl_anchor')) or _finite(box.get('lo'))
        return _ohlc_stop('BUY', anchor, entry=entry, buffer=buffer)
    anchor = _finite(box.get('sl_anchor')) or _finite(box.get('hi'))
    return _ohlc_stop('SELL', anchor, entry=entry, buffer=buffer)


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
    # PR #181 restore: honor Store/seed min_rr (1.0), not a hard 1.5 floor.
    min_rr = max(1.0, float(out['min_rr'] or DEFAULT_MIN_RR))
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

    # Playbook SL helpers: break-candle extreme, HL/LH tags, sweep wick.
    try:
        from . import playbook_entry as _pb
        break_ext = _pb.break_candle_extreme(action, path)
        hl_anchor = _pb.tagged_hl_lh_anchor(action, path)
        sweep_ext = _pb.sweep_wick_extreme(action, path, lookback=6)
    except Exception:
        break_ext = hl_anchor = sweep_ext = None

    # ----- SL + TP candidate pool per regime (all candidates = real OHLC) -----
    if trig == 'break' or name in ('BREAKOUT_UP', 'BREAKOUT_DOWN'):
        if action == 'BUY':
            # Prefer break-candle low, then broken level / range edge.
            br = break_ext
            if br is None:
                br = r_hi
            if br is None and isinstance(supply, dict):
                br = _finite(supply.get('lo'))
            if br is None and highs:
                below = [s for s in highs if float(s['price']) <= entry_f]
                br = float(max(below, key=lambda s: float(s['price']))['price']) if below else None
            if br is not None:
                stop = _ohlc_stop('BUY', br, entry=entry_f, buffer=buffer)
            mm = _measured_move('BUY', entry_f, r_lo, r_hi)
            if mm is not None:
                candidates.append(mm)
            if isinstance(supply, dict):
                candidates.extend([supply.get('lo'), supply.get('hi')])
            candidates.extend(swings)
            reason = 'breakout_buy'
        else:
            br = break_ext
            if br is None:
                br = r_lo
            if br is None and isinstance(demand, dict):
                br = _finite(demand.get('hi'))
            if br is None and lows:
                above = [s for s in lows if float(s['price']) >= entry_f]
                br = float(min(above, key=lambda s: float(s['price']))['price']) if above else None
            if br is not None:
                stop = _ohlc_stop('SELL', br, entry=entry_f, buffer=buffer)
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
                stop = _ohlc_stop('BUY', edge, entry=entry_f, buffer=buffer)
                zone_sl = stop is not None
            if r_lo is not None and r_hi is not None:
                # TP = opposite range edge (real OHLC), not invented mid/%.
                candidates.append(float(r_hi))
            if isinstance(supply, dict):
                candidates.append(supply.get('lo'))
            candidates.extend(swings)
            reason = 'range_buy_edge'
        else:
            edge = r_hi if r_hi is not None else (
                _finite((supply or {}).get('hi')) if supply else None)
            if edge is not None:
                stop = _ohlc_stop('SELL', edge, entry=entry_f, buffer=buffer)
                zone_sl = stop is not None
            if r_lo is not None and r_hi is not None:
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
                cands = [s for s in lows if float(s['price']) < entry_f]
                if cands:
                    stop = _ohlc_stop(
                        'BUY',
                        float(max(cands, key=lambda s: float(s['price']))['price']),
                        entry=entry_f, buffer=buffer)
            elif action == 'SELL' and highs:
                cands = [s for s in highs if float(s['price']) > entry_f]
                if cands:
                    stop = _ohlc_stop(
                        'SELL',
                        float(min(cands, key=lambda s: float(s['price']))['price']),
                        entry=entry_f, buffer=buffer)
        zone_sl = stop is not None and box is not None
        candidates.extend(swings)
        if action == 'BUY' and isinstance(supply, dict):
            candidates.extend([supply.get('lo'), supply.get('hi')])
        if action == 'SELL' and isinstance(demand, dict):
            candidates.extend([demand.get('hi'), demand.get('lo')])
        reason = 'choch_' + action.lower()

    elif name == 'FAKEOUT':
        if action == 'BUY':
            # SL behind deepest sweep — min(range.lo, recent wick).
            edge = r_lo
            if sweep_ext is not None:
                edge = (min(float(edge), float(sweep_ext))
                        if edge is not None else float(sweep_ext))
            if edge is None and lows:
                edge = float(min(lows, key=lambda s: float(s['price']))['price'])
            if edge is not None:
                stop = _ohlc_stop('BUY', edge, entry=entry_f, buffer=buffer)
            if r_lo is not None and r_hi is not None:
                candidates.append(float(r_hi))
            candidates.extend(swings)
            reason = 'fakeout_fade_buy'
        else:
            edge = r_hi
            if sweep_ext is not None:
                edge = (max(float(edge), float(sweep_ext))
                        if edge is not None else float(sweep_ext))
            if edge is None and highs:
                edge = float(max(highs, key=lambda s: float(s['price']))['price'])
            if edge is not None:
                stop = _ohlc_stop('SELL', edge, entry=entry_f, buffer=buffer)
            if r_lo is not None and r_hi is not None:
                candidates.append(float(r_lo))
            candidates.extend(swings)
            reason = 'fakeout_fade_sell'

    else:
        # TREND / zone pullback — SL behind HL/LH tag, then zone, then swing.
        box = demand if action == 'BUY' else supply
        if hl_anchor is not None:
            stop = _ohlc_stop(action, hl_anchor, entry=entry_f, buffer=buffer)
        if stop is None:
            stop = _sl_behind_box(action, entry_f, box, buffer=buffer, floor=floor)
        zone_sl = stop is not None
        if stop is None:
            if action == 'BUY' and lows:
                cands = [s for s in lows if float(s['price']) < entry_f]
                if cands:
                    stop = _ohlc_stop(
                        'BUY',
                        float(max(cands, key=lambda s: float(s['price']))['price']),
                        entry=entry_f, buffer=buffer)
            elif action == 'SELL' and highs:
                cands = [s for s in highs if float(s['price']) > entry_f]
                if cands:
                    stop = _ohlc_stop(
                        'SELL',
                        float(min(cands, key=lambda s: float(s['price']))['price']),
                        entry=entry_f, buffer=buffer)
        # Prior HH / next structure for BUY; prior LL for SELL.
        candidates.extend(swings)
        if action == 'BUY' and isinstance(supply, dict):
            candidates.extend([supply.get('lo'), supply.get('hi')])
        if action == 'SELL' and isinstance(demand, dict):
            candidates.extend([demand.get('hi'), demand.get('lo')])
        reason = 'trend_zone_' + action.lower()

    if stop is None:
        # Last resort SL still from a real extreme of recent path — not entry±κ/ATR.
        if path:
            if action == 'BUY':
                stop = _ohlc_stop(
                    'BUY', min(b['low'] for b in path[-12:]),
                    entry=entry_f, buffer=buffer)
            else:
                stop = _ohlc_stop(
                    'SELL', max(b['high'] for b in path[-12:]),
                    entry=entry_f, buffer=buffer)
        if stop is None:
            return dict(out, reason='no_structural_sl', regime=name)

    # OHLC stop as-is. Broker min only skips levels Capital cannot accept —
    # never invent entry±ATR, never drop a real candle level for ATR noise.
    if action == 'BUY':
        stop_d = entry_f - float(stop)
    else:
        stop_d = float(stop) - entry_f
    if floor > 0 and stop_d + 1e-12 < floor:
        return dict(out, reason='regime_sl_below_broker_min',
                    stop_distance=float(stop_d), stop_level=float(stop),
                    regime=name)

    tp1, tp2, structural_rr = _pick_tp_ladder(
        action, entry_f, candidates=candidates, risk=stop_d,
        min_rr=min_rr, floor=floor)
    if tp1 is None or tp2 is None:
        return dict(out, reason='no_structural_tp',
                    stop_distance=stop_d, stop_level=float(stop), regime=name)

    # V5 B03: TP1/TP2 ladder for both BUY and SELL (structural levels).
    if action == 'BUY':
        tp1_d = tp1 - entry_f
        tp2_d = tp2 - entry_f
    else:
        tp1_d = entry_f - tp1
        tp2_d = entry_f - tp2
    if floor > 0 and tp2_d + 1e-12 < floor:
        return dict(out, reason='regime_tp_below_broker_min',
                    stop_distance=stop_d, stop_level=float(stop),
                    tp1_level=tp1, tp2_level=tp2, regime=name)

    rr_now = (float(tp2_d) / float(stop_d)) if stop_d > 1e-12 else 0.0
    # V5 B01/B04/T09: never soft-cap / shrink structural SL past invalidation
    # to hit RR, and never invent HOLD_RR as an OPEN veto. RR is context.
    # V5 B03: TP1/TP2 ladder + scale-out for both sides.
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
        'rr_capped': False,
        'kappa': float(rr_now) if rr_now > 0 else min_rr,
        'min_rr': float(min_rr),
        'rr': float(rr_now),
        'rr_tp1': (float(tp1_d / stop_d) if stop_d > 1e-12 else None),
        'regime': name,
        'trigger': trig or None,
        'zone_sl': bool(zone_sl),
        'zone_tp': True,
        'structural_tp': True,
        'structural_rr': bool(structural_rr or rr_now + 1e-12 >= min_rr),
        'buffer': float(buffer),
        'atr_used': float(atr_v) if atr_v is not None else None,
        'bos_event': sc.get('event'),
        'bos_bias': sc.get('bias'),
        'scale_out_frac': 0.5,
        'tp_ladder': True,
    }
