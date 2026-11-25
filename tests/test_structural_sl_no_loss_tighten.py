"""Audit fix: loss aggression must not shrink swing SL toward entry."""
from app import bracket_learn as bl
from app import unified_cycle as uc


def _bar(o, h, l, c):
    return {'open': o, 'high': h, 'low': l, 'close': c, 'mid': c}


def test_swing_brackets_ignore_tighten_below_one():
    # Clear swing: lows at 100, highs at 110 — BUY entry mid-structure.
    rows = []
    for i in range(30):
        base = 100.0 + (i % 5) * 0.2
        rows.append(_bar(base, base + 2.0, base - 1.5, base + 0.5))
    # Explicit swing low / high pivots
    rows[10] = _bar(102, 103, 98.0, 102.5)   # swing low
    rows[20] = _bar(108, 112.0, 107, 110.0)  # swing high
    entry = 105.0
    zone = bl.zone_at_open(rows, multi_tf=False)
    full = bl.decide_brackets_from_ohlc(
        zone, candles=rows, entry=entry, action='BUY', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.5, tighten=0.70, require_swing=True)
    assert full.get('ready') is True, full.get('reason')
    assert full.get('source') == 'swing_anchor'
    # Distances must match structural levels — not 0.70× them.
    assert float(full['tighten']) == 1.0
    assert abs(float(full['stop_distance']) - float(full['ohlc_sl'])) < 1e-9


def test_compose_open_reports_aggression_but_tighten_one():
    state = uc.default_state()
    state['aggression'] = 0.70
    state['loss_streak'] = 3
    pkg = uc.compose_open(
        action='SELL', scores={'SELL': 0.55, 'BUY': 0.25, 'HOLD': 0.20},
        evaluation={'action': 'SELL'},
        zone={'kappa': 2.0, 'range_distance': 10.0, 'max_tp': 20.0},
        learned={'sl_scale': 0.5}, cycle_state=state, base_sl=10.0)
    assert pkg['blocked'] is False
    assert pkg['tighten'] == 1.0
    assert pkg['aggression'] == 0.70
