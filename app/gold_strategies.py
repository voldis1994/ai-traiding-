"""GOLD audit strategies — policies distilled from labeled history mistakes.

Evidence (client_1 GOLD backup audit, ~293 closed trades, sum £ −19.90):
  * Against-structure entries (BUY on *_DOWN / SELL on *_UP) → 71 trades,
    WR ~18%, £ −24.58 — **hard-block** (structure_align)
  * Counterfactual: keep only aligned/unknown → 222 trades, £ **+4.68**
  * RR < 1.0 skips were net £ +3.94 on the same set — do **not** hard-block
    by default; teach still labels weak RR; live brackets already extend TP
  * BUY underperformed in DOWN frames → buy_strict_down as belt-and-braces

Strategies (OPEN gates when enabled — not a new neural architecture):
  1. ``structure_align`` — block side that fights live BOS/CHoCH structure
  2. ``min_rr`` — optional; off by default (see evidence above)
  3. ``buy_strict_down`` — BUY in DOWN frames needs clearer score edge

Applies to GOLD / XAU only. ``teach_label_from_outcome`` turns closed trades
into learning hints so the same machine develops from known failure modes.
"""
from __future__ import annotations

import math
import re

STORE_LAST_KEY = 'gold_strategies_last_v1'

DEFAULTS = {
    'enabled': 1,
    'structure_align': 1,
    # min_rr hard gate OFF by default: on real GOLD closes, RR<1 skips were
    # net *positive* (+£3.94) while against-structure skips were −£24.58.
    # RR<1 still feeds teach_label_from_outcome; live brackets already extend TP.
    'min_rr': 0,
    'buy_strict_down': 1,
    'min_rr_floor': 1.0,
    # BUY score_edge must clear this in TREND_DOWN / BREAKOUT_DOWN.
    'buy_down_min_edge': 0.12,
    # Soft: CHOP/RANGE → prefer HOLD unless edge is strong (optional gate).
    'chop_hold': 0,
    'chop_min_edge': 0.18,
}

UP_STRUCTURES = frozenset({'TREND_UP', 'BREAKOUT_UP'})
DOWN_STRUCTURES = frozenset({'TREND_DOWN', 'BREAKOUT_DOWN'})
CHOPPY = frozenset({'CHOP', 'RANGE'})


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def applies_to_epic(epic) -> bool:
    """True for GOLD / XAU instruments only."""
    code = str(epic or '').upper()
    if not code:
        return False
    if re.search(r'(?<![A-Z])GOLD(?![A-Z])', code):
        return True
    # XAU, XAUUSD, CS.D.XAUUSD.CFD.IP — not XAUD (Aussie) false friend: require
    # XAU as its own token or XAU followed by a quote currency / separator.
    if re.search(r'(?<![A-Z])XAU(?:USD|\/USD|_USD)?(?![A-Z])', code):
        return True
    if 'XAUUSD' in code or code.endswith('.XAU') or '.XAU.' in code:
        return True
    return False


def _params(store=None) -> dict:
    out = dict(DEFAULTS)
    if store is None:
        return out
    try:
        from .self_params import get as sp_get
        out['enabled'] = int(sp_get(store, 'gold_strat_enabled', DEFAULTS['enabled']))
        out['structure_align'] = int(
            sp_get(store, 'gold_strat_structure_align', DEFAULTS['structure_align']))
        out['min_rr'] = int(sp_get(store, 'gold_strat_min_rr', DEFAULTS['min_rr']))
        out['buy_strict_down'] = int(
            sp_get(store, 'gold_strat_buy_strict_down', DEFAULTS['buy_strict_down']))
        out['min_rr_floor'] = float(
            sp_get(store, 'gold_strat_min_rr_floor', DEFAULTS['min_rr_floor']))
        out['buy_down_min_edge'] = float(
            sp_get(store, 'gold_strat_buy_down_min_edge', DEFAULTS['buy_down_min_edge']))
        out['chop_hold'] = int(sp_get(store, 'gold_strat_chop_hold', DEFAULTS['chop_hold']))
        out['chop_min_edge'] = float(
            sp_get(store, 'gold_strat_chop_min_edge', DEFAULTS['chop_min_edge']))
    except Exception:
        pass
    out['min_rr_floor'] = max(1.0, min(3.0, float(out['min_rr_floor'])))
    out['buy_down_min_edge'] = max(0.02, min(0.45, float(out['buy_down_min_edge'])))
    out['chop_min_edge'] = max(0.05, min(0.50, float(out['chop_min_edge'])))
    return out


