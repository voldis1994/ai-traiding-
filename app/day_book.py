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
PHASE_DONE = 'DONE'          # flat and day profit target hit — idle until next day


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
        'profit_target_frac': float(sp.get('day_profit_target_frac', 0.0)),
        'loss_budget_frac': float(sp.get('day_loss_budget_frac', 0.10)),
        'risk_per_trade_frac': float(sp.get('day_risk_per_trade_frac', 0.005)),
        'be_band_frac': float(sp.get('day_be_band_frac', 0.001)),
        # Floor concurrent deals / flat pack slices — evolve must not collapse
        # LIVE back to a single ticket (operator: always multi-lot when flat).
        'max_positions': max(5, int(sp.get('day_max_positions', 5))),
        'conviction_nudge': float(sp.get('day_conviction_nudge', 0.12)),
        'lot_max_mult': float(sp.get('day_lot_vs_mandate_max_mult', 2.0)),
        'lot_min_mult': float(sp.get('day_lot_vs_mandate_min_mult', 0.25)),
        'add_min_edge': float(sp.get('day_add_min_edge', 0.08)),
        'open_slices': max(3, int(sp.get('day_open_slices', 5))),
        'slice_min_edge': float(sp.get('day_slice_min_edge', 0.04)),
        'size_full_balance': int(sp.get('day_size_full_balance', 1)),
    }


def _trade_closed_on_utc_date(closed_raw, today):
    """True when a trades.closed timestamp falls on the given UTC date string."""
    if closed_raw in (None, '') or not today:
        return False
    try:
        text = str(closed_raw).replace('Z', '+00:00')
        # SQLite / journal sometimes store 'YYYY-MM-DD HH:MM:SS' without T.
        if 'T' not in text and ' ' in text[:19]:
            text = text[:10] + 'T' + text[11:]
        dt = datetime.fromisoformat(text[:32])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat() == today
    except Exception:
        # Fallback: prefix match on ISO date.
        return str(closed_raw)[:10] == today


def load_daily(store, *, now=None, equity=None):
    today = _utc_date(now)
    raw = store.get(DAILY_KEY) if store is not None else None
    if isinstance(raw, dict) and raw.get('date') == today:
        deal_ids = raw.get('deal_ids') if isinstance(raw.get('deal_ids'), list) else []
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
            'deal_ids': [str(x) for x in deal_ids],
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
            'deal_ids': [],
        }
    if state['start_equity'] is None and equity is not None:
        eq = _finite(equity)
        if eq is not None and eq > 0:
            state['start_equity'] = float(eq)
    return state


