"""One closed LIVE cycle: perceive → decide → risk → settle → learn.

Neural scores, OHLC brackets, and £ outcomes used to drift apart (argmax in
one place, SL ratchet in another, lot from mandate). This module is the single
coordination point so OPEN is one machine state — not three competing policies.

Lot size stays the operator mandate. Direction stays neural. Risk width
(κ / sl_scale dampening) and whether a weak edge may OPEN are coordinated here
from the same confidence + recent £ streak.
"""
from __future__ import annotations

import math

STORE_KEY = 'unified_cycle_v1'

# Soft bounds on cycle aggression — evolved from £, not a per-trade hardcode tip.
AGGRESSION_MIN = 0.70
AGGRESSION_MAX = 1.15


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _clip(v, lo, hi):
    return float(max(lo, min(hi, float(v))))


def default_state():
    return {
        'aggression': 1.0,
        'loss_streak': 0,
        'win_streak': 0,
        'last_pnl': None,
        'last_action': None,
        'opens': 0,
        'settlements': 0,
        'version': 1,
    }


def load_state(store) -> dict:
    raw = store.get(STORE_KEY) if store is not None else None
    out = default_state()
    if isinstance(raw, dict):
        for k in out:
            if k in raw:
                out[k] = raw[k]
        out['aggression'] = _clip(
            _finite(out.get('aggression')) or 1.0, AGGRESSION_MIN, AGGRESSION_MAX)
        out['loss_streak'] = max(0, int(out.get('loss_streak') or 0))
        out['win_streak'] = max(0, int(out.get('win_streak') or 0))
    return out


def save_state(store, state: dict) -> dict:
    clean = default_state()
    if isinstance(state, dict):
        clean.update({k: state.get(k, clean[k]) for k in clean})
    clean['aggression'] = _clip(
        _finite(clean.get('aggression')) or 1.0, AGGRESSION_MIN, AGGRESSION_MAX)
    if store is not None:
        store.set(STORE_KEY, clean)
    return clean


def score_margin(scores) -> float | None:
    if not isinstance(scores, dict):
        return None
    try:
        buy, sell, hold = float(scores['BUY']), float(scores['SELL']), float(scores['HOLD'])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (buy, sell, hold)):
        return None
    direction = 'BUY' if buy >= sell else 'SELL'
    strength = buy if direction == 'BUY' else sell
    return float(strength - max(sell if direction == 'BUY' else buy, hold))


def perceive_decision(scores, evaluation=None) -> dict:
    """Fold neural scores (+ optional policy evaluation) into one decision view."""
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    action = evaluation.get('action')
    if action not in ('BUY', 'SELL', 'HOLD'):
        if isinstance(scores, dict) and set(scores) >= {'BUY', 'SELL', 'HOLD'}:
            action = max(('BUY', 'SELL', 'HOLD'), key=lambda a: float(scores.get(a, 0) or 0))
        else:
            action = 'HOLD'
    margin = _finite(evaluation.get('score_edge'))
    if margin is None:
        margin = score_margin(scores)
    conf = None
    if isinstance(scores, dict):
        try:
            conf = max(float(scores[k]) for k in ('BUY', 'SELL', 'HOLD'))
        except (TypeError, ValueError, KeyError):
            conf = None
    return {
        'action': action,
        'margin': float(margin) if margin is not None else 0.0,
        'confidence': float(conf) if conf is not None else 0.0,
        'reason': evaluation.get('reason'),
        'proposal': evaluation.get('proposal') or action,
    }


def min_open_margin(state: dict) -> float:
    """After losses, demand a clearer neural edge before the next OPEN."""
    losses = int(state.get('loss_streak') or 0)
    # Data-driven floor from streak length — no frozen 0.06 tip; scales with pain.
    return float(min(0.20, 0.02 * max(0, losses)))


# Back-compat alias used while the module settled.
_min_open_margin = min_open_margin


def edge_allows_open(scores, evaluation=None, cycle_state=None) -> dict:
    """Public gate: after £ losses, weak neural margin becomes HOLD."""
    state = dict(cycle_state or default_state())
    decision = perceive_decision(scores, evaluation)
    need = min_open_margin(state)
    act = decision['action']
    if act in ('BUY', 'SELL') and float(decision['margin']) < need:
        return {
            'allowed': False,
            'action': 'HOLD',
            'reason': (
                f'Unified cycle: edge {decision["margin"]:.3f} < required {need:.3f} '
                f'after {int(state.get("loss_streak") or 0)} loss streak'),
            'decision': decision,
            'min_margin': need,
            'aggression': _clip(
                _finite(state.get('aggression')) or 1.0, AGGRESSION_MIN, AGGRESSION_MAX),
        }
    return {
        'allowed': True,
        'action': act,
        'reason': 'Unified cycle edge ok',
        'decision': decision,
        'min_margin': need,
        'aggression': _clip(
            _finite(state.get('aggression')) or 1.0, AGGRESSION_MIN, AGGRESSION_MAX),
    }


