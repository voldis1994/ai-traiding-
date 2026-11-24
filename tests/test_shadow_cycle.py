"""V3 H01: isolated SHADOW NeuralCore; paper still no flip-exit."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.core import NeuralCore
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.shadow_paper import ShadowPaperJournal


def test_shadow_is_isolated_from_live():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain.core.last_z = np.zeros(brain.core.d)
        brain.core.last_z[0] = 0.4
        e = SingleTradeEngine(store, brain)
        shadow = e._ensure_shadow_brain()
        assert shadow is not brain.core
        scores = e._shadow_scores_now(advance=True, bar_time='2024-01-01T00:01:00+00:00')
        assert scores is not None
        assert set(scores) == {'BUY', 'SELL', 'HOLD'}
        e.shadow_brain = None
        shadow2 = e._ensure_shadow_brain()
        assert shadow2 is not brain.core


def test_shadow_learn_does_not_teach_live_core():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain.core.last_z = np.linspace(-0.2, 0.2, brain.core.d)
        e = SingleTradeEngine(store, brain)
        e._shadow_scores_now(advance=True, bar_time='t0')
        live_before = brain.core.snapshot_weights()
        shadow_before = e.shadow_brain.snapshot_weights()
        assert e._shadow_learn(None, 0, lr=0.1) is not None
        assert NeuralCore.weight_delta_norm(
            shadow_before, e.shadow_brain.snapshot_weights()) > 0
        assert NeuralCore.weight_delta_norm(
            live_before, brain.core.snapshot_weights()) == pytest.approx(0)
        assert e.shadow_brain is not brain.core


def test_no_flip_exit_vs_live_trail_rules():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        kw = dict(slip_bps=0., fill_delay_bars=0)
        assert j.on_bar('BUY', mid=100., spread=0.2, bar_time='b1',
                        high=100.3, low=99.8, tick=0.1,
                        stop_distance=2.0, profit_distance=4.0, **kw) is None
        assert j.position is not None
        entry = j.position['entry']
        stop = j.position['stop']
        assert j.on_bar('SELL', mid=100.5, spread=0.2, bar_time='b2',
                        high=100.8, low=100.1, tick=0.1, **kw) is None
        assert j.position is not None
        assert j.position['direction'] == 'BUY'
        assert all(r.get('exit_reason') != 'flip' for r in j.history)
        assert j.on_bar('HOLD', mid=101.5, spread=0.2, bar_time='b3',
                        high=102.0, low=101.0, tick=0.1, **kw) is None
        assert j.position is not None
        closed = j.on_bar('SELL', mid=float(stop) - 1.0, spread=0.2, bar_time='b4',
                          high=float(stop) + 0.2, low=float(stop) - 1.5, tick=0.1, **kw)
        assert closed is not None
        assert closed['exit_reason'] in ('sl', 'trail', 'tp')
        assert closed['exit_reason'] != 'flip'
        assert closed['entry'] == pytest.approx(entry)


def test_shadow_paper_reads_exec_friction_from_self_params():
    """Critical #4: friction from Store self_params — not module constants."""
    from app.self_params import DEFAULTS, save
    assert DEFAULTS['exec_fill_delay_bars'] >= 1
    assert DEFAULTS['exec_slip_bps'] > 0
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**DEFAULTS, 'exec_fill_delay_bars': 1, 'exec_slip_bps': 2.0},
             reason='test')
        j = ShadowPaperJournal(store)
        j.on_bar('BUY', mid=100., spread=0.2, bar_time='t1',
                 high=100.2, low=99.8, tick=0.1,
                 stop_distance=1.5, profit_distance=3.0)
        assert j.position is None
        assert j.pending is not None
        j.on_bar('HOLD', mid=100.1, spread=0.2, bar_time='t2',
                 high=100.3, low=99.9, tick=0.1)
        assert j.position is not None
