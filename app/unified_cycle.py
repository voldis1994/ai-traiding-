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

# LIVE entry edge in score_edge units (shared with policy MIN_EDGE).
# Default seed 0.06; the machine may recalibrate from holdout history + spread
# inside soft rails. Only whole hundredths: 0.06 → 0.07 (never 0.065 / 0.086).
# £ settle / ΔW do NOT move this gate.
ENTRY_EDGE_DEFAULT = 0.06
ENTRY_EDGE_MIN = 0.03
ENTRY_EDGE_MAX = 0.12
ENTRY_EDGE_STEP = 0.01
CALIB_META_KEY = 'entry_edge_calibration_v1'


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _safe_int(v, default=0) -> int:
    """Non-negative int from Store junk (NaN / str / None → default)."""
    n = _finite(v)
    if n is None:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return int(default)
    if not math.isfinite(float(n)):
        return int(default)
    return max(0, int(n))


def _clip(v, lo, hi):
    return float(max(lo, min(hi, float(v))))


def quantize_entry_edge(v) -> float:
    """Snap to 0.01 steps inside rails — 0.06, 0.07, … never 0.065."""
    n = _finite(v)
    if n is None:
        n = ENTRY_EDGE_DEFAULT
    step = float(ENTRY_EDGE_STEP)
    # Round to nearest whole hundredth (avoid float dust like 0.069999).
    snapped = round(round(n / step) * step, 2)
    return _clip(snapped, ENTRY_EDGE_MIN, ENTRY_EDGE_MAX)


def _score_value(scores, key) -> float | None:
    if not isinstance(scores, dict):
        return None
    return _finite(scores.get(key))


