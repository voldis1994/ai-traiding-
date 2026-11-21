"""M1 multi-source consensus: Order Flow + Liquidity + Short-term Impact.

Free-only stack at the robot's real tempo (completed M1). Several public
feeds are fused into one consensus — no paid CME/dxFeed path planned.

  • Yahoo ``GC=F`` / ETF volume (delayed futures tape — free proxy)
  • Coinbase / OKX ``PAXG`` volume + L2 book (gold substitute, labeled)
  • Capital BID/ASK + OHLC structure remain execution / swing truth

Never invents Capital CFD depth. Soft-nudge only; never hard-vetoes OPEN.
"""
from __future__ import annotations

import math
from typing import Any

from .liquidity_feed import (
    build_liquidity_observation,
    load_liquidity_snapshot,
    liquidity_observation_from_path,
)

STORE_NUDGE_KEY = 'flow_consensus_nudge'
STORE_BLOCK_KEY = 'flow_consensus_block_fights'
DEFAULT_NUDGE = 0.18
DEFAULT_BLOCK_FIGHTS = 1
# Block OPEN only when this many pillars agree against the proposed side.
BLOCK_MIN_AGREE = 2

# Pillar thresholds on already-bounded liquidity features [-1, 1].
FLOW_IMPULSE = 0.12
FLOW_VOL_Z = 0.20
LIQ_IMBALANCE = 0.18
IMPACT_MICRO = 0.15


def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _vote(side: str | None) -> str:
    s = (side or '').upper()
    return s if s in ('BUY', 'SELL', 'NEUTRAL') else 'NEUTRAL'


def _cfg_nudge(store) -> float:
    if store is None:
        return DEFAULT_NUDGE
    try:
        from . import self_params
        v = self_params.sp_get(store, STORE_NUDGE_KEY, DEFAULT_NUDGE)
        return max(0.0, min(0.35, float(v)))
    except Exception:
        return DEFAULT_NUDGE


def _cfg_block_fights(store) -> bool:
    if store is None:
        return bool(DEFAULT_BLOCK_FIGHTS)
    try:
        from . import self_params
        v = self_params.sp_get(store, STORE_BLOCK_KEY, DEFAULT_BLOCK_FIGHTS)
        return int(v) != 0
    except Exception:
        return bool(DEFAULT_BLOCK_FIGHTS)


def provider_catalog(*, snapshot=None) -> list[dict]:
    """Free public sources fused into one M1 consensus (no paid providers)."""
    active = []
    if isinstance(snapshot, dict):
        routes = snapshot.get('routes') if isinstance(snapshot.get('routes'), dict) else {}
        for sym in routes.get('yahoo') or []:
            active.append({
                'id': f'yahoo:{sym}',
                'label': f'Yahoo {sym}',
                'status': 'active',
                'role': 'volume_1m',
                'relation': 'substitute' if str(sym).endswith('=F') or sym in (
                    'GLD', 'SLV') else 'external',
            })
        if routes.get('coinbase'):
            active.append({
                'id': f"coinbase:{routes['coinbase']}",
                'label': f"Coinbase {routes['coinbase']}",
                'status': 'active',
                'role': 'volume_1m+l2_book',
                'relation': 'substitute',
            })
        if routes.get('okx'):
            active.append({
                'id': f"okx:{routes['okx']}",
                'label': f"OKX {routes['okx']}",
                'status': 'active',
                'role': 'volume_1m',
                'relation': 'substitute',
            })
        for src in snapshot.get('sources') or []:
            if not any(a['id'].split(':', 1)[-1] in str(src) for a in active):
                active.append({
                    'id': str(src),
                    'label': str(src),
                    'status': 'active',
                    'role': 'feed',
                    'relation': 'external',
                })
    return active


