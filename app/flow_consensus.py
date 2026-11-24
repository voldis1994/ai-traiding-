"""M1 multi-source consensus: Order Flow + Liquidity + Short-term Impact.

Free-only stack at the robot's real tempo (completed M1). Several public
feeds are fused into one consensus — no paid CME/dxFeed path planned.

  • Yahoo ``GC=F`` / ETF volume (delayed futures tape — free proxy)
  • Coinbase / OKX ``PAXG`` volume + L2 book (gold substitute, labeled)
  • Capital BID/ASK + OHLC structure remain execution / swing truth

Gate / diagnostic only (V3.0 L01): soft-nudge scores for desk display, and
may HOLD a fight as a confirm/refuse gate — never flips BUY↔SELL. Neural
``scores_raw`` remain the decision authority; consensus does not rewrite
the proposed side. After closes and resolved blocks, the ledger scores
whether consensus helped £ and adapts nudge/block strength.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .liquidity_feed import (
    build_liquidity_observation,
    load_liquidity_snapshot,
    liquidity_observation_from_path,
)

STORE_NUDGE_KEY = 'flow_consensus_nudge'
STORE_BLOCK_KEY = 'flow_consensus_block_fights'
STORE_BLOCK_MIN_KEY = 'flow_consensus_block_min_agree'
STORE_LEDGER_KEY = 'flow_consensus_ledger_v1'
DEFAULT_NUDGE = 0.18
# V5 A03: seed OFF — flow is context/nudge, not a separate OPEN veto.
DEFAULT_BLOCK_FIGHTS = 0
DEFAULT_BLOCK_MIN_AGREE = 2
# Block OPEN only when this many pillars agree against the proposed side.
BLOCK_MIN_AGREE = 2
LEDGER_MAX = 80
BLOCK_HORIZON_BARS = 5  # M1 bars to judge a blocked OPEN
ADAPT_MIN_N = 4

# Default pillar thresholds on already-bounded liquidity features [-1, 1].
# Live values live in self_params (flow_pillar_*); these are seed/fallback only.
FLOW_IMPULSE = 0.12
FLOW_VOL_Z = 0.20
LIQ_IMBALANCE = 0.18
IMPACT_MICRO = 0.15
STORE_PILLAR_IMPULSE = 'flow_pillar_impulse'
STORE_PILLAR_VOL_Z = 'flow_pillar_vol_z'
STORE_PILLAR_IMBALANCE = 'flow_pillar_imbalance'
STORE_PILLAR_IMPACT = 'flow_pillar_impact'


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
        v = self_params.get(store, STORE_NUDGE_KEY, DEFAULT_NUDGE)
        return max(0.0, min(0.35, float(v)))
    except Exception:
        return DEFAULT_NUDGE


def _cfg_block_fights(store) -> bool:
    # Playbook hard ENTRY: volume/flow fight becomes a real OPEN ban.
    try:
        from . import playbook_entry as _pb
        if _pb.hard_entry_enabled(store):
            return True
    except Exception:
        pass
    if store is None:
        return bool(DEFAULT_BLOCK_FIGHTS)
    try:
        from . import self_params
        v = self_params.get(store, STORE_BLOCK_KEY, DEFAULT_BLOCK_FIGHTS)
        return int(v) != 0
    except Exception:
        return bool(DEFAULT_BLOCK_FIGHTS)


def _cfg_block_min_agree(store) -> int:
    if store is None:
        return int(DEFAULT_BLOCK_MIN_AGREE)
    try:
        from . import self_params
        v = self_params.get(
            store, STORE_BLOCK_MIN_KEY, DEFAULT_BLOCK_MIN_AGREE)
        return max(2, min(3, int(v)))
    except Exception:
        return int(DEFAULT_BLOCK_MIN_AGREE)


def _cfg_pillar_thresholds(store=None) -> dict:
    """Order-flow / liquidity / impact vote thresholds from self_params."""
    defaults = {
        'impulse': FLOW_IMPULSE,
        'vol_z': FLOW_VOL_Z,
        'imbalance': LIQ_IMBALANCE,
        'impact': IMPACT_MICRO,
    }
    if store is None:
        return defaults
    try:
        from . import self_params
        return {
            'impulse': max(0.04, min(0.40, float(self_params.get(
                store, STORE_PILLAR_IMPULSE, FLOW_IMPULSE)))),
            'vol_z': max(0.05, min(0.50, float(self_params.get(
                store, STORE_PILLAR_VOL_Z, FLOW_VOL_Z)))),
            'imbalance': max(0.05, min(0.45, float(self_params.get(
                store, STORE_PILLAR_IMBALANCE, LIQ_IMBALANCE)))),
            'impact': max(0.04, min(0.40, float(self_params.get(
                store, STORE_PILLAR_IMPACT, IMPACT_MICRO)))),
        }
    except Exception:
        return defaults


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_ledger(store) -> dict:
    if store is None:
        return {'samples': [], 'pending_blocks': [], 'stats': {}, 'adapt': {}}
    raw = store.get(STORE_LEDGER_KEY)
    if not isinstance(raw, dict):
        return {'samples': [], 'pending_blocks': [], 'stats': {}, 'adapt': {}}
    return {
        'samples': list(raw.get('samples') or [])[-LEDGER_MAX:],
        'pending_blocks': list(raw.get('pending_blocks') or [])[-40:],
        'stats': dict(raw.get('stats') or {}),
        'adapt': dict(raw.get('adapt') or {}),
    }


def save_ledger(store, bag: dict):
    if store is None:
        return
    store.set(STORE_LEDGER_KEY, {
        'samples': list(bag.get('samples') or [])[-LEDGER_MAX:],
        'pending_blocks': list(bag.get('pending_blocks') or [])[-40:],
        'stats': dict(bag.get('stats') or {}),
        'adapt': dict(bag.get('adapt') or {}),
    })


def open_stamp(flow_decision: dict | None, desk: dict | None = None) -> dict:
    """Compact stamp frozen into entry_brain at OPEN."""
    fd = flow_decision if isinstance(flow_decision, dict) else {}
    dk = desk if isinstance(desk, dict) else {}
    return {
        'effect': fd.get('effect') or dk.get('decision_effect'),
        'blocked': bool(fd.get('blocked') or dk.get('blocked')),
        'nudged': bool(fd.get('nudged')),
        'reason': fd.get('reason') or dk.get('decision_reason'),
        'consensus': dk.get('consensus') or fd.get('action'),
        'accept': dk.get('accept'),
        'agree': dk.get('agree'),
        'confidence': dk.get('confidence'),
        'proposed': dk.get('proposed') or fd.get('proposal'),
        'final_action': dk.get('final_action') or fd.get('action'),
        'at': _utc_now_iso(),
    }


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


def _pillar_order_flow(obs: dict, thr: dict | None = None) -> dict:
    """Tape / volume impulse on completed M1 (not tick race)."""
    thr = thr or _cfg_pillar_thresholds(None)
    flow_impulse = float(thr.get('impulse', FLOW_IMPULSE))
    flow_vol_z = float(thr.get('vol_z', FLOW_VOL_Z))
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
    if impulse is not None and abs(impulse) >= flow_impulse and abs(vol_z) >= flow_vol_z * 0.5:
        if impulse > 0 and vol_z >= -flow_vol_z:
            vote = 'BUY'
            reason = f'impulse={impulse:.2f} vol_z={vol_z:.2f}'
        elif impulse < 0 and vol_z <= flow_vol_z:
            vote = 'SELL'
            reason = f'impulse={impulse:.2f} vol_z={vol_z:.2f}'
    elif abs(vol_z) >= flow_vol_z * 1.5:
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


def _pillar_liquidity(obs: dict, thr: dict | None = None) -> dict:
    """External L2 imbalance (PAXG etc.) — labeled substitute, never Capital DOM."""
    thr = thr or _cfg_pillar_thresholds(None)
    liq_imbalance = float(thr.get('imbalance', LIQ_IMBALANCE))
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
    if imb >= liq_imbalance:
        vote = 'BUY'
    elif imb <= -liq_imbalance:
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


def _pillar_impact(obs: dict, thr: dict | None = None) -> dict:
    """Short-term price-impact proxy from microprice vs mid (bounded)."""
    thr = thr or _cfg_pillar_thresholds(None)
    impact_micro = float(thr.get('impact', IMPACT_MICRO))
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
    if micro >= impact_micro:
        vote = 'BUY'
    elif micro <= -impact_micro:
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
    store=None,
) -> dict:
    """Build M1 consensus panel + soft advice from one liquidity observation."""
    thr = _cfg_pillar_thresholds(store)
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
        'pillar_thresholds': thr,
        'capital_note': (
            'Capital = execution BID/ASK + OHLC only; '
            'depth/size never from Capital CFD'),
    }
    if not isinstance(obs, dict) or not obs.get('ok'):
        out['reason'] = (obs or {}).get('reason') if isinstance(obs, dict) else 'no_observation'
        out['pillars'] = [
            _pillar_order_flow(obs or {}, thr),
            _pillar_liquidity(obs or {}, thr),
            _pillar_impact(obs or {}, thr),
        ]
        return out

    pillars = [
        _pillar_order_flow(obs, thr),
        _pillar_liquidity(obs, thr),
        _pillar_impact(obs, thr),
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
    store=None,
) -> dict:
    snap = load_liquidity_snapshot(path) if path is not None else None
    obs = liquidity_observation_from_path(
        path, bar_time, now=now, epic=epic) if path is not None else build_liquidity_observation(
            {'ok': False}, bar_time, now=now, epic=epic)
    return evaluate(
        obs, proposed=proposed, structure=structure, trend=trend,
        snapshot=snap, epic=epic, store=store)


def soft_nudge_scores(scores, consensus, *, store=None):
    """Diagnostic tilt of a *copy* of scores toward M1 consensus.

    Never a hard veto and never authority over ``scores_raw`` / final action.
    Callers must keep the proposed BUY/SELL; only ``apply_to_decision`` may
    HOLD a fight as a gate. Missing/neutral consensus → no change.
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
    # Fighting consensus → louder damp for desk diagnostics only (no action flip).
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
    """M1 consensus gate / diagnostic on LIVE OPEN (V3 L01).

    Soft-nudges a score *copy* for desk diagnostics only — never changes
    BUY↔SELL (no ``flipped_to_consensus``). Neural ``scores_raw`` / proposed
    action stay authority. Strong majority against the proposed side may
    HOLD as a confirm/refuse gate when ``block_fights`` is on. Missing /
    weak feeds → no-op.
    """
    prop = (action or '').upper()
    out = {
        'scores': dict(scores) if isinstance(scores, dict) else scores,
        'scores_raw': dict(scores) if isinstance(scores, dict) else scores,
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
                structure=structure, trend=trend, store=store)
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
    block_need = _cfg_block_min_agree(store)
    if fight and block_on and agree >= block_need:
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
            'final_action': 'HOLD',
        })
        return out

    # L01: never flip BUY↔SELL from nudged argmax — scores_raw stay authority.
    # Soft nudge is diagnostic only; action stays the proposed side.
    if out['nudged'] and cons.get('accept') is True:
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
        'learn': consensus.get('learn'),
    }