def default_state():
    return {
        'aggression': 1.0,
        'entry_edge_floor': ENTRY_EDGE_DEFAULT,
        'loss_streak': 0,
        'win_streak': 0,
        'last_pnl': None,
        'last_action': None,
        'opens': 0,
        'settlements': 0,
        'version': 2,
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
        out['entry_edge_floor'] = quantize_entry_edge(
            out.get('entry_edge_floor') or ENTRY_EDGE_DEFAULT)
        out['loss_streak'] = _safe_int(out.get('loss_streak'), 0)
        out['win_streak'] = _safe_int(out.get('win_streak'), 0)
        out['opens'] = _safe_int(out.get('opens'), 0)
        out['settlements'] = _safe_int(out.get('settlements'), 0)
        out['version'] = _safe_int(out.get('version'), 2) or 2
        out['last_pnl'] = _finite(out.get('last_pnl'))
        if out.get('last_action') not in ('BUY', 'SELL', 'HOLD', None):
            out['last_action'] = None
    return out


def save_state(store, state: dict) -> dict:
    clean = default_state()
    if isinstance(state, dict):
        clean.update({k: state.get(k, clean[k]) for k in clean})
    clean['aggression'] = _clip(
        _finite(clean.get('aggression')) or 1.0, AGGRESSION_MIN, AGGRESSION_MAX)
    clean['entry_edge_floor'] = quantize_entry_edge(
        clean.get('entry_edge_floor') or ENTRY_EDGE_DEFAULT)
    clean['loss_streak'] = _safe_int(clean.get('loss_streak'), 0)
    clean['win_streak'] = _safe_int(clean.get('win_streak'), 0)
    clean['opens'] = _safe_int(clean.get('opens'), 0)
    clean['settlements'] = _safe_int(clean.get('settlements'), 0)
    clean['version'] = _safe_int(clean.get('version'), 2) or 2
    clean['last_pnl'] = _finite(clean.get('last_pnl'))
    if clean.get('last_action') not in ('BUY', 'SELL', 'HOLD', None):
        clean['last_action'] = None
    if store is not None:
        store.set(STORE_KEY, clean)
    return clean


def entry_edge_floor(store=None, cycle_state=None) -> float:
    """LIVE edge shared by cycle gate + policy MIN_EDGE (calibrated or default)."""
    state = cycle_state if isinstance(cycle_state, dict) else load_state(store)
    return quantize_entry_edge(
        state.get('entry_edge_floor') or ENTRY_EDGE_DEFAULT)


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
        vals = {a: _score_value(scores, a) for a in ('BUY', 'SELL', 'HOLD')}
        if all(v is not None for v in vals.values()):
            action = max(('BUY', 'SELL', 'HOLD'), key=lambda a: vals[a])
        else:
            action = 'HOLD'
    margin = _finite(evaluation.get('score_edge'))
    if margin is None:
        margin = score_margin(scores)
    # Missing scores → margin stays None. Do NOT invent 0.0 ("weak edge") —
    # that falsely blocked explicit submit / lot-recovery when no signal yet.
    conf = None
    if isinstance(scores, dict):
        finite_scores = [_finite(scores.get(k)) for k in ('BUY', 'SELL', 'HOLD')]
        finite_scores = [v for v in finite_scores if v is not None]
        if finite_scores:
            conf = max(finite_scores)
    return {
        'action': action,
        'margin': float(margin) if margin is not None else None,
        'confidence': float(conf) if conf is not None else 0.0,
        'reason': evaluation.get('reason'),
        'proposal': evaluation.get('proposal') or action,
    }


def min_open_margin(state: dict = None) -> float:
    """LIVE entry floor — calibrated value in state, else default 0.06.

    Always a whole 0.01 step. Does not ratchet with £/ΔW — only via
    ``set_calibrated_floor`` (holdout + spread) inside soft rails.
    """
    state = state or default_state()
    return quantize_entry_edge(
        state.get('entry_edge_floor') or ENTRY_EDGE_DEFAULT)


# Back-compat alias used while the module settled.
_min_open_margin = min_open_margin


def set_calibrated_floor(store, value, *, method='holdout', detail=None) -> dict:
    """Persist holdout/spread-calibrated edge — never from last £ win/loss.

    Value is snapped to ENTRY_EDGE_STEP (0.01): 0.06, 0.07, … not 0.065.
    """
    state = load_state(store)
    floor = quantize_entry_edge(value if value is not None else ENTRY_EDGE_DEFAULT)
    state['entry_edge_floor'] = floor
    save_state(store, state)
    meta = {
        'floor': floor,
        'method': str(method or 'holdout'),
        'detail': detail if isinstance(detail, dict) else {},
    }
    if store is not None:
        store.set(CALIB_META_KEY, meta)
    return meta


def calibration_meta(store) -> dict:
    raw = store.get(CALIB_META_KEY) if store is not None else None
    if isinstance(raw, dict):
        return raw
    return {'floor': ENTRY_EDGE_DEFAULT, 'method': 'default', 'detail': {}}


def note_weight_update(store, *, won: bool, delta_norm=None) -> dict:
    """Neural ΔW updates scores; does not move the entry floor.

    Floor changes only through holdout calibration (``set_calibrated_floor``).
    """
    return load_state(store)


def edge_allows_open(scores, evaluation=None, cycle_state=None) -> dict:
    """Public gate: after £ losses, weak neural margin becomes HOLD."""
    state = dict(cycle_state or default_state())
    decision = perceive_decision(scores, evaluation)
    need = min_open_margin(state)
    act = decision['action']
    margin = decision['margin']
    # Only gate when edge is known — no scores ≠ automatic weak-edge veto.
    if act in ('BUY', 'SELL') and margin is not None and float(margin) < need:
        return {
            'allowed': False,
            'action': 'HOLD',
            'reason': (
                f'Unified cycle: edge {float(margin):.3f} < required {need:.3f} '
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
    # Known margin only; missing scores skip the weak-edge HOLD (not invent 0).
    raw_margin = decision['margin']
    known_margin = raw_margin is not None
    margin = float(raw_margin) if known_margin else 0.0
    aggression = _clip(_finite(state.get('aggression')) or 1.0,
                       AGGRESSION_MIN, AGGRESSION_MAX)
    need = min_open_margin(state)
    zone = zone if isinstance(zone, dict) else {}
    tp_hint = _finite(max_tp) if max_tp is not None else _finite(zone.get('max_tp'))
    base_hint = _finite(base_sl)
    if base_hint is None:
        base_hint = _finite(zone.get('range_distance')) or 0.0
    if act in ('BUY', 'SELL') and known_margin and margin < need:
        return {
            'action': 'HOLD',
            'kappa': None,
            'sl_scale': None,
            'base_sl': float(base_hint),
            'max_tp': float(tp_hint) if tp_hint is not None else None,
            'blocked': True,
            'reason': (
                f'Unified cycle: edge {margin:.3f} < required {need:.3f} '
                f'after {_safe_int(state.get("loss_streak"), 0)} loss streak'),
            'decision': decision,
            'aggression': aggression,
            'min_margin': need,
        }
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
    tp = tp_hint
    base = float(base_hint)
    if base > 0 and tp is not None and tp > 0:
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
    """Fold broker £ into aggression / streaks for exit width — not entry floor.

    Calibrated entry_edge_floor is preserved (clipped to rails). Aggression
    still shapes κ/sl on the next OPEN so exits improve from £ feedback.
    """
    state = load_state(store)
    p = _finite(pnl)
    state['settlements'] = _safe_int(state.get('settlements'), 0) + 1
    state['last_pnl'] = p
    state['last_action'] = action if action in ('BUY', 'SELL', 'HOLD', None) else None
    # Keep calibrated floor (0.01 steps); do not reset on every settle.
    state['entry_edge_floor'] = quantize_entry_edge(
        state.get('entry_edge_floor') or ENTRY_EDGE_DEFAULT)
    agg = _finite(state.get('aggression')) or 1.0
    if p is None:
        return save_state(store, state)
    if p < 0:
        state['loss_streak'] = _safe_int(state.get('loss_streak'), 0) + 1
        state['win_streak'] = 0
        # Cool aggression after losses → tighter SL/κ on next OPEN (exits).
        step = 0.96 if state['loss_streak'] >= 2 else 0.98
        state['aggression'] = _clip(agg * step, AGGRESSION_MIN, AGGRESSION_MAX)
    elif p > 0:
        state['win_streak'] = _safe_int(state.get('win_streak'), 0) + 1
        state['loss_streak'] = 0
        step = 1.02 if state['win_streak'] >= 2 else 1.01
        state['aggression'] = _clip(agg * step, AGGRESSION_MIN, AGGRESSION_MAX)
    else:
        # Flat £ — mild cool-off, do not treat as a win.
        state['aggression'] = _clip(agg * 0.995, AGGRESSION_MIN, AGGRESSION_MAX)
    return save_state(store, state)


def note_open(store, *, action=None) -> dict:
    state = load_state(store)
    state['opens'] = _safe_int(state.get('opens'), 0) + 1
    state['last_action'] = action if action in ('BUY', 'SELL', 'HOLD', None) else None
    return save_state(store, state)
