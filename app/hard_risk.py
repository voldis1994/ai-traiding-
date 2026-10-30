"""Optional operator hard-risk bookkeeping — does NOT gate LIVE OPEN.

Audit #21 originally required max risk / daily loss / drawdown. That silently
froze every AI OPEN after #42 while manual SL/TP still worked. Operator asked
to remove the OPEN risk gate entirely — limits may still be stored for Admin
visibility, but entry_allowed / preflight never block a new trade.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

STORE_KEY = 'operator_hard_risk_v1'
DAILY_KEY = 'operator_hard_risk_daily_v1'
PEAK_KEY = 'operator_hard_risk_peak_equity'

# None = unset. Operator must configure; neural must not invent values.
DEFAULTS = {
    'max_risk_per_trade': None,
    'daily_loss_limit': None,
    'equity_drawdown_limit': None,
    'trading_halted': False,
    'halt_reason': None,
}

LIMIT_KEYS = ('max_risk_per_trade', 'daily_loss_limit', 'equity_drawdown_limit')


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _utc_date(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date().isoformat()


def _limit(v):
    n = _finite(v)
    if n is None or n <= 0:
        return None
    return float(n)


def configured(limits: dict) -> bool:
    """True when all three monetary limits are positive operator values."""
    return all(_limit(limits.get(k)) is not None for k in LIMIT_KEYS)


def missing_limits(limits: dict) -> list:
    return [k for k in LIMIT_KEYS if _limit(limits.get(k)) is None]


def load(store) -> dict:
    raw = store.get(STORE_KEY) if store is not None else None
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in LIMIT_KEYS:
            if k in raw:
                out[k] = _limit(raw.get(k))
        out['trading_halted'] = bool(raw.get('trading_halted'))
        reason = raw.get('halt_reason')
        out['halt_reason'] = str(reason) if reason else None
    return out


def save_operator(store, params: dict, *, reason: str = '') -> dict:
    """Persist operator-chosen limits. Missing keys keep previous; None/0 clears.

    Clearing a limit is allowed but OPEN then blocks until reconfigured — never
    silently replaced with invented surrogate numbers.
    """
    if store is None:
        return dict(DEFAULTS)
    prev = load(store)
    clean = {
        'trading_halted': bool(params.get('trading_halted')),
        'halt_reason': (str(params['halt_reason'])
                        if params.get('halt_reason') else None),
    }
    for k in LIMIT_KEYS:
        if k in params:
            clean[k] = _limit(params.get(k))
        else:
            clean[k] = _limit(prev.get(k))
    store.set(STORE_KEY, clean)
    if reason:
        store.set('operator_hard_risk_reason', reason)
    return clean


def neural_may_not_raise(before: dict, after: dict) -> dict:
    out = dict(before)
    for k in LIMIT_KEYS:
        prev = _limit(before.get(k))
        nxt = _limit(after.get(k))
        # Neural cannot invent a limit the operator never set.
        if prev is None:
            out[k] = None
        elif nxt is None:
            out[k] = prev
        else:
            out[k] = float(min(prev, nxt))
    if before.get('trading_halted'):
        out['trading_halted'] = True
        out['halt_reason'] = before.get('halt_reason') or after.get('halt_reason')
    else:
        out['trading_halted'] = bool(after.get('trading_halted'))
        out['halt_reason'] = (str(after['halt_reason'])
                              if after.get('trading_halted') and after.get('halt_reason')
                              else None)
    return out


def apply_neural_update(store, params: dict, *, reason: str = '') -> dict:
    before = load(store)
    safe = neural_may_not_raise(before, params or {})
    store.set(STORE_KEY, safe)
    if reason:
        store.set('operator_hard_risk_reason', 'neural_tightened:' + reason)
    return safe


def halt(store, reason: str) -> dict:
    cur = load(store)
    cur['trading_halted'] = True
    cur['halt_reason'] = str(reason or 'trading halt')
    store.set(STORE_KEY, cur)
    return cur


def clear_halt(store, *, operator: bool = True) -> dict:
    if not operator:
        return load(store)
    cur = load(store)
    cur['trading_halted'] = False
    cur['halt_reason'] = None
    store.set(STORE_KEY, cur)
    return cur


def _daily_state(store, *, now=None) -> dict:
    today = _utc_date(now)
    raw = store.get(DAILY_KEY) if store is not None else None
    if isinstance(raw, dict) and raw.get('date') == today:
        pnl = _finite(raw.get('realized_pnl')) or 0.0
        return {'date': today, 'realized_pnl': float(pnl)}
    state = {'date': today, 'realized_pnl': 0.0}
    if store is not None:
        store.set(DAILY_KEY, state)
    return state


def record_realized_pnl(store, pnl, *, now=None) -> dict:
    n = _finite(pnl)
    if n is None or store is None:
        return _daily_state(store, now=now)
    state = _daily_state(store, now=now)
    state['realized_pnl'] = float(state['realized_pnl']) + float(n)
    store.set(DAILY_KEY, state)
    limits = load(store)
    daily_cap = limits.get('daily_loss_limit')
    if daily_cap is not None and state['realized_pnl'] <= -abs(float(daily_cap)):
        halt(store, f"daily loss limit hit: realized={state['realized_pnl']:.4f} "
                    f"limit=-{float(daily_cap):.4f}")
    return state


def update_peak_equity(store, equity) -> float | None:
    eq = _finite(equity)
    if eq is None or store is None:
        return _finite(store.get(PEAK_KEY)) if store is not None else None
    peak = _finite(store.get(PEAK_KEY))
    if peak is None or eq > peak:
        store.set(PEAK_KEY, float(eq))
        return float(eq)
    return float(peak)


def drawdown_from_peak(store, equity) -> float | None:
    eq = _finite(equity)
    peak = update_peak_equity(store, equity)
    if eq is None or peak is None:
        return None
    return float(max(0.0, peak - eq))


def check_drawdown_halt(store, equity) -> dict:
    limits = load(store)
    dd_lim = limits.get('equity_drawdown_limit')
    dd = drawdown_from_peak(store, equity)
    if dd_lim is not None and dd is not None and dd >= abs(float(dd_lim)):
        halt(store, f"equity drawdown limit hit: dd={dd:.4f} limit={float(dd_lim):.4f}")
    return load(store)


def preflight(store) -> dict:
    """OPEN is never blocked by hard-risk (operator request: remove risk gate)."""
    limits = load(store)
    return {
        'allowed': True,
        'reason': 'hard risk does not gate OPEN',
        'limits': limits,
        'missing_limits': missing_limits(limits),
        'open_gated': False,
    }


def entry_allowed(store, *, proposed_risk=None, equity=None, now=None) -> dict:
    """Always allow LIVE OPEN — hard-risk limits are informational only.

    Daily/drawdown bookkeeping may still update peak equity / halt flags for
    Admin display, but they must not freeze new entries.
    """
    limits = load(store)
    if equity is not None:
        update_peak_equity(store, equity)
        # Record drawdown halt flag for Admin, but do not block OPEN.
        check_drawdown_halt(store, equity)
        limits = load(store)
    daily = _daily_state(store, now=now)
    daily_cap = limits.get('daily_loss_limit')
    if daily_cap is not None and daily['realized_pnl'] <= -abs(float(daily_cap)):
        # Keep halt flag for visibility; OPEN still allowed.
        halt(store, f"daily loss limit hit: realized={daily['realized_pnl']:.4f} "
                    f"limit=-{float(daily_cap):.4f}")
        limits = load(store)
    risk = _finite(proposed_risk)
    return {
        'allowed': True,
        'reason': 'ok — hard risk does not gate OPEN',
        'limits': limits,
        'daily': daily,
        'proposed_risk': float(risk) if risk is not None else None,
        'open_gated': False,
    }


def snapshot(store) -> dict:
    limits = load(store)
    return {
        'limits': limits,
        'configured': configured(limits),
        'missing_limits': missing_limits(limits),
        'daily': _daily_state(store),
        'peak_equity': _finite(store.get(PEAK_KEY)) if store is not None else None,
        'note': ('Hard-risk limits are optional/informational. '
                 'They do not block LIVE OPEN.'),
        'open_gated': False,
        'preflight': preflight(store),
    }
