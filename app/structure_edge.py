"""Adaptive structure edge — robot learns which layout×side pays, without hard rules.

Uses the same TREND/BREAKOUT/RANGE/CHOP buckets as ``structure_shadow`` (£ already
banked in ``total_pnl`` / ``samples``). Does **not** invent “always HOLD against
trend”: it compares BUY vs SELL EV in the live frame and may soft-nudge scores,
teach the opposite side, or HOLD only when *both* sides look negative.

Trail self-arm: enable broker trail when MFE / structure EV says a runner is
plausible — not a fixed human “always after 1R” law.
"""
from __future__ import annotations

import math

from . import structure_shadow as ss

STORE_ADVICE_KEY = 'structure_edge_last_advice_v1'


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _cfg(store=None):
    from .self_params import load
    return load(store)


def _bucket(store, epic, structure, direction):
    lib = ss.load_library(store)
    key = ss.frame_key(epic=epic, structure=structure, direction=direction)
    b = (lib.get('buckets') or {}).get(key)
    return key, b if isinstance(b, dict) else None


def side_stats(store, epic, structure, direction):
    """Mean £ / samples for one structure×side — None mean when no evidence."""
    key, b = _bucket(store, epic, structure, direction)
    if not b:
        return {
            'key': key, 'samples': 0, 'mean_pnl': None, 'win_count': 0,
            'loss_count': 0, 'total_pnl': 0.0,
        }
    n = int(b.get('samples') or 0)
    total = _finite(b.get('total_pnl')) or 0.0
    return {
        'key': key,
        'samples': n,
        'mean_pnl': (total / n) if n > 0 else None,
        'win_count': int(b.get('win_count') or 0),
        'loss_count': int(b.get('loss_count') or 0),
        'total_pnl': float(total),
    }


def compare_frame(store, *, epic, structure):
    """BUY vs SELL evidence in one layout — robot's adaptive edge read."""
    cfg = _cfg(store)
    need = max(2, int(cfg.get('structure_edge_min_samples', 5)))
    buy = side_stats(store, epic, structure, 'BUY')
    sell = side_stats(store, epic, structure, 'SELL')
    out = {
        'structure': structure,
        'epic': epic,
        'buy': buy,
        'sell': sell,
        'min_samples': need,
        'advice': None,
        'reason': 'insufficient_evidence',
        'confidence': 0.0,
    }
    buy_n, sell_n = buy['samples'], sell['samples']
    buy_m, sell_m = buy['mean_pnl'], sell['mean_pnl']
    # Need at least one side with enough samples to speak.
    buy_ready = buy_n >= need and buy_m is not None
    sell_ready = sell_n >= need and sell_m is not None
    if not buy_ready and not sell_ready:
        return out

    # Rank sides that have evidence; missing side = unknown (not forced HOLD).
    ranked = []
    if buy_ready:
        ranked.append(('BUY', float(buy_m), buy_n))
    if sell_ready:
        ranked.append(('SELL', float(sell_m), sell_n))
    ranked.sort(key=lambda x: x[1], reverse=True)
    best_side, best_ev, best_n = ranked[0]
    other = ranked[1] if len(ranked) > 1 else None

    if best_ev > 0:
        out['advice'] = best_side
        if other is None:
            out['reason'] = (
                f'{structure}: {best_side} mean £={best_ev:.4f} '
                f'(n={best_n}); opposite unproven — prefer {best_side}')
            out['confidence'] = min(0.85, 0.35 + 0.05 * best_n)
        elif other[1] < 0:
            out['reason'] = (
                f'{structure}: {best_side} +EV ({best_ev:.4f}) vs '
                f'{other[0]} −EV ({other[1]:.4f}) — prefer {best_side}')
            out['confidence'] = min(0.95, 0.45 + 0.04 * (best_n + other[2]))
        else:
            # Both positive — keep the stronger, mild confidence.
            out['reason'] = (
                f'{structure}: both sides +EV; stronger {best_side} '
                f'({best_ev:.4f} vs {other[1]:.4f})')
            out['confidence'] = min(0.7, 0.3 + 0.03 * best_n)
        return out

    # Best available side is still ≤0.
    if other is not None and other[1] <= 0 and best_ev <= 0:
        out['advice'] = 'HOLD'
        out['reason'] = (
            f'{structure}: BUY mean={buy_m} (n={buy_n}), '
            f'SELL mean={sell_m} (n={sell_n}) — both ≤0; HOLD until edge appears')
        out['confidence'] = min(0.9, 0.4 + 0.03 * (buy_n + sell_n))
        return out

    # Only one side known and it is ≤0 — do NOT auto-HOLD forever; leave room
    # for the opposite (unproven) to be tried by neurons / CF teacher.
    out['advice'] = None
    out['reason'] = (
        f'{structure}: known side {best_side} mean £={best_ev:.4f}≤0 '
        f'(n={best_n}); opposite still unproven — no forced HOLD')
    out['confidence'] = min(0.55, 0.2 + 0.03 * best_n)
    return out


