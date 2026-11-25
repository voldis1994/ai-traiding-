"""LIVE SL/TP must use 1m OHLC geometry — not M5 ATR floor invent."""
from app.engine import SingleTradeEngine
from app import bracket_learn
from tests.ohlc_zone_path import demand_zone_path
from app import ohlc_entry_zones as oez
from app import ohlc_scenarios as osc


def test_bracket_m1_hints_prefers_m1_atr_over_m5_stop_hint():
    class _E(SingleTradeEngine):
        def __init__(self):
            pass
    e = _E()
    mtf = {
        'ready': True,
        'stop_hint_atr14': 15.0,  # M5 — must NOT win
        'tf': {
            'm1': {'atr14': 3.2, 'kama': 100.1},
            'm5': {'atr14': 15.0, 'kama': 100.5},
        },
    }
    atr, kama = e._bracket_m1_hints(mtf)
    assert atr == 3.2
    assert kama == 100.1


def test_bracket_m1_hints_no_m5_stop_hint_fallback():
    class _E(SingleTradeEngine):
        def __init__(self):
            pass
    e = _E()
    atr, kama = e._bracket_m1_hints({
        'stop_hint_atr14': 8.0,  # M5 — must stay unused for LIVE floor
        'tf': {'m1': {}, 'm5': {'kama': 99.0}},
    })
    assert atr is None
    assert kama == 99.0


def test_swing_refuses_soft_cap_keeps_structural_sl():
    """Far 1m swing SL must not be pulled to entry±fantasy for RR."""
    # Wide SL vs tiny TP geometry → apply_min_rr would want to soft-cap.
    rows, mid = demand_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    # Force tiny opposite by using entry near the high swing with zone SL far —
    # if soft-cap were allowed, stop would move off zone.lo.
    swing = bracket_learn.swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0, entry_zone=buy)
    if swing.get('ready'):
        # Structural stop stays behind the zone (1m OHLC), never mid-box invent.
        assert float(swing['stop_level']) < float(buy['lo']) + 1e-6
        assert 'rr_cap_sl' not in str(swing.get('reason') or '')
    else:
        assert swing.get('reason') in (
            'rr_keep_structural_sl', 'rr_unachievable_vs_floor', 'rr_fail',
            'rr_unachievable', 'swing_distances_below_floor',
            'no_structural_swings_for_entry')


def test_decide_uses_m1_zone_medians_with_entry_zone():
    rows, mid = demand_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = osc.classify(rows, mid=entry, zones=zones, tick=0.1)
    z = bracket_learn.zone_at_open(rows, multi_tf=False)
    decided = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry, action='BUY',
        candles=rows, atr=1.0, require_swing=True, entry_zone=buy,
        scenario=sc, zones=zones, trigger='zone')
    assert decided.get('ready') is True, decided
    assert decided.get('source') in ('swing_anchor', 'regime_brackets')
    assert float(decided['stop_level']) < entry
    assert float(decided['profit_level']) > entry
