"""Adaptive re-entry confirmation in the same neural OPEN path.

Modules (structure/MTF/day_book) can amplify BUY/SELL. Before a new OPEN:

1. Raw neural edge must be strong enough (scores_raw, not nudged).
2. Same-direction re-entry after a close needs new tape proof (ATR / structure).
3. Strong/medium pass without eternal M1 colour-confirm HOLD — candle *quality*
   is gated by ``ohlc_regime_gate`` (chop/doji/impulse/chaos) instead.
4. Outcomes of fast / same-dir re-entries nudge the required raw floors.
"""
from __future__ import annotations

import math

STORE_KEY = 'reentry_confirm_v1'

DEFAULTS = {
    'enabled': 1,
    # Raw score_edge floors (winner − max(other, HOLD)) on scores_raw.
    # Audit-seeded slightly higher so weak same-bar re-opens need more edge.
    'raw_strong': 0.20,
    'raw_medium': 0.12,
    'raw_weak': 0.07,
    # Same-dir re-entry: need this fraction of ATR14 move since last close mid.
    # Audit: rapid same-dir re-open after loss cost −£14.24 / 176 GOLD trades.
    'same_dir_atr_frac': 0.55,
    # Medium tier: consecutive completed M1 closes in action direction.
    # 2 = demand a fresh micro-move (was 1 — too easy to spam re-entry).
    'medium_m1_streak': 2,
    # Soft learn rails for floors.
    'raw_strong_min': 0.12,
    'raw_strong_max': 0.28,
    'raw_medium_min': 0.06,
    'raw_medium_max': 0.18,
}


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _clip(key, value):
    lo = DEFAULTS.get(f'{key}_min')
    hi = DEFAULTS.get(f'{key}_max')
    n = _finite(value)
    if n is None:
        return DEFAULTS[key]
    if lo is not None:
        n = max(float(lo), n)
    if hi is not None:
        n = min(float(hi), n)
    return float(n)


def load_params(store=None) -> dict:
    out = dict(DEFAULTS)
    if store is None:
        return out
    try:
        from .self_params import get as sp_get
        out['enabled'] = int(sp_get(store, 'reentry_confirm_enabled', 0))
        out['raw_strong'] = float(sp_get(store, 'reentry_raw_strong', DEFAULTS['raw_strong']))
        out['raw_medium'] = float(sp_get(store, 'reentry_raw_medium', DEFAULTS['raw_medium']))
        out['raw_weak'] = float(sp_get(store, 'reentry_raw_weak', DEFAULTS['raw_weak']))
        out['same_dir_atr_frac'] = float(
            sp_get(store, 'reentry_same_dir_atr_frac', DEFAULTS['same_dir_atr_frac']))
        out['medium_m1_streak'] = int(
            sp_get(store, 'reentry_medium_m1_streak', DEFAULTS['medium_m1_streak']))
    except Exception:
        pass
    bag = store.get(STORE_KEY) if store is not None else None
    if isinstance(bag, dict):
        for k in ('raw_strong', 'raw_medium', 'raw_weak', 'same_dir_atr_frac'):
            if bag.get(k) is not None:
                out[k] = float(bag[k])
        if bag.get('medium_m1_streak') is not None:
            out['medium_m1_streak'] = int(bag['medium_m1_streak'])
        if bag.get('enabled') is not None:
            out['enabled'] = int(bag['enabled'])
    out['raw_strong'] = _clip('raw_strong', out['raw_strong'])
    out['raw_medium'] = _clip('raw_medium', out['raw_medium'])
    # Weak floor stays ≤ medium.
    weak = _finite(out.get('raw_weak'))
    out['raw_weak'] = float(DEFAULTS['raw_weak'] if weak is None else weak)
    out['raw_weak'] = min(out['raw_weak'], out['raw_medium'])
    out['same_dir_atr_frac'] = max(0.15, min(1.0, float(out['same_dir_atr_frac'])))
    out['medium_m1_streak'] = max(1, min(4, int(out['medium_m1_streak'])))
    return out