def _recompute_stats(samples: list) -> dict:
    stats = {
        'n': 0,
        'aligned_wins': 0,
        'aligned_losses': 0,
        'aligned_pnl_sum': 0.0,
        'fight_wins': 0,
        'fight_losses': 0,
        'block_saved': 0,
        'block_missed': 0,
        'helpful': 0,
        'harmful': 0,
        'mean_aligned_pnl': None,
    }
    aligned_pnls = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        kind = s.get('kind')
        helpful = s.get('helpful')
        if helpful is True:
            stats['helpful'] += 1
        elif helpful is False:
            stats['harmful'] += 1
        stats['n'] += 1
        pnl = _finite(s.get('pnl'))
        if kind == 'aligned_open' and pnl is not None:
            aligned_pnls.append(pnl)
            stats['aligned_pnl_sum'] += pnl
            if pnl > 0:
                stats['aligned_wins'] += 1
            elif pnl < 0:
                stats['aligned_losses'] += 1
        elif kind == 'fight_open' and pnl is not None:
            if pnl > 0:
                stats['fight_wins'] += 1
            elif pnl < 0:
                stats['fight_losses'] += 1
        elif kind == 'block_resolved':
            if s.get('saved'):
                stats['block_saved'] += 1
            elif s.get('missed'):
                stats['block_missed'] += 1
    if aligned_pnls:
        stats['mean_aligned_pnl'] = sum(aligned_pnls) / len(aligned_pnls)
    return stats