def reconcile_daily_from_trades(store, *, now=None, equity=None, open_count=0):
    """Rebuild UTC-day closes / realized P&L from the trades table.

    Fixes Admin desync when ``day_book_daily_v1`` stayed at 0 closes while
    ``trades`` already had several closed rows (missed ``record_close``,
    restart before bank, or late broker cash).
    """
    daily = load_daily(store, now=now, equity=equity)
    if store is None:
        return daily
    today = daily.get('date') or _utc_date(now)
    try:
        rows = store.db.execute(
            'SELECT deal_id,pnl,pnl_source,closed FROM trades '
            'WHERE closed IS NOT NULL AND pnl IS NOT NULL'
        ).fetchall()
    except Exception:
        rows = []
    # Also count UTC-day opens from trades.opened (capacity / Admin opens).
    try:
        open_rows = store.db.execute(
            'SELECT deal_id,opened FROM trades WHERE opened IS NOT NULL'
        ).fetchall()
    except Exception:
        open_rows = []
    # Prefer broker cash when a deal appears twice; otherwise keep any settled pnl.
    by_deal = {}
    for deal_id, pnl, src, closed in rows:
        if not _trade_closed_on_utc_date(closed, today):
            continue
        cash = _finite(pnl)
        if cash is None:
            continue
        deal = str(deal_id)
        src_s = str(src or '').strip().lower()
        prev = by_deal.get(deal)
        if prev is None:
            by_deal[deal] = (float(cash), src_s)
            continue
        # Upgrade estimate → broker when cash finally settles.
        if prev[1] != 'broker' and src_s == 'broker':
            by_deal[deal] = (float(cash), src_s)
    opens_today = 0
    for deal_id, opened in open_rows or ():
        if _trade_closed_on_utc_date(opened, today):
            opens_today += 1

    if not by_deal:
        # Still heal opens count when closes are empty/mismatched.
        if opens_today and int(daily.get('opens') or 0) != opens_today:
            daily['opens'] = int(opens_today)
            save_daily(store, daily)
        return daily
    realized = 0.0
    wins = losses = 0
    deal_ids = sorted(by_deal.keys())
    for deal in deal_ids:
        cash, _src = by_deal[deal]
        realized += float(cash)
        if cash > 0:
            wins += 1
        elif cash < 0:
            losses += 1
    closes = len(deal_ids)
    daily_closes = int(daily.get('closes') or 0)
    daily_pnl = float(daily.get('realized_pnl') or 0.0)
    daily_ids = set(str(x) for x in (daily.get('deal_ids') or []))
    # Already aligned — leave phase/opens alone.
    if (daily_closes == closes and abs(daily_pnl - realized) < 1e-9
            and daily_ids == set(deal_ids)):
        return daily
    daily['realized_pnl'] = float(realized)
    daily['closes'] = int(closes)
    daily['wins'] = int(wins)
    daily['losses'] = int(losses)
    daily['deal_ids'] = list(deal_ids)
    if opens_today:
        daily['opens'] = max(int(daily.get('opens') or 0), int(opens_today))
    params = load_params(store)
    b = budgets(equity=equity, params=params, daily=daily)
    daily['phase'] = phase_for(
        daily=daily, budgets_map=b, open_count=int(open_count or 0))
    save_daily(store, daily)
    return daily


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
    # 0 / missing profit_target_frac → no day profit cap.
    try:
        ptf = float(p.get('profit_target_frac') if p.get('profit_target_frac') is not None else 0.0)
    except (TypeError, ValueError):
        ptf = 0.0
    profit = (max(0.0, eq * ptf) if ptf > 0 else None)
    loss = max(0.0, eq * float(p.get('loss_budget_frac') or 0.10))
    be = max(0.0, eq * float(p.get('be_band_frac') or 0.001))
    # Full-balance mode: OPEN pack cash-risk at SL ≈ whole account (then
    # multi-lot splits that pack). Fractional mode keeps risk_per_trade_frac.
    if int(p.get('size_full_balance') or 0) == 1:
        rpt = eq
    else:
        rpt = max(0.0, eq * float(p.get('risk_per_trade_frac') or 0.005))
    return {
        'equity': eq,
        'profit_target': profit,
        'loss_budget': loss,
        'be_band': be,
        'risk_per_trade': rpt,
        'size_full_balance': int(p.get('size_full_balance') or 0) == 1,
        'reason': 'ok',
    }


