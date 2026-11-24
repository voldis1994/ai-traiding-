"""Regime SL/TP: BUY/SELL symmetric rules per scenario."""
from app import ohlc_entry_zones as oez
from app import ohlc_scenarios as osc
from app import ohlc_regime_brackets as orb
from app import bracket_learn
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


def test_trend_zone_buy_sl_behind_demand_tp_toward_supply_or_rr():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    assert buy
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = osc.classify(rows, mid=entry, zones=zones, tick=0.1)
    # Force trend-like name for rule path.
    sc = dict(sc, scenario='TREND_UP', entry_hint='BUY')
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=zones,
        entry_zone=buy, trigger='zone', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert br['source'] == 'regime_brackets'
    assert float(br['stop_level']) < float(buy['lo']) + 1e-9
    assert float(br['profit_level']) > entry
    assert float(br['profit_distance']) / float(br['stop_distance']) + 1e-9 >= 1.9
    assert br.get('zone_sl') is True


def test_trend_zone_sell_sl_behind_supply_symmetric():
    rows, mid = supply_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    sell = zones['sell_entry']
    assert sell
    entry = (float(sell['lo']) + float(sell['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_DOWN', entry_hint='SELL')
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='SELL', scenario=sc, zones=zones,
        entry_zone=sell, trigger='zone', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert float(br['stop_level']) > float(sell['hi']) - 1e-9
    assert float(br['profit_level']) < entry
    assert float(br['profit_distance']) / float(br['stop_distance']) + 1e-9 >= 1.9


def test_breakout_buy_does_not_use_distant_only_floor_sl():
    rows, mid = demand_zone_path(confirm=False)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    tip = float(rows[-1]['close'])
    sc = {
        'ready': True, 'scenario': 'BREAKOUT_UP', 'entry_hint': 'BUY',
        'range': zones and {
            'lo': zones.get('range_lo') or tip - 5,
            'hi': zones.get('range_hi') or tip - 1,
        },
        'bias': 'UP', 'event': 'BOS_UP',
    }
    br = orb.regime_brackets(
        candles=rows, entry=tip, action='BUY', scenario=sc, zones=zones,
        entry_zone=zones.get('buy_entry'), trigger='break', tick=0.1,
        spread=0.2, min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert 'breakout' in (br.get('reason') or '')
    assert float(br['stop_level']) < tip
    assert float(br['profit_level']) > tip


def test_range_edge_buy_tp_at_opposite_edge():
    sc = {
        'ready': True, 'scenario': 'RANGE', 'entry_hint': 'EDGE',
        'range': {'lo': 100.0, 'hi': 110.0, 'span': 10.0},
    }
    # Synthetic flat candles around range.
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(40):
        px = 100.5 + (i % 5) * 0.2
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': px, 'high': px + 0.3, 'low': px - 0.3, 'close': px + 0.05,
        })
    entry = 101.0
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=None,
        trigger='zone', tick=0.1, spread=0.2, min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert float(br['stop_level']) <= 100.0 + 1e-6
    assert float(br['profit_level']) >= 110.0 - 1e-6 or (
        float(br['profit_distance']) / float(br['stop_distance']) >= 1.9)


def test_decide_brackets_prefers_regime_source():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_UP')
    z = bracket_learn.zone_at_open(rows, multi_tf=True)
    decided = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry, action='BUY',
        candles=rows, atr=1.0, require_swing=True, entry_zone=buy,
        scenario=sc, zones=zones, trigger='zone')
    assert decided.get('ready') is True
    assert decided.get('source') == 'regime_brackets'
    assert float(decided['stop_level']) < float(buy['lo']) + 1e-6
    # SL vs TP geometry: TP distance must never be shorter than SL.
    assert float(decided['profit_distance']) + 1e-9 >= float(decided['stop_distance'])


def test_regime_refuses_when_atr_floor_makes_tp_shorter_than_sl():
    """Wide ATR floor must not ship inverted risk (TP < SL)."""
    rows, mid = supply_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    entry = float(rows[-1]['close'])
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='RETEST_UP')
    # Large ATR → stop floor can exceed structural TP distance.
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=zones,
        entry_zone=zones.get('buy_entry'), trigger='zone', tick=0.1,
        spread=0.2, min_distance=0.5, atr=8.0)
    if br.get('ready'):
        assert float(br['profit_distance']) + 1e-9 >= float(br['stop_distance'])
        assert float(br.get('rr') or 0) + 1e-9 >= float(br.get('min_rr') or 1.0)
        assert br.get('rr_capped') is not True
    else:
        assert br.get('reason') in (
            'HOLD_RR', 'regime_rr_below_one', 'no_structural_tp',
            'regime_tp_below_broker_min', 'regime_sl_below_broker_min',
            'regime_tp_below_floor', 'regime_sl_below_floor',
            'no_structural_sl')


def test_regime_brackets_hold_rr_instead_of_soft_cap_sl():
    """V3 B01: RR shortfall → HOLD_RR; structural SL must not move toward entry."""
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones.get('buy_entry')
    entry = (float(buy['lo']) + float(buy['hi'])) / 2 if buy else mid
    # Force a wide structural SL with a near TP so RR < min_rr.
    sc = {
        'scenario': 'RETEST_UP',
        'range': {'lo': entry - 10.0, 'hi': entry + 2.0},
    }
    # Put demand box far below entry so SL is wide.
    wide_demand = {
        'side': 'BUY', 'lo': entry - 10.0, 'hi': entry - 9.0,
        'sl_anchor': entry - 10.0,
    }
    supply = zones.get('sell_entry') or {
        'side': 'SELL', 'lo': entry + 1.5, 'hi': entry + 2.0,
    }
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc,
        zones={'buy_entry': wide_demand, 'sell_entry': supply},
        entry_zone=wide_demand, trigger='zone', tick=0.1,
        spread=0.2, min_distance=0.5, atr=1.0, store=None)
    if br.get('ready'):
        # If structure luckily cleared min_rr, SL must still be structural (no cap).
        assert br.get('rr_capped') is not True
        assert float(br.get('rr') or 0) + 1e-9 >= float(br.get('min_rr') or 1.0)
    else:
        assert br.get('reason') in ('HOLD_RR', 'regime_rr_below_one',
                                    'no_structural_tp', 'no_structural_sl',
                                    'regime_sl_below_broker_min',
                                    'regime_tp_below_broker_min')
        if br.get('reason') == 'HOLD_RR':
            assert br.get('rr_capped') is not True
            # Structural stop kept at invalidation — not soft-capped toward entry.
            assert float(br['stop_distance']) + 1e-9 >= 9.0
            assert float(br.get('rr') or 0) + 1e-9 < float(br.get('min_rr') or 2.0)