def observe_close(
    store,
    *,
    flow_at_open,
    broker_pnl,
    direction=None,
    epic=None,
    deal_id=None,
) -> dict:
    """Score executed OPEN against frozen flow stamp; adapt system knobs."""
    if store is None:
        return {'ok': False, 'reason': 'no_store'}
    pnl = _finite(broker_pnl)
    if pnl is None:
        return {'ok': False, 'reason': 'no_pnl'}
    stamp = flow_at_open if isinstance(flow_at_open, dict) else {}
    cons = (stamp.get('consensus') or '').upper()
    effect = stamp.get('effect')
    accept = stamp.get('accept')
    prop = (stamp.get('proposed') or direction or '').upper()
    # Aligned = consensus confirmed the side we traded (or boost/flip to it).
    aligned = (
        accept is True
        or effect in ('aligned_boost', 'flipped_to_consensus')
        or (cons in ('BUY', 'SELL') and prop == cons)
    )
    fought = accept is False or effect == 'blocked_fight'
    # Did flow help the £ result?
    # aligned + win / fight + loss → helpful; aligned + loss / fight + win → harmful
    helpful = None
    if aligned:
        helpful = pnl > 0
        kind = 'aligned_open'
    elif fought:
        helpful = pnl < 0  # fighting consensus and losing means consensus was right
        kind = 'fight_open'
    else:
        kind = 'neutral_open'
        helpful = None

    sample = {
        'kind': kind,
        'helpful': helpful,
        'pnl': pnl,
        'direction': (direction or prop or '').upper() or None,
        'consensus': cons or None,
        'effect': effect,
        'accept': accept,
        'epic': epic,
        'deal_id': deal_id,
        'at': _utc_now_iso(),
    }
    bag = load_ledger(store)
    bag['samples'] = (bag.get('samples') or []) + [sample]
    bag['stats'] = _recompute_stats(bag['samples'])
    adapt = adapt_from_ledger(store, bag)
    bag['adapt'] = adapt
    save_ledger(store, bag)
    try:
        store.event('learning', 'Flow consensus observed close', {
            'kind': kind, 'helpful': helpful, 'pnl': pnl,
            'adapt': adapt.get('changed'), 'deal_id': deal_id,
        })
    except Exception:
        pass
    return {'ok': True, 'sample': sample, 'stats': bag['stats'], 'adapt': adapt}


