"""Shadow the OPPOSITE side when LIVE HOLDs a directional proposal.

Problem: weak edge / policy veto / bad proposal → HOLD. Losses already teach
opposite-vs-HOLD after fills, but *abstained* bars never get a teacher — so the
machine can drift toward HOLD-on-every-move.

Fix (shadow only — never opens LIVE):
  1. When final action is HOLD but neural proposal was BUY or SELL, enqueue a
     pending case with the same SL/TP distances that OPEN would have used.
  2. After enough M1 bars, replay proposal AND opposite on that path.
  3. If opposite hits TP (and proposal did not win), teach the opposite label
     lightly so scores can clear the fixed 0.06 floor next time — strategy
     evidence, not a silent LIVE flip.

Entry floor stays 0.06. This only improves *who* clears the bar.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .shadow_paper import simulate_action_on_path

PENDING_KEY = 'hold_shadow_pending_v1'
STATS_KEY = 'hold_shadow_stats_v1'
HISTORY_KEY = 'hold_shadow_history_v1'

HORIZON_BARS = 45
MAX_PENDING = 12
MAX_HISTORY = 200
# When opposite TP rate is high, expose a strategy hint (evolve / UI).
STRATEGY_HINT_MIN = 20
STRATEGY_HINT_RATE = 0.55

_ACTION_LABEL = {'BUY': 0, 'SELL': 1, 'HOLD': 2}


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def opposite_of(side: str) -> str | None:
    if side == 'BUY':
        return 'SELL'
    if side == 'SELL':
        return 'BUY'
    return None


def default_stats():
    return {
        'enqueued': 0,
        'resolved': 0,
        'opposite_hit_tp': 0,
        'proposal_hit_tp': 0,
        'both_lost': 0,
        'taught_opposite': 0,
        'skipped': 0,
        'strategy_hint': None,
        'opposite_tp_rate': None,
    }


def load_stats(store) -> dict:
    raw = store.get(STATS_KEY) if store is not None else None
    out = default_stats()
    if isinstance(raw, dict):
        out.update({k: raw.get(k, out[k]) for k in out})
    return out


def save_stats(store, stats: dict) -> dict:
    clean = default_stats()
    if isinstance(stats, dict):
        clean.update({k: stats.get(k, clean[k]) for k in clean})
    for k in ('enqueued', 'resolved', 'opposite_hit_tp', 'proposal_hit_tp',
              'both_lost', 'taught_opposite', 'skipped'):
        try:
            clean[k] = max(0, int(clean[k] or 0))
        except (TypeError, ValueError):
            clean[k] = 0
    if store is not None:
        store.set(STATS_KEY, clean)
    return clean


def load_pending(store) -> list:
    raw = store.get(PENDING_KEY) if store is not None else None
    return list(raw) if isinstance(raw, list) else []


def save_pending(store, rows: list) -> list:
    keep = list(rows or [])[-MAX_PENDING:]
    if store is not None:
        store.set(PENDING_KEY, keep)
    return keep


def evaluate_sides(*, proposal, bars, spread=0., stop_distance=None,
                   profit_distance=None, tick=None, min_distance=None,
                   trail=True):
    """Replay proposal + opposite under identical bracket/cost rules."""
    opp = opposite_of(proposal)
    if opp is None or not bars:
        return None
    prop_sim = simulate_action_on_path(
        proposal, bars, spread=spread, stop_distance=stop_distance,
        profit_distance=profit_distance, tick=tick, min_distance=min_distance,
        trail=trail)
    opp_sim = simulate_action_on_path(
        opp, bars, spread=spread, stop_distance=stop_distance,
        profit_distance=profit_distance, tick=tick, min_distance=min_distance,
        trail=trail)
    if not prop_sim or not opp_sim:
        return None
    prop_pnl = _finite(prop_sim.get('pnl'))
    opp_pnl = _finite(opp_sim.get('pnl'))
    if prop_pnl is None or opp_pnl is None:
        return None
    return {
        'proposal': proposal,
        'opposite': opp,
        'proposal_sim': prop_sim,
        'opposite_sim': opp_sim,
        'proposal_hit_tp': prop_sim.get('reason') == 'tp',
        'opposite_hit_tp': opp_sim.get('reason') == 'tp',
        'proposal_pnl': float(prop_pnl),
        'opposite_pnl': float(opp_pnl),
        # Teach opposite only when it clearly won the path (TP) and proposal
        # did not — avoids reinforcing noise / both-side wins.
        'teach_opposite': bool(
            opp_sim.get('reason') == 'tp' and prop_sim.get('reason') != 'tp'),
    }


def enqueue(store, *, proposal, reason, bar_time, epic, entry, spread,
            stop_distance, profit_distance, tick, min_distance, transition,
            scores=None, trail=True):
    """Queue a HOLD abstention for later opposite-path shadow."""
    if proposal not in ('BUY', 'SELL'):
        return None
    entry_f = _finite(entry)
    if entry_f is None or entry_f <= 0:
        return None
    if not isinstance(transition, dict):
        return None
    pending = load_pending(store)
    # One pending per bar_time — replace if re-evaluated.
    pending = [p for p in pending if str(p.get('bar_time')) != str(bar_time)]
    row = {
        'id': f'hs-{len(pending)+1}-{str(bar_time or "")[-12:]}',
        'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'proposal': proposal,
        'opposite': opposite_of(proposal),
        'reason': reason,
        'bar_time': bar_time,
        'epic': epic,
        'entry': float(entry_f),
        'spread': float(_finite(spread) or 0.),
        'stop_distance': _finite(stop_distance),
        'profit_distance': _finite(profit_distance),
        'tick': _finite(tick),
        'min_distance': _finite(min_distance),
        'trail': bool(trail),
        'transition': transition,
        'scores': scores if isinstance(scores, dict) else None,
        'bars_seen': 0,
    }
    pending.append(row)
    save_pending(store, pending)
    stats = load_stats(store)
    stats['enqueued'] = int(stats.get('enqueued') or 0) + 1
    save_stats(store, stats)
    return row


def _append_history(store, row: dict):
    hist = list(store.get(HISTORY_KEY) or []) if store is not None else []
    if not isinstance(hist, list):
        hist = []
    hist.append(row)
    if store is not None:
        store.set(HISTORY_KEY, hist[-MAX_HISTORY:])


def _update_strategy_hint(stats: dict) -> dict:
    n = int(stats.get('resolved') or 0)
    hits = int(stats.get('opposite_hit_tp') or 0)
    rate = (hits / n) if n else None
    stats['opposite_tp_rate'] = float(rate) if rate is not None else None
    if n >= STRATEGY_HINT_MIN and rate is not None and rate >= STRATEGY_HINT_RATE:
        stats['strategy_hint'] = (
            f'hold_shadow: opposite hit TP on {hits}/{n} ({rate:.0%}) abstentions '
            f'— prefer reverse when proposal is weak rather than permanent HOLD')
    else:
        stats['strategy_hint'] = None
    return stats


def resolve_one(case: dict, bars: list) -> dict | None:
    """Resolve one pending case against subsequent OHLC bars."""
    if not case or not bars:
        return None
    return evaluate_sides(
        proposal=case.get('proposal'),
        bars=bars,
        spread=case.get('spread') or 0.,
        stop_distance=case.get('stop_distance'),
        profit_distance=case.get('profit_distance'),
        tick=case.get('tick'),
        min_distance=case.get('min_distance'),
        trail=case.get('trail', True))


def resolve_ready(store, *, candles_after, learn_callback=None,
                  horizon=HORIZON_BARS) -> list:
    """Resolve pending cases that have enough path; optionally teach opposite.

    ``candles_after(bar_time, epic)`` → list of bar dicts (mid/high/low/spread)
    starting at/after the abstention bar.
    ``learn_callback(label, transition, mode, detail)`` applies one light update.
    """
    pending = load_pending(store)
    if not pending:
        return []
    still = []
    outcomes = []
    stats = load_stats(store)
    for case in pending:
        bars = []
        try:
            bars = list(candles_after(case.get('bar_time'), case.get('epic')) or [])
        except Exception:
            bars = []
        if len(bars) < max(8, int(horizon) // 3):
            # Not enough path yet — keep waiting (cap age via bars_seen proxy).
            case = dict(case)
            case['bars_seen'] = len(bars)
            still.append(case)
            continue
        path = bars[:int(horizon)]
        detail = resolve_one(case, path)
        if detail is None:
            stats['skipped'] = int(stats.get('skipped') or 0) + 1
            continue
        stats['resolved'] = int(stats.get('resolved') or 0) + 1
        if detail['opposite_hit_tp']:
            stats['opposite_hit_tp'] = int(stats.get('opposite_hit_tp') or 0) + 1
        if detail['proposal_hit_tp']:
            stats['proposal_hit_tp'] = int(stats.get('proposal_hit_tp') or 0) + 1
        if (not detail['opposite_hit_tp'] and not detail['proposal_hit_tp']
                and detail['opposite_pnl'] <= 0 and detail['proposal_pnl'] <= 0):
            stats['both_lost'] = int(stats.get('both_lost') or 0) + 1
        taught = False
        mode = 'hold_shadow_observe'
        if detail['teach_opposite']:
            mode = f"hold_missed_opposite_tp_{detail['opposite'].lower()}"
            if learn_callback is not None and case.get('transition'):
                label = _ACTION_LABEL[detail['opposite']]
                try:
                    learn_callback(label, case['transition'], mode, detail)
                    taught = True
                    stats['taught_opposite'] = int(stats.get('taught_opposite') or 0) + 1
                except Exception:
                    taught = False
        out = {
            'case_id': case.get('id'),
            'bar_time': case.get('bar_time'),
            'proposal': detail['proposal'],
            'opposite': detail['opposite'],
            'mode': mode,
            'taught': taught,
            'opposite_hit_tp': detail['opposite_hit_tp'],
            'proposal_hit_tp': detail['proposal_hit_tp'],
            'opposite_pnl': detail['opposite_pnl'],
            'proposal_pnl': detail['proposal_pnl'],
            'path_bars': len(path),
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
        outcomes.append(out)
        _append_history(store, out)
    stats = _update_strategy_hint(stats)
    save_stats(store, stats)
    save_pending(store, still)
    return outcomes


def strategy_evidence(store) -> dict:
    """Compact evidence for evolve / snapshot — not a LIVE flip switch."""
    stats = load_stats(store)
    return {
        'opposite_tp_rate': stats.get('opposite_tp_rate'),
        'resolved': stats.get('resolved'),
        'taught_opposite': stats.get('taught_opposite'),
        'strategy_hint': stats.get('strategy_hint'),
        'pending': len(load_pending(store)),
    }