def phase_for(*, daily, budgets_map, open_count=0):
    """Which book mode the robot is in — data-driven from £ vs equity fracs.

    DEFEND on loss budget (default 10% equity). Profit target is optional —
    when unset/0 there is no PROTECT/DONE from day £ wins.
    Mere BE-band after a scratch/small close must NOT flip DONE/PROTECT
    (that forced HOLD after pennies).
    """
    b = budgets_map or {}
    realized = float((daily or {}).get('realized_pnl') or 0.0)
    target = _finite(b.get('profit_target'))
    loss_cap = _finite(b.get('loss_budget'))
    if loss_cap is not None and realized <= -abs(loss_cap):
        return PHASE_DEFEND
    if target is not None and target > 0 and realized >= abs(target):
        if int(open_count or 0) <= 0:
            return PHASE_DONE
        return PHASE_PROTECT
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
    full_bal = int(p.get('size_full_balance') or 0) == 1
    hi_mult = max(1.0, float(p.get('lot_max_mult') or 2.0))
    lo_mult = max(0.0, float(p.get('lot_min_mult') or 0.25))
    minimum = grid['min']
    step = grid['step']
    if full_bal:
        # Use whole account cash-risk: Capital maxDealSize is the only ceiling
        # (mandate 2× cap would leave most of a £20 book unused).
        if grid.get('max') is not None:
            maximum = grid['max']
        else:
            maximum = snap_deal_size(
                seed * Decimal('1000'), min_size=minimum, step=step,
                max_size=None) or seed
        maximum = snap_deal_size(
            maximum, min_size=minimum, step=step, max_size=grid.get('max')) or seed
        robot_floor = minimum
        risk_cap = float(rpt)  # already = equity; do not inflate past balance
    else:
        robot_cap = seed * Decimal(str(hi_mult))
        if grid.get('max') is not None:
            maximum = min(grid['max'], robot_cap)
        else:
            maximum = robot_cap
        maximum = snap_deal_size(
            maximum, min_size=minimum, step=step, max_size=grid.get('max')) or seed
        robot_floor = snap_deal_size(
            seed * Decimal(str(lo_mult)), min_size=minimum, step=step,
            max_size=maximum)
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
    if (not full_bal and robot_floor is not None and size is not None
            and size < robot_floor):
        # Risk room may still forbid the floor — then refuse rather than undersize.
        floor_risk = None
        try:
            floor_risk = float(cash_risk(robot_floor, stop_ticks, tick_value))
        except Exception:
            floor_risk = None
        if floor_risk is not None and floor_risk > risk_cap - committed + 1e-12:
            return {
                'size': None, 'reason': 'mandate_floor_exceeds_risk_room',
                'allowed': False, 'budgets': b, 'committed_risk': committed,
                'size_grid': grid, 'lot_min_mult': lo_mult,
            }
        size = robot_floor
    scale = float(size / seed) if seed > 0 else 1.0
    return {
        'size': size,
        'reason': ('full_balance_on_capital_grid' if full_bal
                   else 'equity_risk_on_capital_grid'),
        'allowed': True,
        'budgets': b,
        'committed_risk': committed,
        'estimated_risk': float(sized.get('estimated_risk') or 0),
        'scale': scale,
        'raw': float(sized.get('raw') or 0),
        'lot_min_mult': lo_mult,
        'size_full_balance': full_bal,
        'size_grid': {
            'min': str(grid['min']), 'step': str(grid['step']),
            'max': str(grid['max']) if grid.get('max') is not None else None,
            'source': grid.get('source'),
        },
    }


def allows_new_open(*, store, equity, positions, epic=None, mandate_epic=None,
                    score_edge=None, now=None, trend_aligned=False,
                    skip_add_edge=False):
    """May the robot add/open another deal on the mandated instrument?

    ``trend_aligned``: live tape matches the open book side (e.g. TREND_UP with
    BUY open) — then do not demand a huge softmax edge to ADD on the rebound.
    ``skip_add_edge``: mid multi-lot burst — capacity/phase only (edge already
    cleared for the planned N slices).
    """
    params = load_params(store)
    open_n = len(positions or [])
    daily = reconcile_daily_from_trades(
        store, now=now, equity=equity, open_count=open_n)
    b = budgets(equity=equity, params=params, daily=daily)
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
    # Adding onto an existing book needs a clearer edge than a flat open —
    # unless the live tape already agrees with the book (clear rebound ADD).
    # Planned multi-lot burst skips this — edge was checked once for the pack.
    if open_n > 0 and not skip_add_edge:
        need = float(params.get('add_min_edge') or 0.08)
        if trend_aligned:
            try:
                from .self_params import get as sp_get
                floor = float(sp_get(store, 'day_add_trend_floor', 0.02))
            except Exception:
                floor = 0.02
            need = min(need, floor)
        edge = _finite(score_edge)
        if edge is None or edge < need:
            return {
                'allowed': False, 'reason': 'add_needs_stronger_edge',
                'phase': phase, 'daily': daily, 'budgets': b,
                'open_count': open_n, 'need_edge': need, 'score_edge': edge,
                'trend_aligned': bool(trend_aligned),
            }
    return {
        'allowed': True, 'reason': 'day_book_hunt', 'phase': phase,
        'daily': daily, 'budgets': b, 'open_count': open_n,
        'max_positions': max_pos, 'trend_aligned': bool(trend_aligned),
    }