def observe_block(
    store,
    *,
    proposed,
    consensus_side,
    agree=None,
    mid=None,
    bar_time=None,
    epic=None,
    reason=None,
) -> dict:
    """Queue a blocked OPEN for later Capital OHLC counterfactual."""
    if store is None:
        return {'ok': False, 'reason': 'no_store'}
    prop = (proposed or '').upper()
    cons = (consensus_side or '').upper()
    if prop not in ('BUY', 'SELL') or cons not in ('BUY', 'SELL'):
        return {'ok': False, 'reason': 'need_sides'}
    mid_v = _finite(mid)
    if mid_v is None or mid_v <= 0:
        return {'ok': False, 'reason': 'no_mid'}
    bag = load_ledger(store)
    pending = {
        'proposed': prop,
        'consensus': cons,
        'agree': agree,
        'mid': mid_v,
        'bar_time': bar_time,
        'epic': epic,
        'reason': reason,
        'at': _utc_now_iso(),
        'horizon_bars': BLOCK_HORIZON_BARS,
    }
    bag['pending_blocks'] = (bag.get('pending_blocks') or []) + [pending]
    save_ledger(store, bag)
    return {'ok': True, 'pending': pending}


def maybe_resolve_blocks(store, *, candles=None, mid=None, epic=None) -> dict:
    """Resolve blocked OPEN shadows from later Capital mid / M1 closes."""
    if store is None:
        return {'ok': False, 'resolved': 0}
    bag = load_ledger(store)
    pending = list(bag.get('pending_blocks') or [])
    if not pending:
        return {'ok': True, 'resolved': 0}
    now_mid = _finite(mid)
    closes = []
    for c in candles or []:
        if isinstance(c, dict):
            v = _finite(c.get('close') if 'close' in c else c.get('c'))
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            v = _finite(c[4])
        else:
            v = None
        if v is not None:
            closes.append(v)
    still = []
    resolved = []
    for p in pending:
        if epic and p.get('epic') and str(p.get('epic')) != str(epic):
            still.append(p)
            continue
        entry = _finite(p.get('mid'))
        if entry is None:
            continue
        # Prefer horizon close from candle tape; else live mid after enough age.
        exit_px = None
        need = int(p.get('horizon_bars') or BLOCK_HORIZON_BARS)
        if len(closes) >= need + 1:
            exit_px = closes[-1]
        elif now_mid is not None:
            # Age gate: only if bar_time is at least ~need minutes old.
            bt = p.get('bar_time')
            try:
                from .market_context import candle_time
                t0 = candle_time(bt)
                age_min = (datetime.now(timezone.utc) - t0).total_seconds() / 60.0 if t0 else 0
            except Exception:
                age_min = need + 1
            if age_min >= need:
                exit_px = now_mid
        if exit_px is None:
            still.append(p)
            continue
        prop = p.get('proposed')
        move = exit_px - entry
        # If proposed BUY and price rose → we missed a winner (harmful block).
        proposed_pnl_proxy = move if prop == 'BUY' else -move
        missed = proposed_pnl_proxy > 0
        saved = proposed_pnl_proxy < 0
        sample = {
            'kind': 'block_resolved',
            'helpful': saved,  # blocking a loser = helpful
            'missed': missed,
            'saved': saved,
            'pnl': proposed_pnl_proxy,  # counterfactual £ proxy (price units)
            'direction': prop,
            'consensus': p.get('consensus'),
            'epic': p.get('epic') or epic,
            'at': _utc_now_iso(),
            'entry_mid': entry,
            'exit_mid': exit_px,
        }
        resolved.append(sample)
    if not resolved:
        return {'ok': True, 'resolved': 0}
    bag['pending_blocks'] = still
    bag['samples'] = (bag.get('samples') or []) + resolved
    bag['stats'] = _recompute_stats(bag['samples'])
    adapt = adapt_from_ledger(store, bag)
    bag['adapt'] = adapt
    save_ledger(store, bag)
    try:
        store.event('learning', 'Flow consensus resolved blocked opens', {
            'resolved': len(resolved), 'adapt': adapt.get('changed'),
        })
    except Exception:
        pass
    return {'ok': True, 'resolved': len(resolved), 'samples': resolved, 'adapt': adapt}


