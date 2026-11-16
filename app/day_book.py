"""Day book — robot-governed book for the trading day.

Operator chooses the instrument (manual mandate). The robot decides:

  • how much £ profit is *enough* today (frac of equity → target)
  • how much £ loss is *enough* to stop adding risk (frac of equity → budget)
  • cash risk per new OPEN from equity
  • whether another concurrent position is allowed
  • lot size from remaining risk budget / stop (mandate size is a seed grid)
  • soft anti-HOLD when directional conviction is clear and the day still needs work

Goal: finish the UTC day at **plus or break-even**. Early losses are expected;
the book learns size/targets from £ — never a hardcoded "DOWN → no BUY".
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR

from .math_rules import (
    position_size as risk_position_size, cash_risk, dealing_size_grid,
    snap_deal_size,
)

STORE_KEY = 'day_book_v1'
DAILY_KEY = 'day_book_daily_v1'
LAST_PLAN_KEY = 'day_book_last_plan_v1'

# Soft phase labels — advice, not frozen human scripts.
PHASE_HUNT = 'HUNT'          # day behind target — trade / add when edge
PHASE_PROTECT = 'PROTECT'    # day at/above target — bank, prefer no new risk
PHASE_DEFEND = 'DEFEND'      # day at loss budget — no new opens
PHASE_DONE = 'DONE'          # flat and day +/BE — idle until next day


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


def _utc_date(now=None):
    current = now or _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date().isoformat()


def _dec(x, default=None):
    try:
        d = Decimal(str(x))
        if d.is_finite():
            return d
    except Exception:
        pass
    return default


def load_params(store):
    """Robot knobs (seeds in self_params; evolve from £)."""
    sp = _cfg(store)
    return {
        'profit_target_frac': float(sp.get('day_profit_target_frac', 0.01)),
        'loss_budget_frac': float(sp.get('day_loss_budget_frac', 0.02)),
        'risk_per_trade_frac': float(sp.get('day_risk_per_trade_frac', 0.005)),
        'be_band_frac': float(sp.get('day_be_band_frac', 0.001)),
        'max_positions': int(sp.get('day_max_positions', 3)),
        'conviction_nudge': float(sp.get('day_conviction_nudge', 0.12)),
        'lot_max_mult': float(sp.get('day_lot_vs_mandate_max_mult', 2.0)),
        'lot_min_mult': float(sp.get('day_lot_vs_mandate_min_mult', 0.25)),
        'add_min_edge': float(sp.get('day_add_min_edge', 0.08)),
    }


def load_daily(store, *, now=None, equity=None):
    today = _utc_date(now)
    raw = store.get(DAILY_KEY) if store is not None else None
    if isinstance(raw, dict) and raw.get('date') == today:
        state = {
            'date': today,
            'realized_pnl': float(_finite(raw.get('realized_pnl')) or 0.0),
            'opens': int(raw.get('opens') or 0),
            'closes': int(raw.get('closes') or 0),
            'wins': int(raw.get('wins') or 0),
            'losses': int(raw.get('losses') or 0),
            'start_equity': _finite(raw.get('start_equity')),
            'phase': raw.get('phase') or PHASE_HUNT,
            'updated_at': raw.get('updated_at'),
        }
    else:
        state = {
            'date': today,
            'realized_pnl': 0.0,
            'opens': 0,
            'closes': 0,
            'wins': 0,
            'losses': 0,
            'start_equity': _finite(equity),
            'phase': PHASE_HUNT,
            'updated_at': None,
        }
    if state['start_equity'] is None and equity is not None:
        eq = _finite(equity)
        if eq is not None and eq > 0:
            state['start_equity'] = float(eq)
    return state


def save_daily(store, state):
    if store is None or not isinstance(state, dict):
        return
    out = dict(state)
    out['updated_at'] = _now().isoformat(timespec='seconds')
    store.set(DAILY_KEY, out)


def budgets(*, equity, params, daily):
    """£ targets from equity — robot answer to 'how much is enough'."""
    eq = _finite(equity) or _finite((daily or {}).get('start_equity'))
    if eq is None or eq <= 0:
        return {
            'equity': None,
            'profit_target': None,
            'loss_budget': None,
            'be_band': None,
            'risk_per_trade': None,
            'reason': 'no_equity',
        }
    eq = float(eq)
    p = params or {}
    profit = max(0.0, eq * float(p.get('profit_target_frac') or 0.01))
    loss = max(0.0, eq * float(p.get('loss_budget_frac') or 0.02))
    be = max(0.0, eq * float(p.get('be_band_frac') or 0.001))
    rpt = max(0.0, eq * float(p.get('risk_per_trade_frac') or 0.005))
    return {
        'equity': eq,
        'profit_target': profit,
        'loss_budget': loss,
        'be_band': be,
        'risk_per_trade': rpt,
        'reason': 'ok',
    }


def phase_for(*, daily, budgets_map, open_count=0):
    """Which book mode the robot is in — data-driven from £ vs equity fracs."""
    b = budgets_map or {}
    realized = float((daily or {}).get('realized_pnl') or 0.0)
    target = _finite(b.get('profit_target'))
    loss_cap = _finite(b.get('loss_budget'))
    be = _finite(b.get('be_band')) or 0.0
    if loss_cap is not None and realized <= -abs(loss_cap):
        return PHASE_DEFEND
    if target is not None and realized >= abs(target):
        if int(open_count or 0) <= 0:
            return PHASE_DONE
        return PHASE_PROTECT
    if be > 0 and abs(realized) <= be and int((daily or {}).get('closes') or 0) > 0:
        # Already near BE after trades — protect if open, else done.
        if int(open_count or 0) > 0:
            return PHASE_PROTECT
        return PHASE_DONE
    return PHASE_HUNT


def committed_risk(positions, *, stop_ticks_by_deal=None, tick_value=None):
    """Estimate cash at risk still open (best-effort)."""
    if not positions or tick_value is None:
        return 0.0
    tv = _finite(tick_value)
    if tv is None or tv <= 0:
        return 0.0
    total = 0.0
    stops = stop_ticks_by_deal if isinstance(stop_ticks_by_deal, dict) else {}
    for p in positions:
        try:
            size = float(Decimal(str(p.get('size'))))
        except Exception:
            continue
        if size <= 0:
            continue
        deal = str(p.get('dealId') or '')
        st = _finite(stops.get(deal))
        if st is None or st <= 0:
            # Unknown stop → skip committing phantom risk for that row.
            continue
        try:
            total += float(cash_risk(size, st, tv))
        except Exception:
            continue
    return float(total)


def propose_size(*, mandate_size, equity, daily, params, stop_ticks, tick_value,
                 positions=None, stop_ticks_by_deal=None, score_edge=None,
                 market=None, size_grid=None):
    """Adaptive lot from equity risk — always snapped to Capital size grid.

    Never invents fractions (0.1009). Legal sizes are only
    minDealSize + n×minSizeIncrement (≤ maxDealSize).
    """
    seed = _dec(mandate_size)
    if seed is None or seed <= 0:
        return {'size': None, 'reason': 'no_mandate_size', 'allowed': False}
    grid = size_grid if isinstance(size_grid, dict) else dealing_size_grid(
        market, mandate_size=seed)
    if not grid.get('ok'):
        # No Capital grid and no mandate step → refuse (do not invent a lot).
        return {
            'size': None, 'reason': 'no_capital_size_grid', 'allowed': False,
            'size_grid': grid,
        }
    # Snap mandate itself onto the grid — Capital is source of truth.
    seed_on_grid = snap_deal_size(
        seed, min_size=grid['min'], step=grid['step'], max_size=grid.get('max'))
    if seed_on_grid is None:
        return {
            'size': None, 'reason': 'mandate_off_capital_grid', 'allowed': False,
            'size_grid': grid,
        }
    seed = seed_on_grid
    p = params or load_params(None)
    b = budgets(equity=equity, params=p, daily=daily)
    rpt = _finite(b.get('risk_per_trade'))
    if rpt is None or rpt <= 0 or stop_ticks is None or tick_value is None:
        return {
            'size': seed, 'reason': 'mandate_on_capital_grid', 'allowed': True,
            'budgets': b, 'estimated_risk': None, 'scale': 1.0,
            'size_grid': grid,
        }
    committed = committed_risk(
        positions, stop_ticks_by_deal=stop_ticks_by_deal, tick_value=tick_value)
    # Soft robot cap still on Capital grid (never below broker min / above max).
    hi_mult = max(1.0, float(p.get('lot_max_mult') or 2.0))
    minimum = grid['min']
    step = grid['step']
    robot_cap = seed * Decimal(str(hi_mult))
    if grid.get('max') is not None:
        maximum = min(grid['max'], robot_cap)
    else:
        maximum = robot_cap
    maximum = snap_deal_size(
        maximum, min_size=minimum, step=step, max_size=grid.get('max')) or seed
    edge = _finite(score_edge) or 0.0
    risk_cap = float(rpt) * (1.0 + min(0.5, max(0.0, edge)))
    try:
        sized = risk_position_size(
            risk_cap, committed, stop_ticks, tick_value, step, minimum, maximum)
    except (ValueError, ArithmeticError):
        return {
            'size': seed, 'reason': 'risk_size_error_mandate_grid', 'allowed': True,
            'budgets': b, 'estimated_risk': None, 'scale': 1.0,
            'size_grid': grid,
        }
    raw = sized.get('size')
    size = snap_deal_size(
        raw if raw is not None else 0, min_size=minimum, step=step,
        max_size=maximum)
    if size is None or size <= 0:
        room = max(0.0, risk_cap - committed)
        if room <= 0:
            return {
                'size': None, 'reason': 'no_risk_room', 'allowed': False,
                'budgets': b, 'committed_risk': committed, 'scale': 0.0,
                'size_grid': grid,
            }
        size = snap_deal_size(
            minimum, min_size=minimum, step=step, max_size=maximum)
        if size is None:
            return {
                'size': None, 'reason': 'cannot_snap_to_capital_grid',
                'allowed': False, 'size_grid': grid,
            }
    scale = float(size / seed) if seed > 0 else 1.0
    return {
        'size': size,
        'reason': 'equity_risk_on_capital_grid',
        'allowed': True,
        'budgets': b,
        'committed_risk': committed,
        'estimated_risk': float(sized.get('estimated_risk') or 0),
        'scale': scale,
        'raw': float(sized.get('raw') or 0),
        'size_grid': {
            'min': str(grid['min']), 'step': str(grid['step']),
            'max': str(grid['max']) if grid.get('max') is not None else None,
            'source': grid.get('source'),
        },
    }


def allows_new_open(*, store, equity, positions, epic=None, mandate_epic=None,
                    score_edge=None, now=None):
    """May the robot add/open another deal on the mandated instrument?"""
    params = load_params(store)
    daily = load_daily(store, now=now, equity=equity)
    b = budgets(equity=equity, params=params, daily=daily)
    open_n = len(positions or [])
    phase = phase_for(daily=daily, budgets_map=b, open_count=open_n)
    daily['phase'] = phase
    save_daily(store, daily)

    if mandate_epic and epic and str(epic) != str(mandate_epic):
        return {
            'allowed': False, 'reason': 'wrong_epic', 'phase': phase,
            'daily': daily, 'budgets': b, 'open_count': open_n,
        }
    # Foreign epics in the book → refuse adds (mandate stays one market).
    if positions and mandate_epic:
        for p in positions:
            pe = p.get('epic')
            if pe and str(pe) != str(mandate_epic):
                return {
                    'allowed': False, 'reason': 'foreign_epic_open',
                    'phase': phase, 'daily': daily, 'budgets': b,
                    'open_count': open_n,
                }

    max_pos = max(1, int(params.get('max_positions') or 3))
    if open_n >= max_pos:
        return {
            'allowed': False, 'reason': 'max_positions', 'phase': phase,
            'daily': daily, 'budgets': b, 'open_count': open_n,
            'max_positions': max_pos,
        }
    if phase in (PHASE_DEFEND, PHASE_DONE):
        return {
            'allowed': False, 'reason': f'day_phase_{phase.lower()}',
            'phase': phase, 'daily': daily, 'budgets': b, 'open_count': open_n,
        }
    if phase == PHASE_PROTECT:
        return {
            'allowed': False, 'reason': 'day_protect_bank_target',
            'phase': phase, 'daily': daily, 'budgets': b, 'open_count': open_n,
        }
    # Adding onto an existing book needs a clearer edge than a flat open.
    if open_n > 0:
        need = float(params.get('add_min_edge') or 0.08)
        edge = _finite(score_edge)
        if edge is None or edge < need:
            return {
                'allowed': False, 'reason': 'add_needs_stronger_edge',
                'phase': phase, 'daily': daily, 'budgets': b,
                'open_count': open_n, 'need_edge': need, 'score_edge': edge,
            }
    return {
        'allowed': True, 'reason': 'day_book_hunt', 'phase': phase,
        'daily': daily, 'budgets': b, 'open_count': open_n,
        'max_positions': max_pos,
    }


def plan_open(*, store, equity, mandate_size, stop_ticks, tick_value,
              positions=None, stop_ticks_by_deal=None, score_edge=None,
              epic=None, mandate_epic=None, now=None, market=None):
    """Full OPEN plan: allow? + Capital-grid size. Persist last plan for Admin."""
    gate = allows_new_open(
        store=store, equity=equity, positions=positions, epic=epic,
        mandate_epic=mandate_epic, score_edge=score_edge, now=now)
    out = dict(gate)
    if not gate.get('allowed'):
        out['size'] = None
        if store is not None:
            store.set(LAST_PLAN_KEY, out)
        return out
    sized = propose_size(
        mandate_size=mandate_size, equity=equity, daily=gate.get('daily'),
        params=load_params(store), stop_ticks=stop_ticks, tick_value=tick_value,
        positions=positions, stop_ticks_by_deal=stop_ticks_by_deal,
        score_edge=score_edge, market=market)
    out.update(sized)
    if not sized.get('allowed') or sized.get('size') is None:
        out['allowed'] = False
        out['reason'] = sized.get('reason') or 'size_blocked'
    if store is not None:
        store.set(LAST_PLAN_KEY, {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in out.items()
        })
    return out


def soft_nudge_conviction(scores, *, phase, store=None):
    """Tilt away from stupid HOLD when direction is clear and day needs work.

    Never a veto — renormalize so autonomy._safe_scores stays valid.
    """
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if phase not in (PHASE_HUNT, None, ''):
        return dict(scores), {'nudged': False, 'reason': f'phase_{phase}'}
    cfg = load_params(store)
    strength = float(cfg.get('conviction_nudge') or 0.12)
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    buy, sell, hold = out['BUY'], out['SELL'], out['HOLD']
    best = 'BUY' if buy >= sell else 'SELL'
    best_v = out[best]
    other = sell if best == 'BUY' else buy
    # Convincing = clear winner over HOLD and over the opposite side.
    if best_v < 0.34 or (best_v - hold) < 0.06 or (best_v - other) < 0.04:
        return dict(scores), {'nudged': False, 'reason': 'not_convincing'}
    # Day still hunting → shrink HOLD mass toward the clear side.
    delta = strength * min(1.0, (best_v - hold) * 2.0)
    out[best] = best_v + delta
    out['HOLD'] = max(0.0, hold - 0.7 * delta)
    out['BUY' if best == 'SELL' else 'SELL'] = max(0.0, other - 0.3 * delta)
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate'}
    out = {k: v / total for k, v in out.items()}
    return out, {
        'nudged': True, 'target': best, 'delta': delta,
        'reason': 'conviction_anti_hold', 'phase': phase or PHASE_HUNT,
        'renormalized': True, 'sum': sum(out.values()),
    }


def soft_nudge_day_phase(scores, *, phase, store=None):
    """When day target is banked / defending — soft-tilt toward HOLD for new risk."""
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if phase not in (PHASE_PROTECT, PHASE_DEFEND, PHASE_DONE):
        return dict(scores), {'nudged': False, 'reason': 'hunt_no_phase_tilt'}
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    delta = 0.10 if phase == PHASE_PROTECT else 0.16
    out['HOLD'] = out['HOLD'] + delta
    out['BUY'] = max(0.0, out['BUY'] - 0.5 * delta)
    out['SELL'] = max(0.0, out['SELL'] - 0.5 * delta)
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate'}
    out = {k: v / total for k, v in out.items()}
    return out, {
        'nudged': True, 'target': 'HOLD', 'delta': delta,
        'reason': f'day_phase_{phase.lower()}', 'phase': phase,
        'renormalized': True,
    }


def record_open(store, *, now=None, equity=None):
    daily = load_daily(store, now=now, equity=equity)
    daily['opens'] = int(daily.get('opens') or 0) + 1
    save_daily(store, daily)
    return daily


def record_close(store, *, pnl, now=None, equity=None, open_count=0):
    """Bank broker £ into the day book and refresh phase."""
    cash = _finite(pnl)
    if cash is None:
        return load_daily(store, now=now, equity=equity)
    daily = load_daily(store, now=now, equity=equity)
    daily['realized_pnl'] = float(daily.get('realized_pnl') or 0.0) + float(cash)
    daily['closes'] = int(daily.get('closes') or 0) + 1
    if cash > 0:
        daily['wins'] = int(daily.get('wins') or 0) + 1
    elif cash < 0:
        daily['losses'] = int(daily.get('losses') or 0) + 1
    params = load_params(store)
    b = budgets(equity=equity, params=params, daily=daily)
    daily['phase'] = phase_for(
        daily=daily, budgets_map=b, open_count=open_count)
    save_daily(store, daily)
    # Soft evolve risk knobs from this day's £ path (queued via self_params).
    _evolve_knobs_from_close(store, pnl=cash, daily=daily, budgets_map=b)
    return daily


def _evolve_knobs_from_close(store, *, pnl, daily, budgets_map):
    """Tiny equity-frac nudges — robot learns 'enough' without hardcode bans."""
    if store is None:
        return
    from .self_params import load, save, _clip
    p = load(store)
    notes = []
    realized = float((daily or {}).get('realized_pnl') or 0.0)
    target = _finite((budgets_map or {}).get('profit_target'))
    loss_cap = _finite((budgets_map or {}).get('loss_budget'))
    if pnl < 0:
        # Losses → slightly smaller per-trade risk next (still inside rails).
        p['day_risk_per_trade_frac'] = _clip(
            'day_risk_per_trade_frac',
            float(p['day_risk_per_trade_frac']) * 0.98)
        notes.append('day loss → trim risk_per_trade_frac')
    elif pnl > 0 and target is not None and realized >= abs(target):
        # Hit day target → ask a touch more next day (ambition climbs slowly).
        p['day_profit_target_frac'] = _clip(
            'day_profit_target_frac',
            float(p['day_profit_target_frac']) * 1.01)
        notes.append('day target hit → slight raise profit_target_frac')
    if loss_cap is not None and realized <= -abs(loss_cap):
        p['day_loss_budget_frac'] = _clip(
            'day_loss_budget_frac',
            float(p['day_loss_budget_frac']) * 0.97)
        p['day_max_positions'] = _clip(
            'day_max_positions',
            int(p['day_max_positions']) - 1)
        notes.append('day loss budget hit → tighten budget + max positions')
    if notes:
        save(store, p, reason='; '.join(notes))


def open_upl_sum(positions):
    total = 0.0
    any_upl = False
    for p in positions or []:
        u = _finite(p.get('upl'))
        if u is None:
            continue
        total += float(u)
        any_upl = True
    return total if any_upl else None


def snapshot(store, *, equity=None, positions=None):
    params = load_params(store)
    daily = load_daily(store, equity=equity)
    b = budgets(equity=equity, params=params, daily=daily)
    open_n = len(positions or [])
    phase = phase_for(daily=daily, budgets_map=b, open_count=open_n)
    upl = open_upl_sum(positions)
    day_mark = float(daily.get('realized_pnl') or 0.0) + (float(upl) if upl is not None else 0.0)
    return {
        'params': params,
        'daily': dict(daily, phase=phase),
        'budgets': b,
        'phase': phase,
        'open_count': open_n,
        'open_upl': upl,
        'day_mark_pnl': day_mark,
        'last_plan': store.get(LAST_PLAN_KEY) if store is not None else None,
        'goal': 'Finish UTC day at plus or break-even; robot sizes risk from equity.',
    }
