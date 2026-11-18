"""Situation memory — self-improving scene×action £ memory without hard rules.

Goal (operator intent):
  • Describe the live market as a *situation* (what a trader would notice).
  • After each closed trade, bank £ into that situation×action bucket.
  • Before OPEN, soft-nudge scores away from scenes that recently lost money
    and toward scenes that paid — never a hardcoded "DOWN → no BUY".
  • Exponential time decay so regime change overwrites stale lessons.

This is the ready loop: scene → £ → next OPEN influence → forget old EV.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'situation_memory_v1'
LAST_ADVICE_KEY = 'situation_memory_last_advice_v1'


def _now():
    return datetime.now(timezone.utc)


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _cfg(store=None):
    from .self_params import load
    return load(store)


def _parse_ts(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _flow_tag(returns_bps):
    """Multi-TF flow as a trader would skim: aligned up/down or mixed."""
    if not isinstance(returns_bps, dict):
        return 'FLOW_UNKNOWN'
    try:
        r5 = float(returns_bps.get('5', 0) or 0)
        r15 = float(returns_bps.get('15', 0) or 0)
    except (TypeError, ValueError):
        return 'FLOW_UNKNOWN'
    if r5 > 0 and r15 > 0:
        return 'FLOW_UP'
    if r5 < 0 and r15 < 0:
        return 'FLOW_DOWN'
    return 'FLOW_MIXED'


def _trend_tag(trend):
    t = (trend or '').upper() if isinstance(trend, str) else ''
    if t in ('UP', 'DOWN', 'MIXED'):
        return 'TREND_' + t
    return 'TREND_UNKNOWN'


def fingerprint(*, epic=None, market_context=None, structure=None, returns_bps=None,
                trend=None):
    """Compact scene id — description only, never a ban rule.

    Parts a human glances at: instrument, HTF trend label, 5/15m flow, OHLC
    structure frame (TREND_UP/BREAKOUT_DOWN/…).
    """
    ctx = market_context if isinstance(market_context, dict) else {}
    ep = (epic or ctx.get('epic') or 'UNKNOWN')
    tr = _trend_tag(trend if trend is not None else ctx.get('trend'))
    flow = _flow_tag(returns_bps if returns_bps is not None else ctx.get('returns_bps'))
    st = (structure or (ctx.get('structure') if ctx else None) or 'CHOP')
    st = str(st).upper() if st else 'CHOP'
    return f'{ep}|{tr}|{flow}|{st}'


def load(store):
    raw = store.get(STORE_KEY) if store is not None else None
    if not isinstance(raw, dict):
        return {'version': 1, 'buckets': {}, 'updated_at': None}
    buckets = raw.get('buckets')
    if not isinstance(buckets, dict):
        buckets = {}
    return {
        'version': int(raw.get('version') or 1),
        'buckets': dict(buckets),
        'updated_at': raw.get('updated_at'),
    }


def save(store, state):
    if store is None:
        return
    out = dict(state or {})
    out['updated_at'] = _now().isoformat(timespec='seconds')
    store.set(STORE_KEY, out)


def _bucket_key(situation, action):
    return f'{situation}|{action}'


def _decay_factor(last_ts, *, half_life_days, now=None):
    """Weight remaining after elapsed time (0.5 ** (days / half_life))."""
    half = max(0.5, float(half_life_days or 14.0))
    now = now or _now()
    prev = _parse_ts(last_ts)
    if prev is None:
        return 1.0
    days = max(0.0, (now - prev).total_seconds() / 86400.0)
    return float(0.5 ** (days / half))


def record_outcome(store, *, situation, action, pnl, epic=None, deal_id=None,
                   now=None):
    """Bank £ into situation×action with time decay on prior mass."""
    if action not in ('BUY', 'SELL', 'HOLD') or not situation:
        return None
    cash = _finite(pnl)
    if cash is None:
        return None
    cfg = _cfg(store)
    half = float(cfg.get('situation_memory_half_life_days', 14.0))
    now = now or _now()
    state = load(store)
    buckets = dict(state.get('buckets') or {})
    key = _bucket_key(situation, action)
    prev = buckets.get(key) if isinstance(buckets.get(key), dict) else {}
    factor = _decay_factor(prev.get('updated_at'), half_life_days=half, now=now)
    old_n = float(prev.get('samples_eff') or 0.0) * factor
    old_pnl = float(prev.get('total_pnl_eff') or 0.0) * factor
    old_wins = float(prev.get('win_eff') or 0.0) * factor
    old_losses = float(prev.get('loss_eff') or 0.0) * factor
    samples_eff = old_n + 1.0
    total_pnl_eff = old_pnl + cash
    win_eff = old_wins + (1.0 if cash > 0 else 0.0)
    loss_eff = old_losses + (1.0 if cash < 0 else 0.0)
    buckets[key] = {
        'situation': situation,
        'action': action,
        'epic': epic,
        'samples_eff': samples_eff,
        'total_pnl_eff': total_pnl_eff,
        'mean_pnl': total_pnl_eff / samples_eff if samples_eff > 0 else None,
        'win_eff': win_eff,
        'loss_eff': loss_eff,
        'raw_count': int(prev.get('raw_count') or 0) + 1,
        'last_pnl': cash,
        'last_deal_id': deal_id,
        'updated_at': now.isoformat(timespec='seconds'),
    }
    # Cap library size — drop oldest by updated_at.
    if len(buckets) > 240:
        ordered = sorted(
            buckets.items(),
            key=lambda kv: kv[1].get('updated_at') or '',
        )
        buckets = dict(ordered[-200:])
    state['buckets'] = buckets
    save(store, state)
    return buckets[key]


def side_stats(store, situation, action, *, now=None):
    cfg = _cfg(store)
    half = float(cfg.get('situation_memory_half_life_days', 14.0))
    now = now or _now()
    state = load(store)
    key = _bucket_key(situation, action)
    b = (state.get('buckets') or {}).get(key)
    if not isinstance(b, dict):
        return {'samples': 0, 'mean_pnl': None, 'total_pnl': 0.0}
    factor = _decay_factor(b.get('updated_at'), half_life_days=half, now=now)
    n = float(b.get('samples_eff') or 0.0) * factor
    total = float(b.get('total_pnl_eff') or 0.0) * factor
    return {
        'samples': n,
        'mean_pnl': (total / n) if n > 1e-9 else None,
        'total_pnl': total,
        'raw_count': int(b.get('raw_count') or 0),
    }


def compare_situation(store, *, situation, now=None):
    """Data-only advice for this scene — None when evidence is thin."""
    cfg = _cfg(store)
    need = max(2.0, float(cfg.get('situation_memory_min_samples', 3)))
    now = now or _now()
    buy = side_stats(store, situation, 'BUY', now=now)
    sell = side_stats(store, situation, 'SELL', now=now)
    hold = side_stats(store, situation, 'HOLD', now=now)
    out = {
        'situation': situation,
        'buy': buy,
        'sell': sell,
        'hold': hold,
        'min_samples': need,
        'advice': None,
        'confidence': 0.0,
        'reason': 'insufficient_evidence',
    }
    ranked = []
    for side, stats in (('BUY', buy), ('SELL', sell), ('HOLD', hold)):
        if stats['samples'] >= need and stats['mean_pnl'] is not None:
            ranked.append((side, float(stats['mean_pnl']), float(stats['samples'])))
    if not ranked:
        return out
    ranked.sort(key=lambda x: x[1], reverse=True)
    best_side, best_ev, best_n = ranked[0]

    # Both directional sides proven negative → HOLD (learned, not hardcoded trend).
    buy_bad = (buy['samples'] >= need and buy['mean_pnl'] is not None
               and float(buy['mean_pnl']) < 0)
    sell_bad = (sell['samples'] >= need and sell['mean_pnl'] is not None
                and float(sell['mean_pnl']) < 0)
    if buy_bad and sell_bad:
        out['advice'] = 'HOLD'
        out['reason'] = (
            f'{situation}: BUY and SELL both −EV after decay '
            f'(buy £={buy["mean_pnl"]:.4f}, sell £={sell["mean_pnl"]:.4f})')
        out['confidence'] = min(0.9, 0.4 + 0.05 * min(buy['samples'], sell['samples']))
        return out

    if best_ev > 0:
        out['advice'] = best_side
        out['reason'] = (
            f'{situation}: {best_side} mean £={best_ev:.4f} (n≈{best_n:.1f})')
        out['confidence'] = min(0.9, 0.35 + 0.06 * best_n)
        return out

    # Best proven side is still ≤0 → prefer HOLD when we have directional graves.
    if best_side in ('BUY', 'SELL') and best_ev <= 0:
        out['advice'] = 'HOLD'
        out['reason'] = (
            f'{situation}: best side {best_side} still ≤0 £={best_ev:.4f} — HOLD')
        out['confidence'] = min(0.85, 0.35 + 0.05 * best_n)
        return out

    if best_side == 'HOLD' and best_ev >= 0:
        out['advice'] = 'HOLD'
        out['reason'] = f'{situation}: HOLD paid (mean £={best_ev:.4f})'
        out['confidence'] = min(0.8, 0.35 + 0.05 * best_n)
        return out
    return out


def soft_nudge_scores(scores, advice, *, store=None, live_trend=None):
    """Tilt scores toward situation £ advice — never a hard veto.

    Renormalizes to sum=1 so autonomy._safe_scores stays valid.
    When live multi-TF trend fights historical scene advice, skip — do not
    glue the robot to yesterday's SELL while the tape has already turned UP.
    """
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if not isinstance(advice, dict) or not advice.get('advice'):
        return dict(scores), {'nudged': False, 'reason': 'no_advice'}
    cfg = _cfg(store)
    strength = float(cfg.get('situation_memory_nudge', 0.12))
    conf = float(advice.get('confidence') or 0)
    if conf < 0.35 or strength <= 0:
        return dict(scores), {'nudged': False, 'reason': 'weak_confidence'}
    target = advice['advice']
    trend = (live_trend or '').upper() if isinstance(live_trend, str) else None
    if target in ('BUY', 'SELL') and trend in ('UP', 'DOWN'):
        if (target == 'BUY' and trend == 'DOWN') or (target == 'SELL' and trend == 'UP'):
            return dict(scores), {
                'nudged': False, 'reason': 'live_trend_disagrees',
                'target': target, 'live_trend': trend,
            }
    delta = strength * conf
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    if target in ('BUY', 'SELL'):
        out[target] = out.get(target, 0) + delta
        other = 'SELL' if target == 'BUY' else 'BUY'
        out[other] = out.get(other, 0) - 0.45 * delta
        out['HOLD'] = out.get('HOLD', 0) - 0.15 * delta
    elif target == 'HOLD':
        out['HOLD'] = out.get('HOLD', 0) + delta
        out['BUY'] = out.get('BUY', 0) - 0.4 * delta
        out['SELL'] = out.get('SELL', 0) - 0.4 * delta
    for k in out:
        out[k] = max(0.0, float(out[k]))
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate'}
    out = {k: v / total for k, v in out.items()}
    return out, {
        'nudged': True, 'delta': delta, 'target': target,
        'reason': advice.get('reason'), 'confidence': conf,
        'situation': advice.get('situation'),
        'renormalized': True, 'sum': sum(out.values()),
    }


def persist_advice(store, advice):
    if store is not None and isinstance(advice, dict):
        store.set(LAST_ADVICE_KEY, advice)


def snapshot(store):
    state = load(store)
    buckets = state.get('buckets') or {}
    return {
        'bucket_count': len(buckets),
        'updated_at': state.get('updated_at'),
        'last_advice': store.get(LAST_ADVICE_KEY) if store is not None else None,
    }