def soft_nudge_scores(scores, advice, *, store=None):
    """Gently tilt softmax-ish scores toward adaptive advice — never a hard veto.

    Returns (new_scores, meta). If advice is None/weak, scores unchanged.

    After tilt, scores are clipped to [0, 1] and renormalized to sum exactly 1
    so ``autonomy._safe_scores`` (sum≈1) does not reject → false HOLD.
    """
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if not isinstance(advice, dict) or not advice.get('advice'):
        return dict(scores), {'nudged': False, 'reason': 'no_advice'}
    cfg = _cfg(store)
    strength = float(cfg.get('structure_edge_nudge', 0.08))
    conf = float(advice.get('confidence') or 0)
    if conf < 0.35 or strength <= 0:
        return dict(scores), {'nudged': False, 'reason': 'weak_confidence'}
    delta = strength * conf
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    target = advice['advice']
    if target in ('BUY', 'SELL'):
        out[target] = out.get(target, 0) + delta
        # Slightly damp the weaker directional side only — HOLD untouched unless
        # advice is HOLD.
        other = 'SELL' if target == 'BUY' else 'BUY'
        out[other] = out.get(other, 0) - 0.5 * delta
    elif target == 'HOLD':
        out['HOLD'] = out.get('HOLD', 0) + delta
        out['BUY'] = out.get('BUY', 0) - 0.35 * delta
        out['SELL'] = out.get('SELL', 0) - 0.35 * delta
    # Clip negatives from dampening, then renormalize to a valid simplex.
    for k in out:
        out[k] = max(0.0, float(out[k]))
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate_after_nudge'}
    out = {k: v / total for k, v in out.items()}
    return out, {
        'nudged': True, 'delta': delta, 'target': target,
        'reason': advice.get('reason'), 'confidence': conf,
        'renormalized': True, 'sum': sum(out.values()),
    }


def teach_after_loss(*, direction, cf_best, structure_prefer_hold=False,
                     frame_advice=None, opp_val=None, opp_floor=None):
    """Loss teacher: evaluate opposite via structure EV — do not auto-HOLD.

    Priority:
      1) If structure EV clearly prefers the opposite side → teach opposite
      2) If structure EV says both ≤0 → HOLD
      3) Else keep counterfactual choice (cf_best), even when old
         ``structure_prefer_hold`` would have forced HOLD
    """
    opposite = 'SELL' if direction == 'BUY' else 'BUY'
    adv = frame_advice if isinstance(frame_advice, dict) else {}
    advice = adv.get('advice')

    if advice == opposite:
        # Never bypass the spread/cost floor — structure EV is a hint, not a free flip.
        # Without opp_val + opp_floor, do NOT flip on historical mean £ alone
        # (missing spread/tick would teach SELL after BUY without cost proof).
        if opp_val is not None and opp_floor is not None:
            if float(opp_val) > float(opp_floor):
                return opposite, 'loss_structure_edge_opposite'
            if cf_best in ('BUY', 'SELL', 'HOLD'):
                return cf_best, 'loss_structure_edge_opp_below_floor'
            return 'HOLD', 'loss_structure_edge_opp_below_floor'
        if cf_best in ('BUY', 'SELL', 'HOLD'):
            return cf_best, 'loss_structure_edge_keeps_cf_no_floor'
        return 'HOLD', 'loss_structure_edge_no_cf_floor'
    if advice == 'HOLD':
        return 'HOLD', 'loss_structure_edge_both_negative'
    # Structure says this layout pays the taken side? Unusual on a loss — keep CF.
    if advice == direction:
        if cf_best in ('BUY', 'SELL', 'HOLD'):
            return cf_best, 'loss_structure_edge_keeps_cf'
        return 'HOLD', 'loss_structure_edge_same_side_hold'

    # No clear frame advice. Old prefer_hold meant “this side is a grave”.
    # Re-interpret: if opposite CF clears floor → opposite; else HOLD.
    # Never force HOLD solely because prefer_hold when opposite CF is strong.
    if structure_prefer_hold:
        floor = opp_floor
        if floor is None:
            floor = 0.0
        if opp_val is not None and float(opp_val) > float(floor):
            return opposite, 'loss_structure_grave_try_opposite'
        return 'HOLD', 'loss_structure_grave_hold'
    if cf_best in ('BUY', 'SELL', 'HOLD'):
        return cf_best, 'loss_cf_unchanged'
    return 'HOLD', 'loss_default_hold'


def trail_arm_decision(store, *, epic, structure, direction, mfe=None,
                       original_risk=None, be_locked=False):
    """Whether the robot should enable broker trail now.

    Adaptive evidence (any one is enough once past noise):
      • stop already at/through BE → always arm (lock gains)
      • MFE ≥ trail_arm_mfe_frac × opening risk
      • structure×side mean £ > trail_arm_min_ev with enough samples
    """
    cfg = _cfg(store)
    out = {
        'arm': False, 'reason': 'waiting_for_evidence',
        'mfe': _finite(mfe), 'original_risk': _finite(original_risk),
        'structure': structure, 'direction': direction,
    }
    if be_locked:
        out['arm'] = True
        out['reason'] = 'be_locked — trail to protect open gains'
        return out
    risk = _finite(original_risk)
    mfe_n = _finite(mfe)
    frac = float(cfg.get('trail_arm_mfe_frac', 0.55))
    if risk is not None and risk > 0 and mfe_n is not None and mfe_n >= frac * risk:
        out['arm'] = True
        out['reason'] = (
            f'mfe {mfe_n:.4f} ≥ {frac:.2f}×risk {risk:.4f} — arm trail')
        return out
    need = max(2, int(cfg.get('structure_edge_min_samples', 5)))
    st = side_stats(store, epic, structure, direction)
    min_ev = float(cfg.get('trail_arm_min_ev', 0.0))
    if (st['samples'] >= need and st['mean_pnl'] is not None
            and float(st['mean_pnl']) > min_ev
            and mfe_n is not None and risk is not None and mfe_n >= 0.35 * risk):
        out['arm'] = True
        out['reason'] = (
            f'structure {structure}/{direction} mean £={st["mean_pnl"]:.4f} '
            f'+ partial mfe — arm trail')
        return out
    out['reason'] = (
        f'waiting: mfe={mfe_n} risk={risk} structure_mean='
        f'{st.get("mean_pnl")} n={st.get("samples")}')
    return out


def persist_advice(store, advice):
    if store is None or not isinstance(advice, dict):
        return
    store.set(STORE_ADVICE_KEY, dict(advice))
