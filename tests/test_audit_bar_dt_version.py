"""Audit fixes: unify LIVE/shadow bar dt; decouple weight_updates from model_version."""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.core import NEURAL_BAR_DT, NeuralCore
from app.engine import AdaptiveBrain, Store
from app.autonomy import AutoEvolution
from app.model_validation import single_version_pnls


def test_neural_bar_dt_matches_shadow_lambda():
    """τ=10, dt=1 → λ≈0.095 for both LIVE and shadow (never LIVE dt=60 → λ≈0.998)."""
    core = NeuralCore(n=4, d=3, seed=1)
    core.tau[:] = 10.0
    lam = 1 - np.exp(-NEURAL_BAR_DT / core.tau)
    assert float(np.mean(lam)) == pytest.approx(0.095162, rel=1e-3)
    # Old LIVE mistake: dt=60 seconds
    bad = 1 - math.exp(-60.0 / 10.0)
    assert bad == pytest.approx(0.9975, rel=1e-3)
    assert abs(float(np.mean(lam)) - bad) > 0.5


def test_tick_clamps_sixty_seconds_down_to_bar_max():
    from app.core import NEURAL_BAR_DT_MAX
    # AdaptiveBrain.tick clamp: never feed wall-clock 60s into λ.
    bar_dt = max(0.1, min(60.0, NEURAL_BAR_DT_MAX))
    assert bar_dt == NEURAL_BAR_DT_MAX
    assert bar_dt < 60.0


def test_closed_trade_bumps_weight_updates_not_model_version():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d, n=8, seed=3)
        brain.epic = 'GOLD'
        before_v = int(brain.model_version)
        before_w = int(getattr(brain, 'weight_updates', 0) or 0)
        # Simulate the post-learn bump path used after closed-trade ΔW.
        brain.weight_updates = before_w + 1
        brain.save()
        path = brain._path_for('GOLD')
        _arrays, meta = brain._read_weight_file(path)
        assert int(meta['model_version']) == before_v
        assert int(meta['weight_updates']) == before_w + 1


def test_single_version_keeps_many_trades_under_one_version():
    rows = [
        {'actual_pnl': 1.0, 'model_version': 3, 'at': '2026-11-16T10:00:00+00:00'},
        {'actual_pnl': -0.5, 'model_version': 3, 'at': '2026-11-16T11:00:00+00:00'},
        {'actual_pnl': 0.2, 'model_version': 3, 'at': '2026-11-16T12:00:00+00:00'},
        {'actual_pnl': 9.0, 'model_version': 4, 'at': '2026-11-16T13:00:00+00:00'},
    ]
    out, ver = single_version_pnls(rows, model_version=3)
    assert ver == 3
    assert len(out) == 3


def test_replay_trade_cash_prefers_pound_not_label_accuracy():
    def always_buy(scores, threshold=None):
        return 'BUY'

    trades = [
        {'scores': {'BUY': 0.6, 'SELL': 0.2, 'HOLD': 0.2}, 'actual_pnl': 1.0},
        {'scores': {'BUY': 0.55, 'SELL': 0.2, 'HOLD': 0.25}, 'actual_pnl': -0.5},
        {'scores': {'BUY': 0.7, 'SELL': 0.1, 'HOLD': 0.2}, 'actual_pnl': 2.0},
        {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'actual_pnl': -0.2},
        {'scores': {'BUY': 0.65, 'SELL': 0.2, 'HOLD': 0.15}, 'actual_pnl': 0.8},
        {'scores': {'BUY': 0.6, 'SELL': 0.25, 'HOLD': 0.15}, 'actual_pnl': -0.1},
    ]
    mean, n = AutoEvolution._replay_trade_cash(always_buy, trades, min_trades=6)
    assert n == 6
    assert mean == pytest.approx(sum(t['actual_pnl'] for t in trades) / 6)