def save_params(store, params: dict) -> dict:
    clean = dict(DEFAULTS)
    for k, v in (params or {}).items():
        if k in DEFAULTS:
            clean[k] = v
    clean['raw_strong'] = _clip('raw_strong', clean['raw_strong'])
    clean['raw_medium'] = _clip('raw_medium', clean['raw_medium'])
    clean['raw_weak'] = min(float(clean.get('raw_weak') or DEFAULTS['raw_weak']),
                            clean['raw_medium'])
    clean['same_dir_atr_frac'] = max(0.15, min(1.0, float(clean['same_dir_atr_frac'])))
    clean['medium_m1_streak'] = max(1, min(4, int(clean['medium_m1_streak'])))
    clean['enabled'] = int(clean.get('enabled') or 0)
    if store is not None:
        store.set(STORE_KEY, {
            'enabled': clean['enabled'],
            'raw_strong': clean['raw_strong'],
            'raw_medium': clean['raw_medium'],
            'raw_weak': clean['raw_weak'],
            'same_dir_atr_frac': clean['same_dir_atr_frac'],
            'medium_m1_streak': clean['medium_m1_streak'],
        })
    return clean


def raw_score_edge(scores_raw, action=None) -> float | None:
    """Neural-only edge — ignores structure/MTF/day_book nudges."""
    if not isinstance(scores_raw, dict):
        return None
    try:
        buy = float(scores_raw.get('BUY') or 0)
        sell = float(scores_raw.get('SELL') or 0)
        hold = float(scores_raw.get('HOLD') or 0)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (buy, sell, hold)):
        return None
    direction = action if action in ('BUY', 'SELL') else ('BUY' if buy >= sell else 'SELL')
    strength = buy if direction == 'BUY' else sell
    other = sell if direction == 'BUY' else buy
    return float(strength - max(other, hold))


