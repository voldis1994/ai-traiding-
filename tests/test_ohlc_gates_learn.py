"""OHLC break/zone gate knobs learn from closed broker £."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_entry_zones as oez
from tests.test_ohlc_entry_zones import _swing_candles


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


def test_learned_band_changes_zone_width():
    rows = _swing_candles()
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        wide = dict(oez.load_params(None), band_span_frac=0.20)
        narrow = dict(oez.load_params(None), band_span_frac=0.08)
        oez.save_params(store, wide)
        z_wide = oez.compute_entry_zones(rows, tick=0.1, spread=0.2, store=store)
        oez.save_params(store, narrow)
        z_narrow = oez.compute_entry_zones(rows, tick=0.1, spread=0.2, store=store)
        assert z_wide['ok'] and z_narrow['ok']
        w = float(z_wide['buy_entry']['hi']) - float(z_wide['buy_entry']['lo'])
        n = float(z_narrow['buy_entry']['hi']) - float(z_narrow['buy_entry']['lo'])
        assert w > n


def test_scratch_pnl_does_not_adapt():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        out = oez.observe_close(
            store,
            ohlc_meta={'trigger': 'break', 'action': 'BUY'},
            broker_pnl=0.01, spread=0.2)
        assert out['adapted'] is False
        assert out['reason'] == 'scratch_band'
