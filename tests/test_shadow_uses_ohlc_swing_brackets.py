"""Shadow paper must use the same OHLC swing/regime SL/TP as LIVE — never invent."""
import tempfile
from pathlib import Path

import pytest

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.shadow_paper import simulate_action_on_path


def test_simulate_refuses_missing_brackets_no_2tick_invent():
    bars = [{'mid': 100., 'high': 101., 'low': 99.} for _ in range(5)]
    assert simulate_action_on_path('BUY', bars, spread=0.1) is None
    assert simulate_action_on_path(
        'BUY', bars, spread=0.1, stop_distance=0., profit_distance=2.) is None
    ok = simulate_action_on_path(
        'BUY', bars, spread=0.1, stop_distance=1.5, profit_distance=3.0, tick=0.01)
    assert ok is not None and ok.get('entry') is not None


def test_shadow_paper_step_uses_would_be_brackets_not_stop_take(monkeypatch):
    """_shadow_paper_step must call _would_be_brackets — not zone+stop_take invent."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': 1}
        e.market = {
            'mid': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
            'bid': 1999.9, 'ask': 2000.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        e._ensure_shadow_brain()
        called = {}

        def fake_wb(proposal):
            called['proposal'] = proposal
            return {
                'entry': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
                'stop_distance': 4.5, 'profit_distance': 9.0,
                'bracket_source': 'swing_anchor', 'ohlc_kappa': 2.0,
                'epic': 'GOLD',
            }

        monkeypatch.setattr(e, '_would_be_brackets', fake_wb)
        monkeypatch.setattr(
            e, '_shadow_scores_now',
            lambda **kw: {'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1})
        # Force delay 0 so open happens this bar.
        from app import self_params
        monkeypatch.setattr(
            self_params, 'load',
            lambda store: {**self_params.DEFAULTS, 'exec_slip_bps': 0.,
                           'exec_fill_delay_bars': 0})
        e._shadow_paper_step(
            mid=2000., spread=0.2, bar_time='2026-11-23T12:00:00+00:00',
            high=2001., low=1999., candles=[])
        assert called.get('proposal') == 'BUY'
        pos = e.shadow_paper.position
        assert pos is not None
        # Distances from swing brackets — not invented 2×tick.
        assert abs(pos['entry'] - pos['stop']) == pytest.approx(4.5, abs=0.5)
        assert abs(pos['profit'] - pos['entry']) == pytest.approx(9.0, abs=0.5)


def test_shadow_paper_skips_open_when_swing_brackets_unavailable(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': 1}
        e.market = {
            'mid': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
            'bid': 1999.9, 'ask': 2000.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        e._ensure_shadow_brain()
        monkeypatch.setattr(e, '_would_be_brackets', lambda proposal: None)
        monkeypatch.setattr(
            e, '_shadow_scores_now',
            lambda **kw: {'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1})
        from app import self_params
        monkeypatch.setattr(
            self_params, 'load',
            lambda store: {**self_params.DEFAULTS, 'exec_slip_bps': 0.,
                           'exec_fill_delay_bars': 0})
        e._shadow_paper_step(
            mid=2000., spread=0.2, bar_time='2026-11-23T12:01:00+00:00',
            high=2001., low=1999., candles=[])
        assert e.shadow_paper.position is None
        assert e.shadow_paper.pending is None