def compose_open(*, action, scores, evaluation, zone, learned, cycle_state,
                 base_sl, max_tp=None) -> dict:
    """One OPEN package: may HOLD if cycle says edge is too weak after losses.

    Returns action, kappa, sl_scale, base_sl, max_tp, cycle metadata.
    Lot is NOT chosen here — mandate remains operator size.
    """
    state = dict(cycle_state or default_state())
    decision = perceive_decision(scores, evaluation)
    act = action if action in ('BUY', 'SELL', 'HOLD') else decision['action']
    margin = float(decision['margin'])
    aggression = _clip(_finite(state.get('aggression')) or 1.0,
                       AGGRESSION_MIN, AGGRESSION_MAX)
    need = min_open_margin(state)
    if act in ('BUY', 'SELL') and margin < need:
        return {
            'action': 'HOLD',
            'kappa': None,
            'sl_scale': None,
            'base_sl': base_sl,
            'max_tp': max_tp if max_tp is not None else (zone or {}).get('max_tp'),
            'blocked': True,
            'reason': (
                f'Unified cycle: edge {margin:.3f} < required {need:.3f} '
                f'after {int(state.get("loss_streak") or 0)} loss streak'),
            'decision': decision,
            'aggression': aggression,
            'min_margin': need,
        }
    zone = zone if isinstance(zone, dict) else {}
    learned = learned if isinstance(learned, dict) else {}
    kappa = _finite(zone.get('kappa')) or 1.0
    # Blend zone κ with learned multiplier, then damp/raise with cycle aggression.
    k_mult = _finite(learned.get('kappa')) or 1.0
    s_mult = max(1.0, _finite(learned.get('sl_scale')) or 1.0)
    kappa = float(kappa) * float(k_mult) * float(aggression)
    # Weak edge → keep SL closer to zone (aggression already ≤1 after losses).
    # Strong edge → full learned width. Never invent lot size.
    edge_scale = _clip(0.85 + max(0.0, margin) * 2.0, 0.85, 1.10)
    sl_scale = max(1.0, float(s_mult) * float(aggression) * float(edge_scale))
    tp = max_tp if max_tp is not None else zone.get('max_tp')
    base = float(base_sl or zone.get('range_distance') or 0)
    if base > 0 and tp and float(tp) > 0:
        room = float(tp) / (base * sl_scale)
        if kappa > room:
            kappa = max(0.5, room)
    return {
        'action': act if act in ('BUY', 'SELL') else decision['action'],
        'kappa': float(kappa),
        'sl_scale': float(sl_scale),
        'base_sl': base,
        'max_tp': float(tp) if tp is not None else None,
        'blocked': False,
        'reason': 'Unified cycle OPEN',
        'decision': decision,
        'aggression': aggression,
        'min_margin': need,
        'edge_scale': float(edge_scale),
    }


def settle(store, *, pnl, action=None, exit_reason=None) -> dict:
    """Fold broker £ into cycle aggression / streaks — one feedback bus."""
    state = load_state(store)
    p = _finite(pnl)
    state['settlements'] = int(state.get('settlements') or 0) + 1
    state['last_pnl'] = p
    state['last_action'] = action
    if p is None:
        return save_state(store, state)
    if p < 0:
        state['loss_streak'] = int(state.get('loss_streak') or 0) + 1
        state['win_streak'] = 0
        # Cool aggression after losses so the next OPEN is tighter / pickier.
        step = 0.96 if state['loss_streak'] >= 2 else 0.98
        state['aggression'] = _clip(
            float(state.get('aggression') or 1.0) * step,
            AGGRESSION_MIN, AGGRESSION_MAX)
    elif p > 0:
        state['win_streak'] = int(state.get('win_streak') or 0) + 1
        state['loss_streak'] = 0
        step = 1.02 if state['win_streak'] >= 2 else 1.01
        state['aggression'] = _clip(
            float(state.get('aggression') or 1.0) * step,
            AGGRESSION_MIN, AGGRESSION_MAX)
    else:
        # Flat £ — mild cool-off, do not treat as a win.
        state['aggression'] = _clip(
            float(state.get('aggression') or 1.0) * 0.995,
            AGGRESSION_MIN, AGGRESSION_MAX)
    return save_state(store, state)


def note_open(store, *, action=None) -> dict:
    state = load_state(store)
    state['opens'] = int(state.get('opens') or 0) + 1
    state['last_action'] = action
    return save_state(store, state)
