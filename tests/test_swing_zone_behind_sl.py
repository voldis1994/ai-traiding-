"""OHLC swing SL/TP must use Demand/Supply zone-behind when pivots alone miss."""
from app import bracket_learn
from app import ohlc_entry_zones as oez
from app import ohlc_scenarios as osc
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


def test_swing_anchor_zone_behind_on_demand_touch():
    rows, mid = demand_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    assert buy
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    swing = bracket_learn.swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0, entry_zone=buy)
    assert swing.get('ready') is True, swing
    assert swing.get('source') == 'swing_anchor'
    assert float(swing['stop_level']) < float(buy['lo']) + 1e-6
    assert float(swing['profit_level']) > entry
    assert bool(swing.get('zone_sl')) is True
    assert 'zone_behind' in str(swing.get('reason') or '')


def test_swing_anchor_zone_behind_on_supply_touch():
    rows, mid = supply_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    sell = zones['sell_entry']
    assert sell
    entry = (float(sell['lo']) + float(sell['hi'])) / 2
    swing = bracket_learn.swing_anchor_brackets(
        candles=rows, entry=entry, action='SELL', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0, entry_zone=sell)
    assert swing.get('ready') is True, swing
    assert float(swing['stop_level']) > float(sell['hi']) - 1e-6
    assert float(swing['profit_level']) < entry
    assert bool(swing.get('zone_sl')) is True


def test_decide_returns_swing_anchor_with_entry_zone():
    rows, mid = demand_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = osc.classify(rows, mid=entry, zones=zones, tick=0.1)
    z = bracket_learn.zone_at_open(rows, multi_tf=True)
    decided = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry, action='BUY',
        candles=rows, atr=1.0, require_swing=True, entry_zone=buy,
        scenario=sc, zones=zones, trigger='zone')
    assert decided.get('ready') is True, decided
    assert decided.get('source') == 'swing_anchor'
    assert float(decided['stop_level']) < entry
    assert float(decided['profit_level']) > entry


def test_propose_for_open_derives_entry_zone_for_swing():
    import tempfile
    from pathlib import Path
    from app.engine import Store
    rows, mid = demand_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        bp = bracket_learn.propose_for_open(
            store, candles=rows, tick=0.1, spread=0.2, min_distance=0.5,
            entry=entry, action='BUY', atr=1.0, require_swing=True)
        assert bp.get('ready') is True, bp
        assert bp.get('bracket_source') in ('swing_anchor', 'regime_brackets')
        assert float(bp['stop_level']) < entry
        assert float(bp['profit_level']) > entry