def _pillar_order_flow(obs: dict) -> dict:
    """Tape / volume impulse on completed M1 (not tick race)."""
    avail = (obs.get('availability') or {}) if isinstance(obs, dict) else {}
    feats = obs.get('features') if isinstance(obs, dict) else None
    if not avail.get('primary_volume') or not isinstance(feats, (list, tuple)) or len(feats) < 3:
        return {
            'id': 'order_flow',
            'label': 'Order Flow (M1 volume)',
            'vote': 'NEUTRAL',
            'ok': False,
            'reason': 'no_volume',
        }
    vol_z = _finite(feats[0]) or 0.0
    impulse = _finite(feats[2]) if avail.get('volume_impulse') else None
    series = (obs.get('series_used') or [{}])[0]
    vote = 'NEUTRAL'
    reason = 'quiet'
    if impulse is not None and abs(impulse) >= FLOW_IMPULSE and abs(vol_z) >= FLOW_VOL_Z * 0.5:
        if impulse > 0 and vol_z >= -FLOW_VOL_Z:
            vote = 'BUY'
            reason = f'impulse={impulse:.2f} vol_z={vol_z:.2f}'
        elif impulse < 0 and vol_z <= FLOW_VOL_Z:
            vote = 'SELL'
            reason = f'impulse={impulse:.2f} vol_z={vol_z:.2f}'
    elif abs(vol_z) >= FLOW_VOL_Z * 1.5:
        vote = 'BUY' if vol_z > 0 else 'SELL'
        reason = f'vol_z={vol_z:.2f}'
    return {
        'id': 'order_flow',
        'label': 'Order Flow (M1 volume)',
        'vote': vote,
        'ok': True,
        'reason': reason,
        'vol_z': vol_z,
        'impulse': impulse,
        'source': series.get('source'),
        'instrument': series.get('instrument'),
        'relation': series.get('relation'),
    }


def _pillar_liquidity(obs: dict) -> dict:
    """External L2 imbalance (PAXG etc.) — labeled substitute, never Capital DOM."""
    book = (obs.get('book') or {}) if isinstance(obs, dict) else {}
    if not book.get('available'):
        return {
            'id': 'liquidity',
            'label': 'Liquidity (external L2)',
            'vote': 'NEUTRAL',
            'ok': False,
            'reason': book.get('reason') or 'dom_book unavailable',
        }
    imb = _finite(book.get('imbalance')) or 0.0
    vote = 'NEUTRAL'
    reason = f'imbalance={imb:.2f}'
    if imb >= LIQ_IMBALANCE:
        vote = 'BUY'
    elif imb <= -LIQ_IMBALANCE:
        vote = 'SELL'
    return {
        'id': 'liquidity',
        'label': 'Liquidity (external L2)',
        'vote': vote,
        'ok': True,
        'reason': reason,
        'imbalance': imb,
        'source': book.get('source'),
        'instrument': book.get('instrument'),
        'relation': book.get('relation'),
        'book_label': book.get('label'),
    }


def _pillar_impact(obs: dict) -> dict:
    """Short-term price-impact proxy from microprice vs mid (bounded)."""
    book = (obs.get('book') or {}) if isinstance(obs, dict) else {}
    if not book.get('available'):
        return {
            'id': 'short_term_impact',
            'label': 'Short-term Impact (micro)',
            'vote': 'NEUTRAL',
            'ok': False,
            'reason': book.get('reason') or 'dom_book unavailable',
        }
    micro = _finite(book.get('micro_bps')) or 0.0
    vote = 'NEUTRAL'
    reason = f'micro={micro:.2f}'
    if micro >= IMPACT_MICRO:
        vote = 'BUY'
    elif micro <= -IMPACT_MICRO:
        vote = 'SELL'
    return {
        'id': 'short_term_impact',
        'label': 'Short-term Impact (micro)',
        'vote': vote,
        'ok': True,
        'reason': reason,
        'micro_bps': micro,
        'source': book.get('source'),
        'instrument': book.get('instrument'),
        'relation': book.get('relation'),
    }