def classify_structure(candles, *, path_bars=80, pre_bars=40) -> dict:
    """Live structure for alignment — prefer BOS/CHoCH bias (entry-consistent).

    Post-trade ``structure_shadow`` labels are for teaching; at OPEN we use the
    same swing BOS read the robot already trusts for structure memory.
    """
    rows = [r for r in (candles or []) if isinstance(r, dict)]
    if len(rows) < 8:
        return {'structure': 'CHOP', 'ready': False, 'reason': 'too_few_bars'}
    path = rows[-max(8, int(path_bars)):]
    try:
        from . import structure_memory
        bos = structure_memory.bos_choch(path, left=2, right=2)
        event = str((bos or {}).get('event') or '').upper()
        bias = str((bos or {}).get('bias') or '').upper()
        if event == 'BOS_UP':
            st = 'BREAKOUT_UP'
        elif event == 'BOS_DOWN':
            st = 'BREAKOUT_DOWN'
        elif event == 'CHOCH_UP':
            st = 'TREND_UP'
        elif event == 'CHOCH_DOWN':
            st = 'TREND_DOWN'
        elif bias == 'UP':
            st = 'TREND_UP'
        elif bias == 'DOWN':
            st = 'TREND_DOWN'
        elif bias == 'RANGE':
            st = 'RANGE'
        else:
            st = 'CHOP'
        return {
            'structure': st, 'ready': True, 'reason': event or f'bias_{bias or "none"}',
            'bos': {'event': event or None, 'bias': bias or None},
        }
    except Exception as e:
        return {'structure': 'CHOP', 'ready': False, 'reason': f'bos_err:{type(e).__name__}'}


def structure_allows(action, structure) -> dict:
    """Strategy 1: do not fight live structure."""
    action = str(action or '').upper()
    st = str(structure or '').upper()
    if action not in ('BUY', 'SELL'):
        return {'ok': True, 'reason': 'not_directional'}
    if st in DOWN_STRUCTURES and action == 'BUY':
        return {
            'ok': False,
            'reason': f'gold_structure_align: BUY blocked on {st}',
            'strategy': 'structure_align',
        }
    if st in UP_STRUCTURES and action == 'SELL':
        return {
            'ok': False,
            'reason': f'gold_structure_align: SELL blocked on {st}',
            'strategy': 'structure_align',
        }
    return {'ok': True, 'reason': 'aligned_or_neutral', 'strategy': 'structure_align'}


def rr_allows(stop_distance, profit_distance, *, floor=1.0) -> dict:
    """Strategy 2: refuse weak reward:risk geometry."""
    sd = _finite(stop_distance)
    pd = _finite(profit_distance)
    need = max(1.0, float(floor))
    if sd is None or pd is None or sd <= 0 or pd <= 0:
        return {
            'ok': False,
            'reason': 'gold_min_rr: missing stop/profit distances',
            'strategy': 'min_rr',
            'rr': None,
            'floor': need,
        }
    rr = pd / sd
    if rr + 1e-12 < need:
        return {
            'ok': False,
            'reason': f'gold_min_rr: RR {rr:.3f} < {need:.3f}',
            'strategy': 'min_rr',
            'rr': float(rr),
            'floor': need,
        }
    return {
        'ok': True,
        'reason': 'rr_ok',
        'strategy': 'min_rr',
        'rr': float(rr),
        'floor': need,
    }


