"""1m scenario classify + self-audit when zone/scenario analysis was wrong."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_scenarios as osc
from app import ohlc_entry_zones as oez
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


def test_classify_trend_up_after_demand_impulse():
    rows, _ = demand_zone_path(confirm=False)
    sc = osc.classify(rows, tick=0.1)
    assert sc['ready'] is True
    assert sc['scenario'] in (
        'TREND_UP', 'BREAKOUT_UP', 'RETEST_UP', 'CHOCH_UP', 'RANGE',
        'COMPRESSION',
    )
    # After UP impulse, hint should not prefer SELL.
    assert sc.get('entry_hint') in ('BUY', 'EDGE', 'WAIT', None) or sc['entry_hint'] != 'SELL'


def test_mid_range_blocked_in_range_box():
    rows, _ = demand_zone_path(confirm=False)
    # Force RANGE-like box around mid span.
    sc = {
        'ready': True, 'scenario': 'RANGE', 'block_mid_range': True,
        'range': {'lo': 100.0, 'hi': 110.0, 'span': 10.0},
    }
    blocked, reason = osc.mid_range_blocked('BUY', sc, 105.0, tick=0.1)
    assert blocked is True
    assert 'mid_range' in (reason or '')
    # Edge near support — allowed.
    blocked2, _ = osc.mid_range_blocked('BUY', sc, 100.5, tick=0.1)
    assert blocked2 is False


def test_scenario_blocks_blind_break_in_range():
    sc = {
        'ready': True, 'enabled': True, 'scenario': 'RANGE',
        'scenarios': ['RANGE'], 'entry_hint': 'EDGE', 'confidence': 0.6,
        'fakeout': False,
    }
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        osc.save_params(store, {
            **osc.load_params(None),
            'prefer_retest_over_blind_break': 1,
        })
        gate = osc.scenario_allows_trigger('BUY', sc, 'break', store=store)
        assert gate['ok'] is False
        assert 'range' in (gate['reason'] or '')


def test_fakeout_blocks_break_trigger():
    sc = {
        'ready': True, 'enabled': True, 'scenario': 'FAKEOUT',
        'scenarios': ['FAKEOUT'], 'entry_hint': 'WAIT', 'confidence': 0.8,
        'fakeout': True,
    }
    gate = osc.scenario_allows_trigger('BUY', sc, 'break', store=None)
    assert gate['ok'] is False
    assert 'fakeout' in (gate['reason'] or '')


def test_entry_gate_stamps_scenario():
    rows, mid = demand_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert isinstance(gate.get('scenario'), dict)
    assert gate['scenario'].get('ready') is True
    assert gate['scenario'].get('scenario')


def test_self_audit_raises_zone_penalty_on_zone_fail():
    rows, mid = demand_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    # Path that closes through demand low after OPEN.
    through = list(rows)
    last = through[-1]
    px = float(buy['lo']) - 1.5
    through.append({
        'at': last['at'], 'open': float(buy['lo']),
        'high': float(buy['lo']) + 0.1, 'low': px - 0.2, 'close': px,
    })
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        before = osc.load_params(store)
        out = osc.audit_close(
            store,
            scenario_meta={'scenario': 'TREND_UP', 'confidence': 0.7},
            ohlc_meta={
                'action': 'BUY', 'trigger': 'zone',
                'zone': buy, 'allowed': True,
            },
            candles=through,
            broker_pnl=-2.5, spread=0.2, action='BUY',
        )
        assert out['audited'] is True
        assert 'zone_fail' in (out.get('mistakes') or [])
        after = osc.load_params(store)
        assert after['penalty_zone'] > before['penalty_zone']
        mistakes = store.get(osc.MISTAKES_KEY) or []
        assert mistakes


def test_self_audit_eases_penalties_after_win():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        osc.save_params(store, {
            **osc.load_params(None),
            'penalty_zone': 0.2,
            'penalty_trend': 0.15,
        })
        out = osc.audit_close(
            store,
            scenario_meta={'scenario': 'TREND_UP'},
            ohlc_meta={'action': 'BUY', 'trigger': 'zone'},
            candles=[],
            broker_pnl=3.0, spread=0.2, action='BUY',
        )
        assert out['audited'] is True
        assert out.get('adapted') is True
        after = osc.load_params(store)
        assert after['penalty_zone'] < 0.2


def test_open_meta_includes_scenario_and_zone():
    rows, mid = demand_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    meta = oez.open_meta_from_gate(gate, action='BUY')
    assert meta.get('scenario', {}).get('scenario')
    assert meta.get('zone')


def test_s08_unready_disabled_unknown_are_not_ok():
    """V3 T08/S08: UNKNOWN / unready / disabled → HOLD_SCENARIO, never ok=True."""
    # Unready
    gate = osc.scenario_allows_trigger(
        'BUY', {'ready': False, 'scenario': 'TREND_UP'}, 'zone')
    assert gate['ok'] is False
    assert gate['reason'] == 'HOLD_SCENARIO'
    assert gate.get('detail') == 'scenario_unready'
    assert gate.get('ready') is False

    # Missing / non-dict
    gate2 = osc.scenario_allows_trigger('BUY', None, 'break')
    assert gate2['ok'] is False
    assert gate2['reason'] == 'HOLD_SCENARIO'

    # Disabled
    gate3 = osc.scenario_allows_trigger(
        'BUY',
        {'ready': True, 'enabled': False, 'scenario': 'TREND_UP',
         'entry_hint': 'BUY', 'confidence': 0.8},
        'zone')
    assert gate3['ok'] is False
    assert gate3['reason'] == 'HOLD_SCENARIO'
    assert gate3.get('detail') == 'scenario_disabled'

    # UNKNOWN primary
    gate4 = osc.scenario_allows_trigger(
        'BUY',
        {'ready': True, 'enabled': True, 'scenario': 'UNKNOWN',
         'entry_hint': 'WAIT', 'confidence': 0.5},
        'zone')
    assert gate4['ok'] is False
    assert gate4['reason'] == 'HOLD_SCENARIO'

    # CHOP
    gate5 = osc.scenario_allows_trigger(
        'SELL',
        {'ready': True, 'enabled': True, 'scenario': 'CHOP',
         'entry_hint': 'WAIT', 'confidence': 0.4},
        'break')
    assert gate5['ok'] is False
    assert gate5['reason'] == 'HOLD_SCENARIO'


def test_s08_compression_blind_break_holds():
    sc = {
        'ready': True, 'enabled': True, 'scenario': 'COMPRESSION',
        'scenarios': ['COMPRESSION'], 'entry_hint': 'WAIT', 'confidence': 0.5,
    }
    gate = osc.scenario_allows_trigger('BUY', sc, 'break')
    assert gate['ok'] is False
    assert 'HOLD_SCENARIO' in (gate.get('reason') or '')


def test_s09_ready_named_scenario_can_pass_zone():
    sc = {
        'ready': True, 'enabled': True, 'scenario': 'TREND_UP',
        'scenarios': ['TREND_UP'], 'entry_hint': 'BUY', 'confidence': 0.7,
    }
    gate = osc.scenario_allows_trigger('BUY', sc, 'zone')
    assert gate['ok'] is True
    assert gate['reason'] == 'scenario_ok'
    assert gate.get('hold') is None
    assert gate.get('ready') is True