def _tally(pillars: list[dict]) -> dict:
    buys = sells = 0
    used = []
    for p in pillars:
        if not p.get('ok'):
            continue
        v = _vote(p.get('vote'))
        if v == 'BUY':
            buys += 1
            used.append(p['id'])
        elif v == 'SELL':
            sells += 1
            used.append(p['id'])
    if buys == 0 and sells == 0:
        return {
            'consensus': 'NEUTRAL',
            'agree': 0,
            'disagree': 0,
            'buy_votes': 0,
            'sell_votes': 0,
            'active_pillars': sum(1 for p in pillars if p.get('ok')),
            'reason': 'no_directional_votes',
        }
    if buys > sells:
        return {
            'consensus': 'BUY',
            'agree': buys,
            'disagree': sells,
            'buy_votes': buys,
            'sell_votes': sells,
            'active_pillars': sum(1 for p in pillars if p.get('ok')),
            'reason': 'majority_buy:' + ','.join(used),
        }
    if sells > buys:
        return {
            'consensus': 'SELL',
            'agree': sells,
            'disagree': buys,
            'buy_votes': buys,
            'sell_votes': sells,
            'active_pillars': sum(1 for p in pillars if p.get('ok')),
            'reason': 'majority_sell:' + ','.join(used),
        }
    return {
        'consensus': 'NEUTRAL',
        'agree': buys,
        'disagree': sells,
        'buy_votes': buys,
        'sell_votes': sells,
        'active_pillars': sum(1 for p in pillars if p.get('ok')),
        'reason': 'split_votes',
    }


def evaluate(
    obs: dict | None,
    *,
    proposed: str | None = None,
    structure: str | None = None,
    trend: str | None = None,
    snapshot: dict | None = None,
    epic: str | None = None,
) -> dict:
    """Build M1 consensus panel + soft advice from one liquidity observation."""
    out: dict[str, Any] = {
        'ok': False,
        'system': 'flow_consensus',
        'tempo': 'M1',
        'epic': epic,
        'proposed': (proposed or '').upper() or None,
        'structure': structure,
        'trend': trend,
        'pillars': [],
        'consensus': 'NEUTRAL',
        'advice': None,
        'confidence': 0.0,
        'accept': None,
        'providers': provider_catalog(snapshot=snapshot),
        'reason': None,
        'capital_note': (
            'Capital = execution BID/ASK + OHLC only; '
            'depth/size never from Capital CFD'),
    }
    if not isinstance(obs, dict) or not obs.get('ok'):
        out['reason'] = (obs or {}).get('reason') if isinstance(obs, dict) else 'no_observation'
        out['pillars'] = [
            _pillar_order_flow(obs or {}),
            _pillar_liquidity(obs or {}),
            _pillar_impact(obs or {}),
        ]
        return out

    pillars = [
        _pillar_order_flow(obs),
        _pillar_liquidity(obs),
        _pillar_impact(obs),
    ]
    out['pillars'] = pillars
    tally = _tally(pillars)
    out.update({k: tally[k] for k in (
        'consensus', 'agree', 'disagree', 'buy_votes', 'sell_votes',
        'active_pillars', 'reason')})
    out['ok'] = True
    out['coverage'] = obs.get('coverage')
    out['book'] = obs.get('book')
    out['series_used'] = obs.get('series_used')
    out['target_bar'] = obs.get('target_bar')

    cons = tally['consensus']
    active = int(tally.get('active_pillars') or 0)
    agree = int(tally.get('agree') or 0)
    # Confidence scales with how many pillars vote the same way.
    if cons in ('BUY', 'SELL') and agree >= 1:
        out['confidence'] = min(0.85, 0.35 + 0.15 * agree + 0.05 * max(0, active - 1))
        out['advice'] = cons
    else:
        out['confidence'] = 0.2 if active else 0.0
        out['advice'] = None

    prop = (proposed or '').upper()
    if prop in ('BUY', 'SELL') and cons in ('BUY', 'SELL'):
        out['accept'] = (prop == cons)
        if not out['accept']:
            out['fight_reason'] = f'proposed {prop} fights M1 consensus {cons}'
    elif prop in ('BUY', 'SELL') and cons == 'NEUTRAL':
        out['accept'] = None  # no filter
    return out