def buy_strict_allows(action, structure, *, score_edge=None, min_edge=0.12) -> dict:
    """Strategy 3: BUY in DOWN frames needs a stronger neural edge."""
    action = str(action or '').upper()
    st = str(structure or '').upper()
    if action != 'BUY' or st not in DOWN_STRUCTURES:
        return {'ok': True, 'reason': 'not_buy_in_down', 'strategy': 'buy_strict_down'}
    edge = _finite(score_edge)
    need = float(min_edge)
    if edge is None or edge < need:
        return {
            'ok': False,
            'reason': (
                f'gold_buy_strict_down: BUY edge '
                f'{edge if edge is not None else "n/a"} < {need} on {st}'
            ),
            'strategy': 'buy_strict_down',
            'score_edge': edge,
            'min_edge': need,
        }
    return {
        'ok': True,
        'reason': 'buy_edge_ok',
        'strategy': 'buy_strict_down',
        'score_edge': edge,
        'min_edge': need,
    }


def chop_allows(action, structure, *, score_edge=None, min_edge=0.18) -> dict:
    """Optional: CHOP/RANGE needs strong edge or HOLD."""
    st = str(structure or '').upper()
    if st not in CHOPPY:
        return {'ok': True, 'reason': 'not_chop', 'strategy': 'chop_hold'}
    edge = _finite(score_edge)
    need = float(min_edge)
    if edge is None or edge < need:
        return {
            'ok': False,
            'reason': f'gold_chop_hold: weak edge on {st}',
            'strategy': 'chop_hold',
            'score_edge': edge,
            'min_edge': need,
        }
    return {'ok': True, 'reason': 'chop_edge_ok', 'strategy': 'chop_hold'}


def evaluate(
    *,
    epic,
    action,
    candles=None,
    structure=None,
    stop_distance=None,
    profit_distance=None,
    score_edge=None,
    store=None,
    brackets=None,
) -> dict:
    """Run enabled GOLD strategies. ``allowed=False`` → caller should HOLD."""
    cfg = _params(store)
    out = {
        'allowed': True,
        'reason': 'pass',
        'epic': epic,
        'action': str(action or '').upper(),
        'applies': False,
        'structure': None,
        'checks': [],
        'cfg': {k: cfg[k] for k in (
            'enabled', 'structure_align', 'min_rr', 'buy_strict_down',
            'min_rr_floor', 'buy_down_min_edge', 'chop_hold')},
    }
    if not applies_to_epic(epic):
        out['reason'] = 'not_gold'
        return out
    out['applies'] = True
    if not int(cfg.get('enabled') or 0):
        out['reason'] = 'disabled'
        return out
    action = out['action']
    if action not in ('BUY', 'SELL'):
        out['reason'] = 'not_directional'
        return out

    st = str(structure or '').upper() if structure else None
    if not st or st == 'UNKNOWN':
        frame = classify_structure(candles)
        st = str((frame or {}).get('structure') or 'CHOP').upper()
        out['frame'] = frame
    out['structure'] = st

    br = brackets if isinstance(brackets, dict) else {}
    sd = stop_distance if stop_distance is not None else br.get('stop_distance')
    pd = profit_distance if profit_distance is not None else br.get('profit_distance')

    checks = []
    if int(cfg.get('structure_align') or 0):
        checks.append(structure_allows(action, st))
    if int(cfg.get('min_rr') or 0):
        checks.append(rr_allows(sd, pd, floor=cfg['min_rr_floor']))
    if int(cfg.get('buy_strict_down') or 0):
        # Only meaningful when structure_align is off; with align on, BUY+DOWN
        # is already blocked. Keep as belt-and-braces + score threshold if align off.
        checks.append(buy_strict_allows(
            action, st, score_edge=score_edge, min_edge=cfg['buy_down_min_edge']))
    if int(cfg.get('chop_hold') or 0):
        checks.append(chop_allows(
            action, st, score_edge=score_edge, min_edge=cfg['chop_min_edge']))

    out['checks'] = checks
    for ch in checks:
        if not ch.get('ok', True):
            out['allowed'] = False
            out['reason'] = ch.get('reason') or 'gold_strategy_block'
            out['blocked_by'] = ch.get('strategy')
            _persist_last(store, out)
            return out
    out['reason'] = 'pass'
    _persist_last(store, out)
    return out