def raw_direction(scores_raw) -> str | None:
    if not isinstance(scores_raw, dict):
        return None
    try:
        buy = float(scores_raw.get('BUY') or 0)
        sell = float(scores_raw.get('SELL') or 0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(buy) or not math.isfinite(sell):
        return None
    return 'BUY' if buy >= sell else 'SELL'


def tier_for_edge(edge, params=None) -> str:
    p = params or DEFAULTS
    e = _finite(edge)
    if e is None:
        return 'weak'
    if e >= float(p['raw_strong']):
        return 'strong'
    if e >= float(p['raw_medium']):
        return 'medium'
    return 'weak'


def _atr14(mtf_analysis) -> float | None:
    if not isinstance(mtf_analysis, dict):
        return None
    atr = _finite(mtf_analysis.get('stop_hint_atr14'))
    if atr is not None and atr > 0:
        return atr
    m5 = ((mtf_analysis.get('tf') or {}).get('m5') or {})
    atr = _finite(m5.get('atr14')) or _finite((m5.get('atr') or {}).get(14))
    if atr is not None and atr > 0:
        return atr
    return None


def _structure_break(mtf_analysis) -> str | None:
    if not isinstance(mtf_analysis, dict):
        return None
    br = mtf_analysis.get('structure_break')
    if br in ('UP', 'DOWN'):
        return br
    m1 = ((mtf_analysis.get('tf') or {}).get('m1') or {})
    br = m1.get('structure_break')
    return br if br in ('UP', 'DOWN') else None


def m1_same_dir_streak(candles, action, *, need=2) -> dict:
    """Count trailing completed M1 closes in BUY/SELL direction."""
    need = max(1, int(need))
    rows = list(candles or [])
    if len(rows) < need:
        return {'ok': False, 'streak': 0, 'reason': 'need_more_m1'}
    streak = 0
    for row in reversed(rows[-12:]):
        o = _finite(row.get('open'))
        c = _finite(row.get('close'))
        if o is None or c is None:
            break
        if action == 'BUY' and c > o:
            streak += 1
        elif action == 'SELL' and c < o:
            streak += 1
        else:
            break
        if streak >= need:
            return {'ok': True, 'streak': streak, 'reason': 'm1_streak_ok'}
    return {'ok': streak >= need, 'streak': streak,
            'reason': 'm1_streak_ok' if streak >= need else 'm1_streak_short'}


def ohlc_strong_agrees(action, mtf_analysis=None, *, min_conf: float = 0.50) -> dict:
    """True when OHLC/MTF stack strongly prefers ``action`` — skip 1m confirm / soft cross.

    Lone micro ``structure_break`` is not enough (BUY bounce in a dump). Break
    must agree with preferred/mode, or carry high confidence without opposite side.
    """
    if action not in ('BUY', 'SELL') or not isinstance(mtf_analysis, dict):
        return {'ok': False, 'reason': 'no_mtf'}
    if not mtf_analysis.get('ready'):
        return {'ok': False, 'reason': 'mtf_not_ready'}
    pref = str(mtf_analysis.get('preferred') or '').upper()
    mode = str(mtf_analysis.get('mode') or '').upper()
    try:
        conf = float(mtf_analysis.get('confidence') or 0)
    except (TypeError, ValueError):
        conf = 0.0
    opposite = 'SELL' if action == 'BUY' else 'BUY'
    br = _structure_break(mtf_analysis)
    if pref == action and conf >= float(min_conf):
        return {'ok': True, 'reason': 'mtf_preferred_strong', 'confidence': conf, 'mode': mode}
    if mode == action and conf >= float(min_conf):
        return {'ok': True, 'reason': 'mtf_mode_strong', 'confidence': conf, 'mode': mode}
    if (action == 'BUY' and br == 'UP') or (action == 'SELL' and br == 'DOWN'):
        # Require MTF same-side or high conf without opposite preferred.
        if pref == opposite or mode == opposite:
            return {'ok': False, 'reason': 'break_vs_mtf_opposite',
                    'confidence': conf, 'preferred': pref, 'mode': mode}
        if pref == action or mode == action:
            return {'ok': True, 'reason': 'structure_break_strong',
                    'confidence': conf, 'mode': mode}
        if conf >= max(float(min_conf), 0.70):
            return {'ok': True, 'reason': 'structure_break_strong',
                    'confidence': conf, 'mode': mode}
        return {'ok': False, 'reason': 'micro_break_not_strong',
                'confidence': conf, 'preferred': pref, 'mode': mode}
    return {'ok': False, 'reason': 'ohlc_not_strong', 'confidence': conf, 'preferred': pref}


def price_confirms(action, *, mtf_analysis=None, market_context=None,
                   candles=None) -> dict:
    """Tape agrees with action: structure break, live trend, or last M1 close."""
    if action not in ('BUY', 'SELL'):
        return {'ok': False, 'reason': 'bad_action'}
    br = _structure_break(mtf_analysis)
    if (action == 'BUY' and br == 'UP') or (action == 'SELL' and br == 'DOWN'):
        return {'ok': True, 'reason': 'structure_break'}
    trend = None
    if isinstance(market_context, dict):
        trend = str(market_context.get('trend') or '').upper()
    if (action == 'BUY' and trend == 'UP') or (action == 'SELL' and trend == 'DOWN'):
        return {'ok': True, 'reason': 'live_trend'}
    returns = (market_context or {}).get('returns_bps') if isinstance(market_context, dict) else None
    if isinstance(returns, dict):
        r1 = _finite(returns.get(1) if 1 in returns else returns.get('1'))
        r5 = _finite(returns.get(5) if 5 in returns else returns.get('5'))
        if r1 is not None and r5 is not None:
            if action == 'BUY' and r1 > 0 and r5 > 0:
                return {'ok': True, 'reason': 'returns_up'}
            if action == 'SELL' and r1 < 0 and r5 < 0:
                return {'ok': True, 'reason': 'returns_down'}
    streak = m1_same_dir_streak(candles, action, need=1)
    if streak.get('ok'):
        return {'ok': True, 'reason': 'last_m1_close'}
    # MTF preferred agrees (incl. HOLD mode when preferred still points a side).
    pref = str((mtf_analysis or {}).get('preferred') or '').upper()
    mode = str((mtf_analysis or {}).get('mode') or '').upper()
    if pref == action and mode in ('BUY', 'SELL', 'SHIFT', 'HOLD'):
        return {'ok': True, 'reason': 'mtf_preferred'}
    strong = ohlc_strong_agrees(action, mtf_analysis)
    if strong.get('ok'):
        return {'ok': True, 'reason': strong.get('reason') or 'ohlc_strong'}
    return {'ok': False, 'reason': 'no_price_confirm'}


def same_dir_new_move(action, *, last_outcome=None, mtf_analysis=None,
                      market_context=None, candles=None, params=None) -> dict:
    """After a close in the same direction, demand a fresh ATR/structure move."""
    p = params or DEFAULTS
    last = last_outcome if isinstance(last_outcome, dict) else {}
    last_dir = last.get('direction')
    if last_dir not in ('BUY', 'SELL') or last_dir != action:
        return {'needed': False, 'ok': True, 'reason': 'not_same_dir_reentry'}
    br = _structure_break(mtf_analysis)
    if (action == 'BUY' and br == 'UP') or (action == 'SELL' and br == 'DOWN'):
        return {'needed': True, 'ok': True, 'reason': 'new_structure_break'}
    atr = _atr14(mtf_analysis)
    close_mid = _finite(last.get('exit_mid') or last.get('exit') or last.get('exit_level'))
    live_mid = None
    if isinstance(market_context, dict):
        live_mid = _finite(market_context.get('close') or market_context.get('mid'))
    if live_mid is None and candles:
        live_mid = _finite((candles[-1] or {}).get('close'))
    frac = float(p.get('same_dir_atr_frac') or DEFAULTS['same_dir_atr_frac'])
    if atr is not None and atr > 0 and close_mid is not None and live_mid is not None:
        move = live_mid - close_mid
        need = frac * atr
        if action == 'BUY' and move >= need:
            return {'needed': True, 'ok': True, 'reason': 'atr_continuation',
                    'move': move, 'need': need}
        if action == 'SELL' and (-move) >= need:
            return {'needed': True, 'ok': True, 'reason': 'atr_continuation',
                    'move': move, 'need': need}
        return {'needed': True, 'ok': False, 'reason': 'need_new_atr_move',
                'move': move, 'need': need, 'atr': atr}
    # Fallback: two M1 closes same way = new micro-move.
    streak = m1_same_dir_streak(
        candles, action, need=max(2, int(p.get('medium_m1_streak') or 2)))
    if streak.get('ok'):
        return {'needed': True, 'ok': True, 'reason': 'm1_streak_as_new_move',
                'streak': streak.get('streak')}
    return {'needed': True, 'ok': False, 'reason': 'need_new_move_proof'}


def evaluate(*, action, scores_raw=None, last_outcome=None, mtf_analysis=None,
             market_context=None, candles=None, store=None) -> dict:
    """Gate a proposed BUY/SELL. HOLD proposals pass through unchanged."""
    params = load_params(store)
    meta = {
        'enabled': bool(params.get('enabled')),
        'allowed': True,
        'tier': None,
        'raw_edge': None,
        'reason': 'pass',
        'params': {
            'raw_strong': params['raw_strong'],
            'raw_medium': params['raw_medium'],
            'raw_weak': params['raw_weak'],
        },
    }
    if not params.get('enabled'):
        meta['reason'] = 'disabled'
        return meta
    if action not in ('BUY', 'SELL'):
        meta['reason'] = 'not_directional'
        return meta
    raw_dir = raw_direction(scores_raw)
    edge = raw_score_edge(scores_raw, action=action)
    meta['raw_edge'] = edge
    meta['raw_direction'] = raw_dir
    if edge is None:
        return dict(meta, allowed=False, reason='no_scores_raw', tier='weak')
    ohlc = ohlc_strong_agrees(action, mtf_analysis)
    meta['ohlc_strong'] = ohlc
    # Modules nudged vs raw neuron "cross": if OHLC/MTF strongly agrees with the
    # nudged side, allow — do not freeze on raw_disagrees forever.
    if raw_dir in ('BUY', 'SELL') and raw_dir != action:
        if ohlc.get('ok'):
            meta['tier'] = 'strong' if (edge or 0) >= float(params['raw_strong']) else 'medium'
            return dict(meta, allowed=True, reason='ohlc_overrides_raw_cross',
                        fast_entry=True)
        return dict(meta, allowed=False, reason='raw_disagrees_nudged',
                    tier='weak')
    if edge < float(params['raw_weak']):
        # Still allow when OHLC stack is clearly with us (strong structural tape).
        if ohlc.get('ok') and float(ohlc.get('confidence') or 0) >= 0.60:
            return dict(meta, allowed=True, reason='ohlc_strong_weak_raw',
                        tier='medium', fast_entry=True)
        return dict(meta, allowed=False, reason='raw_edge_too_weak', tier='weak')
    tier = tier_for_edge(edge, params)
    meta['tier'] = tier
    if tier == 'weak':
        if ohlc.get('ok') and float(ohlc.get('confidence') or 0) >= 0.60:
            return dict(meta, allowed=True, reason='ohlc_strong_weak_raw',
                        tier='medium', fast_entry=True)
        return dict(meta, allowed=False, reason='weak_raw_hold')

    same = same_dir_new_move(
        action, last_outcome=last_outcome, mtf_analysis=mtf_analysis,
        market_context=market_context, candles=candles, params=params)
    meta['same_dir'] = same
    if same.get('needed') and not same.get('ok'):
        return dict(meta, allowed=False, reason=same.get('reason') or 'need_new_move')

    # Structure memory: same-dir re-entry needs new BOS/CHoCH/retest / left range.
    # Also refuse fresh OPEN that chases a vertical impulse into the extreme
    # (e.g. BUY near local high after a spike — structure memory alone misses that).
    try:
        from . import structure_memory
        atr_v = _atr14(mtf_analysis)
        chase = structure_memory.extension_chase(
            action, candles=candles, market_context=market_context, atr=atr_v)
        meta['extension_chase'] = chase
        if chase.get('needed') and not chase.get('ok'):
            return dict(meta, allowed=False,
                        reason=chase.get('reason') or 'chase_extension')
        struct_gate = structure_memory.gate_reentry(
            action=action, last_outcome=last_outcome, candles=candles,
            market_context=market_context, atr=atr_v)
        meta['structure_memory'] = struct_gate
        if struct_gate.get('needed') and not struct_gate.get('ok'):
            return dict(meta, allowed=False,
                        reason=struct_gate.get('reason') or 'same_range_hold')
    except Exception as exc:
        meta['structure_memory'] = {'needed': False, 'ok': True,
                                    'reason': 'structure_memory_error',
                                    'error': type(exc).__name__}

    confirm = price_confirms(
        action, mtf_analysis=mtf_analysis, market_context=market_context,
        candles=candles)
    meta['price_confirm'] = confirm

    # Strong / medium: do NOT eternal-HOLD waiting for M1 colour confirm.
    # Candle *quality* is gated by ``ohlc_regime_gate`` (chop/doji/impulse/…).
    # Here we only keep raw edge + same-dir / chase / structure-memory proofs.
    if tier == 'strong':
        if confirm.get('ok') or ohlc.get('ok'):
            return dict(meta, allowed=True,
                        reason='strong_immediate' if confirm.get('ok') else 'strong_ohlc_skip_m1',
                        fast_entry=True)
        return dict(meta, allowed=True, reason='strong_no_m1_wait',
                    fast_entry=True)

    if ohlc.get('ok') or confirm.get('ok'):
        return dict(meta, allowed=True, reason='medium_tape_ok',
                    fast_entry=True)
    return dict(meta, allowed=True, reason='medium_no_m1_wait',
                fast_entry=True)


def open_meta_from_gate(gate: dict, *, action) -> dict:
    """Persist on entry_brain so close can teach re-entry floors."""
    if not isinstance(gate, dict):
        return {}
    return {
        'action': action,
        'tier': gate.get('tier'),
        'raw_edge': gate.get('raw_edge'),
        'fast_entry': bool(gate.get('fast_entry')),
        'same_dir': bool((gate.get('same_dir') or {}).get('needed')),
        'reason': gate.get('reason'),
    }


def observe_close(store, *, reentry_meta=None, broker_pnl=None, spread=0.0):
    """Adapt raw floors from fast / same-dir re-entry outcomes (after costs)."""
    meta = reentry_meta if isinstance(reentry_meta, dict) else {}
    if not meta:
        return {'adapted': False, 'reason': 'no_reentry_meta'}
    pnl = _finite(broker_pnl)
    if pnl is None:
        return {'adapted': False, 'reason': 'no_pnl'}
    cost = abs(_finite(spread) or 0.0)
    # Net after one RT-ish cost proxy (spread already in account £ often).
    useful = pnl > max(0.02, 0.25 * cost)
    hurt = pnl < -max(0.02, 0.25 * cost)
    if not useful and not hurt:
        return {'adapted': False, 'reason': 'scratch_band'}
    params = load_params(store)
    fast = bool(meta.get('fast_entry'))
    same = bool(meta.get('same_dir'))
    tier = str(meta.get('tier') or '')
    if not (fast or same or tier in ('strong', 'medium')):
        return {'adapted': False, 'reason': 'not_reentry_case'}
    strong = float(params['raw_strong'])
    medium = float(params['raw_medium'])
    note = []
    if hurt and (fast or tier == 'strong'):
        strong = _clip('raw_strong', strong + 0.01)
        note.append('raise_strong_after_fast_loss')
    elif useful and fast and tier == 'strong':
        strong = _clip('raw_strong', strong - 0.005)
        note.append('ease_strong_after_fast_win')
    if hurt and same:
        medium = _clip('raw_medium', medium + 0.01)
        note.append('raise_medium_after_same_dir_loss')
    elif useful and same and tier == 'medium':
        medium = _clip('raw_medium', medium - 0.005)
        note.append('ease_medium_after_same_dir_win')
    if not note:
        return {'adapted': False, 'reason': 'no_rule_hit'}
    # Keep strong > medium.
    if strong <= medium:
        strong = _clip('raw_strong', medium + 0.04)
    params['raw_strong'] = strong
    params['raw_medium'] = medium
    params['raw_weak'] = min(float(params['raw_weak']), medium)
    save_params(store, params)
    hist = list((store.get(STORE_KEY + ':hist') or [])) if store is not None else []
    hist.append({
        'pnl': pnl, 'useful': useful, 'tier': tier, 'fast': fast, 'same': same,
        'notes': note, 'raw_strong': strong, 'raw_medium': medium,
    })
    if store is not None:
        store.set(STORE_KEY + ':hist', hist[-40:])
        store.event('reentry', 'Adapted re-entry raw floors from outcome',
                    {'notes': note, 'raw_strong': strong, 'raw_medium': medium,
                     'pnl': pnl})
    return {'adapted': True, 'notes': note, 'params': params}