def evaluate_from_path(
    path,
    bar_time,
    *,
    now=None,
    epic=None,
    proposed=None,
    structure=None,
    trend=None,
) -> dict:
    snap = load_liquidity_snapshot(path) if path is not None else None
    obs = liquidity_observation_from_path(
        path, bar_time, now=now, epic=epic) if path is not None else build_liquidity_observation(
            {'ok': False}, bar_time, now=now, epic=epic)
    return evaluate(
        obs, proposed=proposed, structure=structure, trend=trend,
        snapshot=snap, epic=epic)


def soft_nudge_scores(scores, consensus, *, store=None):
    """Tilt scores toward M1 multi-source consensus — never hard veto.

    When proposed side fights consensus, damp that side and boost the other /
    HOLD. When aligned, mild boost. Missing/neutral consensus → no change.
    """
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if not isinstance(consensus, dict) or not consensus.get('ok'):
        return dict(scores), {
            'nudged': False,
            'reason': (consensus or {}).get('reason') if isinstance(consensus, dict)
            else 'no_consensus',
        }
    strength = _cfg_nudge(store)
    conf = float(consensus.get('confidence') or 0)
    advice = consensus.get('advice')
    if advice not in ('BUY', 'SELL') or conf < 0.35 or strength <= 0:
        return dict(scores), {
            'nudged': False, 'reason': 'weak_or_neutral',
            'consensus': consensus.get('consensus'),
            'confidence': conf,
        }
    proposed = (consensus.get('proposed') or '').upper()
    delta = strength * conf
    fight = proposed in ('BUY', 'SELL') and proposed != advice
    # Fighting consensus → louder damp so majority M1 votes can flip the tilt.
    if fight:
        delta *= 1.85
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    out[advice] = out.get(advice, 0) + delta
    other = 'SELL' if advice == 'BUY' else 'BUY'
    damp = 1.15 * delta if fight else 0.7 * delta
    out[other] = out.get(other, 0) - damp
    if fight:
        out['HOLD'] = out.get('HOLD', 0) + 0.25 * delta
    for k in out:
        out[k] = max(0.0, float(out[k]))
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate_after_nudge'}
    out = {k: v / total for k, v in out.items()}
    return out, {
        'nudged': True,
        'delta': delta,
        'target': advice,
        'proposed': proposed or None,
        'accept': consensus.get('accept'),
        'confidence': conf,
        'consensus': consensus.get('consensus'),
        'pillars': [
            {'id': p.get('id'), 'vote': p.get('vote'), 'ok': p.get('ok')}
            for p in (consensus.get('pillars') or [])
        ],
        'renormalized': True,
        'sum': sum(out.values()),
        'tempo': 'M1',
    }