def _persist_last(store, payload: dict) -> None:
    if store is None:
        return
    try:
        slim = {
            'allowed': bool(payload.get('allowed')),
            'reason': payload.get('reason'),
            'structure': payload.get('structure'),
            'action': payload.get('action'),
            'blocked_by': payload.get('blocked_by'),
            'checks': [
                {k: c.get(k) for k in ('ok', 'reason', 'strategy', 'rr', 'score_edge')
                 if k in c}
                for c in (payload.get('checks') or [])
            ],
        }
        store.set(STORE_LAST_KEY, slim)
    except Exception:
        pass


def teach_label_from_outcome(
    *,
    action,
    structure,
    pnl,
    stop_distance=None,
    profit_distance=None,
) -> dict:
    """Map a closed GOLD trade to a teach hint for the learning loop.

    Returns ``label`` in {BUY, SELL, HOLD} plus ``mode`` / ``weight`` for callers
    that feed AdaptiveBrain / structure_shadow teachers.
    """
    action = str(action or '').upper()
    st = str(structure or '').upper()
    pnl_v = _finite(pnl) or 0.0
    sd = _finite(stop_distance)
    pd = _finite(profit_distance)
    rr = (pd / sd) if sd and pd and sd > 0 else None

    against = (
        (st in DOWN_STRUCTURES and action == 'BUY')
        or (st in UP_STRUCTURES and action == 'SELL')
    )
    if against and pnl_v <= 0:
        # Known failure mode — teach HOLD (or opposite when path clearly favored it).
        opposite = 'SELL' if action == 'BUY' else 'BUY'
        return {
            'label': 'HOLD',
            'alt_label': opposite,
            'mode': 'gold_against_structure_loss',
            'weight': 3,
            'reason': f'{action} against {st} lost — prefer HOLD/{opposite}',
        }
    if rr is not None and rr < 1.0 and pnl_v <= 0:
        return {
            'label': 'HOLD',
            'alt_label': None,
            'mode': 'gold_rr_under_one_loss',
            'weight': 2,
            'reason': f'RR {rr:.2f}<1 loss — geometry teach HOLD',
        }
    if not against and pnl_v > 0 and action in ('BUY', 'SELL'):
        return {
            'label': action,
            'alt_label': None,
            'mode': 'gold_aligned_win',
            'weight': 2,
            'reason': f'aligned {action} on {st or "UNKNOWN"} win — reinforce',
        }
    if pnl_v > 0 and action in ('BUY', 'SELL'):
        return {
            'label': action,
            'alt_label': None,
            'mode': 'gold_win',
            'weight': 1,
            'reason': 'win reinforce',
        }
    if pnl_v < 0 and action in ('BUY', 'SELL'):
        return {
            'label': 'HOLD',
            'alt_label': None,
            'mode': 'gold_loss',
            'weight': 1,
            'reason': 'loss → HOLD caution',
        }
    return {
        'label': 'HOLD',
        'alt_label': None,
        'mode': 'gold_scratch',
        'weight': 1,
        'reason': 'scratch/flat',
    }