def adapt_from_ledger(store, bag=None) -> dict:
    """Tighten/loosen flow influence from evidence — affects future OPEN + system knobs."""
    from . import self_params
    bag = bag if isinstance(bag, dict) else load_ledger(store)
    stats = bag.get('stats') or _recompute_stats(bag.get('samples') or [])
    out = {
        'changed': False,
        'reason': 'insufficient_samples',
        'nudge': None,
        'block_fights': None,
        'block_min_agree': None,
        'stats': stats,
    }
    n = int(stats.get('n') or 0)
    if store is None or n < ADAPT_MIN_N:
        return out
    params = self_params.load(store)
    nudge = float(params.get(STORE_NUDGE_KEY, DEFAULT_NUDGE))
    block = int(params.get(STORE_BLOCK_KEY, DEFAULT_BLOCK_FIGHTS))
    block_min = int(params.get(STORE_BLOCK_MIN_KEY, DEFAULT_BLOCK_MIN_AGREE))
    before = (nudge, block, block_min)
    helpful = int(stats.get('helpful') or 0)
    harmful = int(stats.get('harmful') or 0)
    mean_a = stats.get('mean_aligned_pnl')
    saved = int(stats.get('block_saved') or 0)
    missed = int(stats.get('block_missed') or 0)

    reasons = []
    # Consensus helping £ → stronger voice on OPEN.
    if helpful >= harmful + 2 and helpful + harmful >= ADAPT_MIN_N:
        nudge = min(0.35, nudge + 0.02)
        reasons.append('helpful_edge')
    elif harmful >= helpful + 2 and helpful + harmful >= ADAPT_MIN_N:
        nudge = max(0.05, nudge - 0.03)
        reasons.append('harmful_edge')
    if mean_a is not None and mean_a > 0 and int(stats.get('aligned_wins') or 0) >= 3:
        nudge = min(0.35, nudge + 0.01)
        reasons.append('aligned_mean_pos')
    elif mean_a is not None and mean_a < 0 and int(stats.get('aligned_losses') or 0) >= 3:
        nudge = max(0.05, nudge - 0.02)
        reasons.append('aligned_mean_neg')

    # Block quality → keep / relax / require more agree.
    if saved + missed >= ADAPT_MIN_N:
        if saved > missed + 1:
            block = 1
            block_min = max(2, block_min - 0)  # keep at least 2
            reasons.append('blocks_saving')
        elif missed > saved + 1:
            block_min = min(3, block_min + 1)
            reasons.append('blocks_missing_winners')
            if missed >= saved * 2 and missed >= 4:
                # Too many missed winners → stop hard blocks, keep soft nudge.
                block = 0
                reasons.append('disable_hard_block')

    after = (round(nudge, 4), int(block), int(block_min))
    if after == (round(before[0], 4), int(before[1]), int(before[2])):
        out['reason'] = 'no_change:' + (','.join(reasons) or 'stable')
        out['nudge'] = after[0]
        out['block_fights'] = after[1]
        out['block_min_agree'] = after[2]
        return out

    params[STORE_NUDGE_KEY] = after[0]
    params[STORE_BLOCK_KEY] = after[1]
    params[STORE_BLOCK_MIN_KEY] = after[2]
    self_params.save(
        store, params,
        reason='flow_consensus_adapt:' + ','.join(reasons) or 'flow_adapt')
    out.update({
        'changed': True,
        'reason': ','.join(reasons) or 'adapted',
        'nudge': after[0],
        'block_fights': after[1],
        'block_min_agree': after[2],
        'before': {
            'nudge': before[0], 'block_fights': before[1],
            'block_min_agree': before[2],
        },
    })
    try:
        store.event('learning', 'Flow consensus adapted system knobs', out)
    except Exception:
        pass
    return out