def apply_to_decision(
    scores,
    *,
    action,
    path=None,
    bar_time=None,
    epic=None,
    structure=None,
    trend=None,
    store=None,
    consensus=None,
) -> dict:
    """Last-word M1 consensus on LIVE OPEN — nudge scores and may HOLD a fight.

    Missing / weak feeds → no-op (never freeze the robot). Strong majority
    against the proposed side → action forced to HOLD when block_fights is on.
    """
    prop = (action or '').upper()
    out = {
        'scores': dict(scores) if isinstance(scores, dict) else scores,
        'action': prop if prop in ('BUY', 'SELL', 'HOLD') else 'HOLD',
        'proposal': prop if prop in ('BUY', 'SELL', 'HOLD') else 'HOLD',
        'blocked': False,
        'nudged': False,
        'effect': 'skipped',
        'reason': None,
        'consensus': None,
        'nudge': {'nudged': False},
        'desk': desk_payload(None),
    }
    if prop not in ('BUY', 'SELL'):
        out['effect'] = 'no_directional_action'
        out['desk'] = desk_payload(consensus if isinstance(consensus, dict) else None)
        return out

    cons = consensus
    if not isinstance(cons, dict) or not cons.get('ok'):
        if path is not None and bar_time is not None:
            cons = evaluate_from_path(
                path, bar_time, epic=epic, proposed=prop,
                structure=structure, trend=trend)
        else:
            cons = {'ok': False, 'reason': 'liquidity_path_or_bar_missing'}
    else:
        # Re-bind proposed to the final policy action.
        cons = dict(cons)
        cons['proposed'] = prop
        if cons.get('consensus') in ('BUY', 'SELL'):
            cons['accept'] = (prop == cons.get('consensus'))
            if not cons['accept']:
                cons['fight_reason'] = (
                    f"proposed {prop} fights M1 consensus {cons.get('consensus')}")

    out['consensus'] = cons
    out['desk'] = desk_payload(cons)
    if not cons.get('ok'):
        out['effect'] = 'feed_unavailable'
        out['reason'] = cons.get('reason')
        return out

    nudged, meta = soft_nudge_scores(out['scores'], cons, store=store)
    out['scores'] = nudged
    out['nudge'] = meta
    out['nudged'] = bool(meta.get('nudged'))

    agree = int(cons.get('agree') or 0)
    fight = cons.get('accept') is False and cons.get('consensus') in ('BUY', 'SELL')
    block_on = _cfg_block_fights(store)
    if fight and block_on and agree >= BLOCK_MIN_AGREE:
        out['action'] = 'HOLD'
        out['blocked'] = True
        out['effect'] = 'blocked_fight'
        out['reason'] = (
            f"M1 multi-source consensus {cons.get('consensus')} "
            f"(agree={agree}) blocks {prop}")
        out['desk'] = desk_payload({
            **cons,
            'decision_effect': out['effect'],
            'decision_reason': out['reason'],
            'blocked': True,
        })
        return out

    # After nudge, if argmax flipped to consensus side, surface that as the action.
    try:
        argmax = max(('BUY', 'SELL', 'HOLD'),
                     key=lambda a: float(nudged.get(a, 0) or 0))
    except Exception:
        argmax = prop
    if argmax in ('BUY', 'SELL') and argmax != prop and cons.get('advice') == argmax:
        out['action'] = argmax
        out['effect'] = 'flipped_to_consensus'
        out['reason'] = f'scores flipped {prop}→{argmax} by M1 consensus'
    elif out['nudged'] and cons.get('accept') is True:
        out['effect'] = 'aligned_boost'
        out['reason'] = f'M1 consensus confirms {prop}'
    elif out['nudged']:
        out['effect'] = 'nudged'
        out['reason'] = meta.get('reason') or 'soft_nudge'
    else:
        out['effect'] = 'neutral'
        out['reason'] = meta.get('reason') or cons.get('reason')

    out['desk'] = desk_payload({
        **cons,
        'decision_effect': out['effect'],
        'decision_reason': out['reason'],
        'blocked': False,
        'final_action': out['action'],
    })
    return out


def desk_payload(consensus: dict | None) -> dict:
    """Compact UI payload for the control-panel book column."""
    if not isinstance(consensus, dict):
        return {'ok': False, 'reason': 'unavailable'}
    pillars = []
    for p in consensus.get('pillars') or []:
        pillars.append({
            'id': p.get('id'),
            'label': p.get('label'),
            'vote': p.get('vote'),
            'ok': bool(p.get('ok')),
            'reason': p.get('reason'),
            'instrument': p.get('instrument'),
            'relation': p.get('relation'),
        })
    providers = []
    for p in consensus.get('providers') or []:
        providers.append({
            'id': p.get('id'),
            'label': p.get('label'),
            'status': p.get('status'),
            'role': p.get('role'),
        })
    return {
        'ok': bool(consensus.get('ok')),
        'tempo': 'M1',
        'consensus': consensus.get('consensus'),
        'advice': consensus.get('advice'),
        'accept': consensus.get('accept'),
        'confidence': consensus.get('confidence'),
        'agree': consensus.get('agree'),
        'reason': consensus.get('reason'),
        'capital_note': consensus.get('capital_note'),
        'pillars': pillars,
        'providers': providers,
        'target_bar': consensus.get('target_bar'),
        'coverage': consensus.get('coverage'),
        'decision_effect': consensus.get('decision_effect'),
        'decision_reason': consensus.get('decision_reason'),
        'blocked': bool(consensus.get('blocked')),
        'final_action': consensus.get('final_action'),
    }