def replay_paper(
    candles,
    *,
    train_frac=0.7,
    spread=0.5,
    tick=0.01,
    min_distance=0.3,
    need_bars=720,
    use_strategies=True,
    store=None,
) -> dict:
    """Chronological GOLD paper replay: MTF/BOS side + swing brackets ± strategies.

    Train fraction is unused for fitting (strategies are fixed from audit); it
    only defines the holdout window so results stay comparable to the offline test.
    """
    from collections import Counter
    from . import bracket_learn, mtf_momentum, structure_memory

    rows = [r for r in (candles or []) if isinstance(r, dict)]
    n = len(rows)
    split = int(n * float(train_frac))
    holdout = rows[split:]
    wins = losses = 0
    pnl_r = []
    reasons = Counter()
    i = 0
    base = split
    while i < len(holdout) - 5:
        abs_i = base + i
        window = rows[max(0, abs_i - need_bars): abs_i + 1]
        if len(window) < need_bars:
            i += 1
            continue
        mid = _finite(window[-1].get('close'))
        if mid is None:
            i += 1
            continue
        action = None
        try:
            mtf = mtf_momentum.analyze(window, spread=spread, epic='GOLD')
            pref = str((mtf or {}).get('preferred') or 'HOLD').upper()
            if pref in ('BUY', 'SELL'):
                action = pref
                reasons['mtf'] += 1
        except Exception:
            reasons['mtf_fail'] += 1
        if action is None:
            bos = structure_memory.bos_choch(window)
            bias = str((bos or {}).get('bias') or '').upper()
            if bias == 'UP':
                action = 'BUY'
                reasons['bos_up'] += 1
            elif bias == 'DOWN':
                action = 'SELL'
                reasons['bos_down'] += 1
            else:
                reasons['no_side'] += 1
                i += 1
                continue

        zone = bracket_learn.zone_at_open(window, multi_tf=True)
        atr = mtf_momentum.atr_wilder(window, 14)
        br = bracket_learn.decide_brackets_from_ohlc(
            zone, tick=tick, spread=spread, min_distance=min_distance, tighten=1.0,
            entry=mid, action=action, candles=window, atr=atr, require_swing=True,
            store=store)
        if not br.get('ready'):
            reasons[f"skip_{br.get('reason') or 'unready'}"] += 1
            i += 1
            continue

        if use_strategies:
            gate = evaluate(
                epic='GOLD', action=action, candles=window,
                stop_distance=br.get('stop_distance'),
                profit_distance=br.get('profit_distance'),
                score_edge=0.20,  # paper has no neural scores — pass min edge
                store=store, brackets=br)
            if not gate.get('allowed', True):
                reasons[f"strat_{gate.get('blocked_by') or 'block'}"] += 1
                i += 1
                continue

        stop = float(br['stop_level'])
        tp = float(br['profit_level'])
        sd = float(br['stop_distance'])
        pd = float(br['profit_distance'])
        if sd <= 0 or pd <= 0:
            i += 1
            continue
        hit = None
        bars_held = 0
        for j in range(abs_i + 1, min(len(rows), abs_i + 1 + 240)):
            bar = rows[j]
            bars_held += 1
            hi = float(bar['high'])
            lo = float(bar['low'])
            if action == 'BUY':
                if lo <= stop:
                    hit = 'SL'
                    break
                if hi >= tp:
                    hit = 'TP'
                    break
            else:
                if hi >= stop:
                    hit = 'SL'
                    break
                if lo <= tp:
                    hit = 'TP'
                    break
        if hit is None:
            reasons['timeout'] += 1
            i += max(5, bars_held // 2)
            continue
        r = (pd / sd if hit == 'TP' else -1.0) - (spread / sd)
        pnl_r.append(r)
        if r >= 0:
            wins += 1
        else:
            losses += 1
        reasons[f'hit_{hit}'] += 1
        i += max(1, bars_held)

    total = wins + losses
    return {
        'strategies_on': bool(use_strategies),
        'trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / total, 4) if total else None,
        'sum_R': round(sum(pnl_r), 3) if pnl_r else 0.0,
        'avg_R': round(sum(pnl_r) / total, 4) if total else None,
        'reasons': dict(reasons.most_common(20)),
        'holdout_bars': len(holdout),
        'train_bars': split,
    }