def plan_open(*, store, equity, mandate_size, stop_ticks, tick_value,
              positions=None, stop_ticks_by_deal=None, score_edge=None,
              epic=None, mandate_epic=None, now=None, market=None,
              trend_aligned=False):
    """Full OPEN plan: allow? + Capital-grid size. Persist last plan for Admin."""
    gate = allows_new_open(
        store=store, equity=equity, positions=positions, epic=epic,
        mandate_epic=mandate_epic, score_edge=score_edge, now=now,
        trend_aligned=trend_aligned)
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


def _slice_advantageous(*, score_edge=None, action=None, live_trend=None,
                        structure=None, returns_bps=None, store=None):
    """True when flat OPEN should split into several simultaneous slices.

    Signal-first: a LIVE BUY/SELL that already cleared HOLD-necessary is enough
    to multi-lot when flat. Do not re-freeze behind a 0.06 score-edge hardcode.
    ``score_edge`` / tape args are kept for callers and future evolve hints.
    """
    _ = (score_edge, live_trend, structure, returns_bps, store)
    return action in ('BUY', 'SELL')


def split_planned_sizes(total_size, *, store=None, positions=None, size_grid=None,
                        score_edge=None, action=None, live_trend=None,
                        structure=None, returns_bps=None):
    """Split one planned lot into N Capital-grid slices for simultaneous OPEN.

    Flat + BUY/SELL → up to ``day_open_slices`` (default 5) pieces whose
    sizes sum ≈ total (when the lot covers ≥2 broker mins). Already-open
    books ADD one slice at a time on later bars.
    """
    grid = size_grid if isinstance(size_grid, dict) else {}
    mn = grid.get('min')
    step = grid.get('step')
    mx = grid.get('max')
    total = _dec(total_size)
    if total is None or total <= 0 or mn is None or step is None:
        one = snap_deal_size(total_size, min_size=mn, step=step, max_size=mx) if mn is not None and step is not None else None
        return [one] if one is not None else []
    params = load_params(store)
    max_pos = max(5, int(params.get('max_positions') or 5))
    want = max(3, int(params.get('open_slices') or 5))
    open_n = len(positions or [])
    room = max(0, max_pos - open_n)
    advantageous = _slice_advantageous(
        score_edge=score_edge, action=action, live_trend=live_trend,
        structure=structure, returns_bps=returns_bps, store=store)
    # How many min-lots fit in the planned total?
    try:
        max_by_min = int((total / Decimal(str(mn))).to_integral_value(
            rounding=ROUND_FLOOR))
    except Exception:
        max_by_min = 1
    if open_n > 0 or not advantageous:
        # Already in a deal → ADD one slice; HOLD never multi-slices.
        n = 1
    else:
        # Flat BUY/SELL: always split into as many tickets as the lot covers
        # (up to open_slices). Do not collapse to one when room was evolved to 1.
        n = min(want, max_by_min if max_by_min > 0 else 1, max(room, want))
    n = max(1, min(n, max_by_min if max_by_min > 0 else 1))
    if n <= 1:
        one = snap_deal_size(total, min_size=mn, step=step, max_size=mx)
        return [one] if one is not None else []
    raw = total / Decimal(n)
    base = snap_deal_size(raw, min_size=mn, step=step, max_size=mx)
    if base is None or base <= 0:
        one = snap_deal_size(total, min_size=mn, step=step, max_size=mx)
        return [one] if one is not None else []
    sizes = [base] * n
    used = base * n
    rem = total - used
    st = Decimal(str(step))
    i = 0
    while rem >= st - Decimal('1e-12') and i < n:
        bumped = snap_deal_size(
            sizes[i] + st, min_size=mn, step=step, max_size=mx)
        if bumped is None or bumped <= sizes[i]:
            break
        rem -= (bumped - sizes[i])
        sizes[i] = bumped
        i += 1
    out = []
    for s in sizes:
        snapped = snap_deal_size(s, min_size=mn, step=step, max_size=mx)
        if snapped is not None and snapped > 0:
            out.append(snapped)
    return out or ([snap_deal_size(total, min_size=mn, step=step, max_size=mx)]
                   if snap_deal_size(total, min_size=mn, step=step, max_size=mx)
                   else [])


