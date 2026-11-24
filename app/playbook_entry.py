"""1m playbook hard ENTRY checklist — structure / zone / session / brackets.

Default ON via ``self_params.playbook_hard_entry``. When hard:
  • Mid-range / failed zone / failed regime → hard OPEN ban (not soft note)
  • Active session window (London→NY overlap) required
  • Flow fight can hard-block
  • Regime SL prefers HL/LH tags, break-candle extreme, sweep wick

Zone candle confirm (pin/engulf/structure) is OFF by default —
``playbook_require_zone_confirm=0``: price touch/cross of the entry zone
is enough for OPEN (no wait for a special confirm candle).
"""
from __future__ import annotations

from datetime import datetime, timezone

# London open ~07 UTC through NY late session ~21 UTC.
SESSION_UTC_START = 7
SESSION_UTC_END = 21


def hard_entry_enabled(store=None) -> bool:
    if store is None:
        return True
    try:
        from .self_params import get as sp_get
        return int(sp_get(store, 'playbook_hard_entry', 1)) == 1
    except Exception:
        return True


def session_filter_enabled(store=None) -> bool:
    if store is None:
        return True
    try:
        from .self_params import get as sp_get
        return int(sp_get(store, 'playbook_session_filter', 1)) == 1
    except Exception:
        return True


def session_allows_open(*, now=None, store=None) -> dict:
    """Quick checklist: trade only during London→NY active hours (UTC)."""
    if not session_filter_enabled(store):
        return {'allowed': True, 'reason': 'session_filter_off', 'hour': None}
    if not hard_entry_enabled(store):
        return {'allowed': True, 'reason': 'playbook_soft', 'hour': None}
    ts = now
    if ts is None:
        ts = datetime.now(timezone.utc)
    elif getattr(ts, 'tzinfo', None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    hour = int(ts.hour)
    ok = SESSION_UTC_START <= hour < SESSION_UTC_END
    return {
        'allowed': bool(ok),
        'reason': 'session_ok' if ok else 'wait_session_london_ny',
        'hour': hour,
        'window': f'{SESSION_UTC_START:02d}-{SESSION_UTC_END:02d}Z',
    }


def zone_confirm_required(store=None) -> bool:
    """When False (default), zone touch/cross opens — no pin/structure wait."""
    try:
        from .self_params import get as sp_get
        return int(sp_get(store, 'playbook_require_zone_confirm', 0)) == 1
    except Exception:
        return False


def zone_confirm_require_both(store=None) -> bool:
    """Optional AND (rejection + structure) — only when confirm is required."""
    if not zone_confirm_required(store):
        return False
    if not hard_entry_enabled(store):
        return False
    try:
        from .self_params import get as sp_get
        return int(sp_get(store, 'playbook_zone_confirm_and', 0)) == 1
    except Exception:
        return False


def break_candle_extreme(action, candles):
    """Breakout SL anchor = last completed break candle high/low."""
    rows = list(candles or [])
    if len(rows) < 1 or action not in ('BUY', 'SELL'):
        return None
    last = rows[-1]
    try:
        if action == 'BUY':
            return float(last.get('low'))
        return float(last.get('high'))
    except (TypeError, ValueError):
        return None


def tagged_hl_lh_anchor(action, candles):
    """Trend SL behind last HL (BUY) or LH (SELL) tag — playbook trend rule."""
    if action not in ('BUY', 'SELL'):
        return None
    try:
        from . import structure_memory
        struct = structure_memory.label_swing_structure(
            candles or [], left=2, right=2) or {}
    except Exception:
        return None
    if action == 'BUY':
        lows = list(struct.get('swing_lows') or [])
        tagged = [s for s in lows if s.get('tag') == 'HL']
        if not tagged:
            tagged = [s for s in lows if s.get('tag') in ('HL', 'SL', 'LL')]
        if not tagged:
            return None
        try:
            return float(tagged[-1]['price'])
        except (TypeError, ValueError, KeyError):
            return None
    highs = list(struct.get('swing_highs') or [])
    tagged = [s for s in highs if s.get('tag') == 'LH']
    if not tagged:
        tagged = [s for s in highs if s.get('tag') in ('LH', 'SH', 'HH')]
    if not tagged:
        return None
    try:
        return float(tagged[-1]['price'])
    except (TypeError, ValueError, KeyError):
        return None


def sweep_wick_extreme(action, candles, *, lookback=6):
    """Fakeout SL behind recent sweep wick (min low / max high)."""
    rows = list(candles or [])[-max(2, int(lookback)):]
    if not rows or action not in ('BUY', 'SELL'):
        return None
    try:
        if action == 'BUY':
            return min(float(b['low']) for b in rows if b.get('low') is not None)
        return max(float(b['high']) for b in rows if b.get('high') is not None)
    except (TypeError, ValueError):
        return None


def impulse_from_level(action, candles, zone, *, tick=0.01) -> dict:
    """Strong leave-zone candle: large body, small opposite wick, closes away."""
    out = {'ok': False, 'reason': 'no_impulse', 'kind': None}
    action = (action or '').upper()
    rows = list(candles or [])
    if action not in ('BUY', 'SELL') or len(rows) < 1 or not isinstance(zone, dict):
        return out
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    last = rows[-1]
    try:
        o = float(last['open']); h = float(last['high'])
        l = float(last['low']); c = float(last['close'])
        lo = float(zone.get('lo')); hi = float(zone.get('hi'))
    except (TypeError, ValueError, KeyError):
        return out
    span = max(h - l, tick_f)
    body = abs(c - o)
    if body < 0.55 * span:
        out['reason'] = 'body_too_small'
        return out
    # Must have interacted with the zone then closed out of it.
    touched = l <= hi + tick_f and h >= lo - tick_f
    if not touched:
        out['reason'] = 'no_zone_touch'
        return out
    if action == 'BUY':
        if c > o and c >= hi - tick_f and (min(o, c) - l) <= 0.35 * span:
            out['ok'] = True
            out['kind'] = 'impulse_bull'
            out['reason'] = 'impulse_bull'
            return out
        out['reason'] = 'no_buy_impulse'
        return out
    if c < o and c <= lo + tick_f and (h - max(o, c)) <= 0.35 * span:
        out['ok'] = True
        out['kind'] = 'impulse_bear'
        out['reason'] = 'impulse_bear'
        return out
    out['reason'] = 'no_sell_impulse'
    return out