def learn_snapshot(store) -> dict:
    """Operator / desk view of flow learning state."""
    bag = load_ledger(store)
    stats = bag.get('stats') or {}
    adapt = bag.get('adapt') or {}
    try:
        from . import self_params
        nudge = self_params.get(store, STORE_NUDGE_KEY, DEFAULT_NUDGE)
        block = self_params.get(store, STORE_BLOCK_KEY, DEFAULT_BLOCK_FIGHTS)
        block_min = self_params.get(
            store, STORE_BLOCK_MIN_KEY, DEFAULT_BLOCK_MIN_AGREE)
    except Exception:
        nudge, block, block_min = DEFAULT_NUDGE, DEFAULT_BLOCK_FIGHTS, DEFAULT_BLOCK_MIN_AGREE
    thr = _cfg_pillar_thresholds(store)
    return {
        'ok': True,
        'n': int(stats.get('n') or 0),
        'helpful': int(stats.get('helpful') or 0),
        'harmful': int(stats.get('harmful') or 0),
        'aligned_wins': int(stats.get('aligned_wins') or 0),
        'aligned_losses': int(stats.get('aligned_losses') or 0),
        'block_saved': int(stats.get('block_saved') or 0),
        'block_missed': int(stats.get('block_missed') or 0),
        'mean_aligned_pnl': stats.get('mean_aligned_pnl'),
        'pending_blocks': len(bag.get('pending_blocks') or []),
        'nudge': nudge,
        'block_fights': int(block),
        'block_min_agree': int(block_min),
        'pillar_thresholds': thr,
        'last_adapt': adapt,
    }
