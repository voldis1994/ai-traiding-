"""OHLC break/zone gate knobs learn from closed broker £."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_entry_zones as oez
from tests.ohlc_zone_path import demand_zone_path


def test_load_params_defaults():
    p = oez.load_params(None)
    assert p['band_span_frac'] == 0.12
    assert p['tape_need'] == 2
    assert p['break_mtf_conf'] == 0.55


def test_observe_close_tightens_break_after_loss():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        before = oez.load_params(store)
        out = oez.observe_close(
            store,
            ohlc_meta={'trigger': 'break', 'action': 'BUY', 'allowed': True},
            broker_pnl=-1.5, spread=0.2)
        assert out['adapted'] is True
        after = oez.load_params(store)
        assert after['tape_need'] >= before['tape_need']
        assert after['break_mtf_conf'] >= before['break_mtf_conf']
        assert after['band_span_frac'] <= before['band_span_frac']


def test_observe_close_narrows_zone_after_loss():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        before = oez.load_params(store)
        out = oez.observe_close(
            store,
            ohlc_meta={'trigger': 'zone', 'action': 'SELL', 'allowed': True},
            broker_pnl=-2.0, spread=0.1)
        assert out['adapted'] is True
        after = oez.load_params(store)
        assert after['band_span_frac'] < before['band_span_frac']


def test_base_impulse_zones_still_build_with_learned_params():
    rows, mid = demand_zone_path(confirm=False)
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        oez.save_params(store, dict(oez.load_params(None), band_span_frac=0.20))
        z = oez.compute_entry_zones(
            rows, mid=mid, tick=0.1, spread=0.2, store=store)
        assert z['ok'] is True
        assert z['buy_entry'] and z['buy_entry']['kind'] == 'demand'
        assert z['params']['band_span_frac'] == 0.20


def test_scratch_pnl_does_not_adapt():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        out = oez.observe_close(
            store,
            ohlc_meta={'trigger': 'break', 'action': 'BUY'},
            broker_pnl=0.01, spread=0.2)
        assert out['adapted'] is False
        assert out['reason'] == 'scratch_band'