def _clear_trend_side(*, live_trend=None, structure=None, returns_bps=None):
    """Map visible market path → BUY/SELL. None when the tape is mixed/chop."""
    st = str(structure or '').upper()
    tr = str(live_trend or '').upper() if live_trend is not None else ''
    if st in ('TREND_UP', 'BREAKOUT_UP') or tr == 'UP':
        return 'BUY'
    if st in ('TREND_DOWN', 'BREAKOUT_DOWN') or tr == 'DOWN':
        return 'SELL'
    # Sharp short-horizon continuation (V-rebound) while 15/60 may still be MIXED.
    if isinstance(returns_bps, dict):
        def _bp(key):
            v = returns_bps.get(key)
            if v is None:
                v = returns_bps.get(str(key))
            try:
                n = float(v)
                return n if math.isfinite(n) else None
            except (TypeError, ValueError):
                return None
        r1, r5 = _bp(1), _bp(5)
        if r1 is not None and r5 is not None:
            if r1 > 0 and r5 > 0:
                return 'BUY'
            if r1 < 0 and r5 < 0:
                return 'SELL'
    return None


def soft_nudge_conviction(scores, *, phase, store=None, live_trend=None,
                          structure=None, returns_bps=None):
    """Tilt away from stupid HOLD when direction is clear and day needs work.

    Never a veto — renormalize so autonomy._safe_scores stays valid.
    When live structure/trend is clear but HOLD still dominates the softmax
    (classic missed TREND_UP), transfer HOLD mass to the tape side so argmax
    can OPEN instead of forever-abstaining.
    """
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if phase not in (PHASE_HUNT, None, ''):
        return dict(scores), {'nudged': False, 'reason': f'phase_{phase}'}
    cfg = load_params(store)
    strength = float(cfg.get('conviction_nudge') or 0.12)
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    buy, sell, hold = out['BUY'], out['SELL'], out['HOLD']
    clear = _clear_trend_side(
        live_trend=live_trend, structure=structure, returns_bps=returns_bps)
    best = 'BUY' if buy >= sell else 'SELL'
    best_v = out[best]
    other = sell if best == 'BUY' else buy

    # --- Clear tape path: break HOLD dominance (the missed GOLD rebound case) ---
    if clear is not None:
        side_v = out[clear]
        opp = 'SELL' if clear == 'BUY' else 'BUY'
        opp_v = out[opp]
        # Do not fight a strongly opposite neural vote.
        if opp_v >= 0.45 and opp_v > side_v + 0.10:
            pass  # fall through to ordinary conviction path
        elif hold >= side_v or (side_v - hold) < 0.05:
            # Need side to lead HOLD by a small margin after transfer.
            lead = 0.06
            need = max(0.0, (hold - side_v) + lead)
            transfer = min(hold * 0.70, max(need, strength * 1.5))
            if transfer > 1e-6:
                out[clear] = side_v + transfer
                out['HOLD'] = max(0.0, hold - transfer)
                # Nibble the opposite side only if it is not already tiny.
                if opp_v > 0.08:
                    trim = min(opp_v * 0.35, 0.25 * transfer)
                    out[opp] = max(0.0, opp_v - trim)
                    out[clear] = out[clear] + trim
                total = sum(out.values())
                if total <= 1e-12:
                    return dict(scores), {'nudged': False, 'reason': 'degenerate'}
                out = {k: v / total for k, v in out.items()}
                return out, {
                    'nudged': True, 'target': clear, 'delta': transfer,
                    'reason': 'clear_trend_anti_hold',
                    'phase': phase or PHASE_HUNT,
                    'structure': structure, 'live_trend': live_trend,
                    'renormalized': True, 'sum': sum(out.values()),
                }

    # Even a modest lean vs the opposite side → drain loud HOLD (HUNT only).
    # Stupid HOLD trap: HOLD 50%+ while BUY already beats SELL (or reverse).
    if (best_v - other) >= 0.03 and hold + 1e-12 >= best_v:
        lead = 0.05
        need = max(0.0, (hold - best_v) + lead)
        transfer = min(hold * 0.80, max(need, strength * 1.25))
        if transfer > 1e-6:
            out[best] = best_v + transfer
            out['HOLD'] = max(0.0, hold - transfer)
            if other > 0.08:
                trim = min(other * 0.25, 0.20 * transfer)
                out['BUY' if best == 'SELL' else 'SELL'] = max(0.0, other - trim)
                out[best] = out[best] + trim
            total = sum(out.values())
            if total <= 1e-12:
                return dict(scores), {'nudged': False, 'reason': 'degenerate'}
            out = {k: v / total for k, v in out.items()}
            return out, {
                'nudged': True, 'target': best, 'delta': transfer,
                'reason': 'lean_anti_hold', 'phase': phase or PHASE_HUNT,
                'clear_side': clear, 'renormalized': True, 'sum': sum(out.values()),
            }

    # Convincing = clear winner over HOLD and over the opposite side.
    # With a matching clear tape, accept a slightly weaker neural lead.
    try:
        from .self_params import get as sp_get
        min_best = float(sp_get(store, 'conviction_min_best_clear' if clear == best
                                else 'conviction_min_best',
                                0.24 if clear == best else 0.28))
        min_vs_hold = float(sp_get(store, 'conviction_min_vs_hold_clear' if clear == best
                                   else 'conviction_min_vs_hold',
                                   0.02 if clear == best else 0.04))
    except Exception:
        min_best = 0.24 if clear == best else 0.28
        min_vs_hold = 0.02 if clear == best else 0.04
    min_vs_other = min_vs_hold
    if best_v < min_best or (best_v - hold) < min_vs_hold or (best_v - other) < min_vs_other:
        return dict(scores), {'nudged': False, 'reason': 'not_convincing',
                              'clear_side': clear}
    # Day still hunting → shrink HOLD mass toward the clear side.
    delta = strength * min(1.0, max(0.25, (best_v - hold) * 2.0))
    if clear == best:
        delta = max(delta, strength * 1.25)
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
        'clear_side': clear, 'renormalized': True, 'sum': sum(out.values()),
    }


