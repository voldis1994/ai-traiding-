"""Operator-owned hard risk limits — independent of neural / self_params.

Audit #21: max risk per trade, daily loss limit, equity drawdown limit, and
trading halt. The model must NOT raise these itself. Only an explicit operator
save may set or loosen them; neural code may only tighten or leave unchanged.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

STORE_KEY = 'operator_hard_risk_v1'
DAILY_KEY = 'operator_hard_risk_daily_v1'
PEAK_KEY = 'operator_hard_risk_peak_equity'

# Mandatory positive floors — None is never a valid monetary limit (critical #2).
# Operator may tighten; neural may only tighten further. Clearing is refused.
DEFAULTS = {
    'max_risk_per_trade': 5.0,
    'daily_loss_limit': 25.0,
    'equity_drawdown_limit': 100.0,
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


def _required_limit(v, key):
    """Positive float only — fall back to mandatory DEFAULTS[key]."""
    n = _limit(v)
    if n is not None:
        return float(n)
    return float(DEFAULTS[key])


def load(store) -> dict:
    raw = store.get(STORE_KEY) if store is not None else None
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in LIMIT_KEYS:
            if k in raw:
                # Never restore None/0 — mandatory numeric floors always apply.
                out[k] = _required_limit(raw.get(k), k)
        out['trading_halted'] = bool(raw.get('trading_halted'))
        reason = raw.get('halt_reason')
        out['halt_reason'] = str(reason) if reason else None
    return out


def save_operator(store, params: dict, *, reason: str = '') -> dict:
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
            parsed = _limit(params.get(k))
            # Refuse to clear a mandatory limit; keep previous / default floor.
            clean[k] = float(parsed) if parsed is not None else float(prev[k])
        else:
            clean[k] = float(prev[k])
    store.set(STORE_KEY, clean)
    if reason:
        store.set('operator_hard_risk_reason', reason)
    return clean


def neural_may_not_raise(before: dict, after: dict) -> dict:
    out = dict(before)
    for k in LIMIT_KEYS:
        prev = _limit(before.get(k))
        nxt = _limit(after.get(k))
        # Mandatory floors: missing before → DEFAULTS, never invent looser via neural.
        if prev is None:
            out[k] = float(DEFAULTS[k])
        elif nxt is None:
            out[k] = float(prev)
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


def entry_allowed(store, *, proposed_risk=None, equity=None, now=None) -> dict:
    limits = load(store)
    if limits.get('trading_halted'):
        return {
            'allowed': False,
            'reason': 'trading halted: ' + (limits.get('halt_reason') or 'operator halt'),
            'limits': limits,
        }
    daily = _daily_state(store, now=now)
    daily_cap = limits.get('daily_loss_limit')
    if daily_cap is not None and daily['realized_pnl'] <= -abs(float(daily_cap)):
        halt(store, f"daily loss limit hit: realized={daily['realized_pnl']:.4f}")
        return {
            'allowed': False,
            'reason': (f"daily loss limit reached "
                       f"({daily['realized_pnl']:.4f} \u2264 -{float(daily_cap):.4f})"),
            'limits': load(store),
            'daily': daily,
        }
    if equity is not None:
        check_drawdown_halt(store, equity)
        limits = load(store)
        if limits.get('trading_halted'):
            return {
                'allowed': False,
                'reason': 'trading halted: ' + (limits.get('halt_reason') or 'drawdown'),
                'limits': limits,
            }
    # Critical #2: max risk per trade is always mandatory (never skip when unset).
    max_risk = _required_limit(limits.get('max_risk_per_trade'), 'max_risk_per_trade')
    risk = _finite(proposed_risk)
    if risk is None:
        return {
            'allowed': False,
            'reason': ('OPEN blocked: max risk per trade is mandatory but cash risk '
                       'could not be computed (missing tick value)'),
            'limits': limits,
        }
    if risk > float(max_risk) + 1e-12:
        return {
            'allowed': False,
            'reason': (f"OPEN blocked: fixed size risk {risk:.4f} exceeds "
                       f"max risk per trade {float(max_risk):.4f}"),
            'limits': limits,
            'proposed_risk': float(risk),
        }
    return {'allowed': True, 'reason': 'ok', 'limits': limits, 'daily': daily,
            'proposed_risk': float(risk) if risk is not None else None}


def snapshot(store) -> dict:
    return {
        'limits': load(store),
        'daily': _daily_state(store),
        'peak_equity': _finite(store.get(PEAK_KEY)) if store is not None else None,
        'note': 'Hard limits are operator/config-owned; neural cannot raise them.',
    }
