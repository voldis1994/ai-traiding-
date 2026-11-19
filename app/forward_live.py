"""Forward-live diagnostics — reveal live problems backtests hide.

Not a profit printer. Each closed deal records:
  1. entry/exit precision (intended mid vs fill, hold bars)
  2. slippage / spread cost vs peļņa
  3. risk geometry (SL/TP vs ATR, size)
  4. false-signal flags (scratch, quick reverse, MTF conflict)
  5. rolling live vs recent expectancy (overfit smell)

Admin reads ``snapshot(store)`` — machine also uses flags for soft nudges.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'forward_live_v1'
MAX_ROWS = 120


def _finite(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_close(store, *, deal_id, epic=None, direction=None, pnl=None,
                 pnl_source=None, entry=None, exit_level=None, spread=None,
                 stop_distance=None, profit_distance=None, atr=None, size=None,
                 hold_bars=None, mtf_mode=None, mtf_preferred=None,
                 shared_side=None, intent=None, scratch_band=0.05) -> dict:
    """Append one forward-live row after a closed trade."""
    if store is None:
        return {}
    pnl_v = _finite(pnl)
    entry_v = _finite(entry)
    exit_v = _finite(exit_level)
    spread_v = _finite(spread) or 0.0
    atr_v = _finite(atr)
    stop_d = _finite(stop_distance)
    profit_d = _finite(profit_distance)
    hold = None
    try:
        hold = int(hold_bars) if hold_bars is not None else None
    except (TypeError, ValueError):
        hold = None

    # 1) Entry/exit precision — adverse mid→exit vs side.
    move = None
    if entry_v is not None and exit_v is not None and direction in ('BUY', 'SELL'):
        move = (exit_v - entry_v) if direction == 'BUY' else (entry_v - exit_v)

    # 2) Slippage proxy: spread as fraction of |pnl| when cash known.
    slip_ratio = None
    if pnl_v is not None and abs(pnl_v) > 1e-9 and spread_v > 0:
        slip_ratio = round(spread_v / max(abs(pnl_v), 1e-6), 4)

    # 3) Risk geometry vs ATR.
    sl_atr = (stop_d / atr_v) if (stop_d is not None and atr_v and atr_v > 0) else None
    tp_atr = (profit_d / atr_v) if (profit_d is not None and atr_v and atr_v > 0) else None
    hairline_sl = bool(sl_atr is not None and sl_atr < 0.45)

    # 4) False-signal flags.
    scratch = bool(pnl_v is not None and abs(pnl_v) <= float(scratch_band or 0.05))
    quick = bool(hold is not None and hold <= 2 and pnl_v is not None and pnl_v < 0)
    side_conflict = False
    pref = str(mtf_preferred or shared_side or '').upper()
    act = str(direction or intent or '').upper()
    if pref in ('BUY', 'SELL') and act in ('BUY', 'SELL') and pref != act:
        side_conflict = True
    false_signal = bool(scratch or quick or side_conflict or hairline_sl)

    row = {
        'at': _now(),
        'deal_id': str(deal_id) if deal_id is not None else None,
        'epic': epic,
        'direction': direction,
        'pnl': round(pnl_v, 4) if pnl_v is not None else None,
        'pnl_source': pnl_source,
        'entry': entry_v,
        'exit': exit_v,
        'move': round(move, 5) if move is not None else None,
        'spread': spread_v,
        'slip_ratio': slip_ratio,
        'stop_distance': stop_d,
        'profit_distance': profit_d,
        'atr': atr_v,
        'sl_atr': round(sl_atr, 3) if sl_atr is not None else None,
        'tp_atr': round(tp_atr, 3) if tp_atr is not None else None,
        'size': _finite(size),
        'hold_bars': hold,
        'mtf_mode': mtf_mode,
        'mtf_preferred': mtf_preferred,
        'shared_side': shared_side,
        'hairline_sl': hairline_sl,
        'scratch': scratch,
        'quick_loss': quick,
        'side_conflict': side_conflict,
        'false_signal': false_signal,
    }
    bag = store.get(STORE_KEY) if hasattr(store, 'get') else None
    rows = list((bag or {}).get('rows') or []) if isinstance(bag, dict) else []
    rows.append(row)
    if len(rows) > MAX_ROWS:
        rows = rows[-MAX_ROWS:]
    store.set(STORE_KEY, {'rows': rows, 'updated': _now()})
    return row


def snapshot(store, *, n=40) -> dict:
    """Admin/forward-test card: rolling live diagnostics."""
    empty = {
        'ok': False, 'n': 0, 'false_signal_rate': None, 'scratch_rate': None,
        'hairline_sl_rate': None, 'avg_sl_atr': None, 'avg_slip_ratio': None,
        'expectancy': None, 'recent_pnl': None, 'side_conflicts': 0,
        'tips': [], 'recent': [],
    }
    if store is None:
        return empty
    bag = store.get(STORE_KEY) if hasattr(store, 'get') else None
    rows = list((bag or {}).get('rows') or []) if isinstance(bag, dict) else []
    if not rows:
        return empty
    recent = rows[-max(1, int(n)):]
    pnls = [r['pnl'] for r in recent if r.get('pnl') is not None]
    false_n = sum(1 for r in recent if r.get('false_signal'))
    scratch_n = sum(1 for r in recent if r.get('scratch'))
    hair_n = sum(1 for r in recent if r.get('hairline_sl'))
    conflict_n = sum(1 for r in recent if r.get('side_conflict'))
    sl_atrs = [r['sl_atr'] for r in recent if r.get('sl_atr') is not None]
    slips = [r['slip_ratio'] for r in recent if r.get('slip_ratio') is not None]
    tips = []
    rate = false_n / len(recent) if recent else 0
    if rate >= 0.4:
        tips.append('Daudz false-signal (scratch / quick loss / side conflict) — pārbaudi zonu + shared side.')
    if hair_n and hair_n / len(recent) >= 0.2:
        tips.append('Hairline SL pret ATR — SL jābūt ≥0.5×ATR.')
    if conflict_n:
        tips.append(f'{conflict_n}× side conflict uz to pašu tirgu — flotē jābūt vienai pusei.')
    if slips and (sum(slips) / len(slips)) >= 1.0:
        tips.append('Spread/slip bieži apēd peļņu — samazini scratch ieejas.')
    if len(pnls) >= 8:
        half = len(pnls) // 2
        early = sum(pnls[:half]) / half
        late = sum(pnls[half:]) / (len(pnls) - half)
        if early > 0.05 and late < -0.02:
            tips.append('Agrīnā peļņa neseko uz jauniem treidiem — overfitting smells.')
    return {
        'ok': True,
        'n': len(recent),
        'false_signal_rate': round(false_n / len(recent), 3),
        'scratch_rate': round(scratch_n / len(recent), 3),
        'hairline_sl_rate': round(hair_n / len(recent), 3),
        'avg_sl_atr': round(sum(sl_atrs) / len(sl_atrs), 3) if sl_atrs else None,
        'avg_slip_ratio': round(sum(slips) / len(slips), 3) if slips else None,
        'expectancy': round(sum(pnls) / len(pnls), 4) if pnls else None,
        'recent_pnl': round(sum(pnls), 4) if pnls else None,
        'side_conflicts': conflict_n,
        'tips': tips[:4],
        'recent': list(reversed(recent[-8:])),
    }