def soft_nudge_day_phase(scores, *, phase, store=None):
    """When day target is banked / defending — soft-tilt toward HOLD for new risk."""
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if phase not in (PHASE_PROTECT, PHASE_DEFEND, PHASE_DONE):
        return dict(scores), {'nudged': False, 'reason': 'hunt_no_phase_tilt'}
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    try:
        from .self_params import get as sp_get
        delta = float(sp_get(store, 'day_phase_protect_tilt', 0.10)
                      if phase == PHASE_PROTECT
                      else sp_get(store, 'day_phase_defend_tilt', 0.16))
    except Exception:
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


def record_close(store, *, pnl, now=None, equity=None, open_count=0, deal_id=None):
    """Bank broker £ into the day book and refresh phase.

    ``deal_id`` makes banking idempotent so reconcile + late cash cannot
    double-count the same close.
    """
    cash = _finite(pnl)
    if cash is None:
        return load_daily(store, now=now, equity=equity)
    daily = load_daily(store, now=now, equity=equity)
    if deal_id is not None:
        ids = [str(x) for x in (daily.get('deal_ids') or [])]
        deal = str(deal_id)
        if deal in ids:
            return daily
        ids.append(deal)
        daily['deal_ids'] = ids
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
    """Tiny day knobs via self_params trial protocol — never bypass freeze."""
    if store is None:
        return
    from .self_params import evolve_from_feedback
    realized = float((daily or {}).get('realized_pnl') or 0.0)
    target = _finite((budgets_map or {}).get('profit_target'))
    loss_cap = _finite((budgets_map or {}).get('loss_budget'))
    event = None
    if loss_cap is not None and realized <= -abs(loss_cap):
        event = 'day_budget_hit'
    elif pnl < 0:
        event = 'day_loss'
    elif pnl > 0 and target is not None and realized >= abs(target):
        event = 'day_target_hit'
    if event:
        evolve_from_feedback(store, day_book_event=event, commit=False,
                             reason=f'day_book {event}')


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
    open_n = len(positions or [])
    daily = reconcile_daily_from_trades(
        store, equity=equity, open_count=open_n)
    b = budgets(equity=equity, params=params, daily=daily)
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
        'goal': 'No day profit cap; DEFEND at 10% equity loss; OPEN pack uses full account £.',
    }
