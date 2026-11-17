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
        {'scores': {'BUY': 0.6, 'SELL': 0.2, 'HOLD': 0.2}, 'direction': 'BUY',
         'actual_pnl': 1.0},
        {'scores': {'BUY': 0.55, 'SELL': 0.2, 'HOLD': 0.25}, 'direction': 'BUY',
         'actual_pnl': -0.5},
        {'scores': {'BUY': 0.7, 'SELL': 0.1, 'HOLD': 0.2}, 'direction': 'BUY',
         'actual_pnl': 2.0},
        {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'direction': 'BUY',
         'actual_pnl': -0.2},
        {'scores': {'BUY': 0.65, 'SELL': 0.2, 'HOLD': 0.15}, 'direction': 'BUY',
         'actual_pnl': 0.8},
        {'scores': {'BUY': 0.6, 'SELL': 0.25, 'HOLD': 0.15}, 'direction': 'BUY',
         'actual_pnl': -0.1},
    ]
    mean, n = AutoEvolution._replay_trade_cash(always_buy, trades, min_trades=6)
    assert n == 6
    assert mean == pytest.approx(sum(t['actual_pnl'] for t in trades) / 6)


def test_replay_trade_cash_does_not_credit_opposite_with_buy_pnl():
    """SELL candidate must not inherit a BUY trade's +£10 or raw price units."""
    def always_sell(scores, threshold=None):
        return 'SELL'

    # Without counterfactual for SELL → row skipped (not +10).
    bare = [{
        'scores': {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1},
        'direction': 'BUY', 'actual_pnl': 10.0,
        'epic': 'GOLD', 'currency': 'GBP',
    }] * 6
    mean, n = AutoEvolution._replay_trade_cash(always_sell, bare, min_trades=6)
    assert n == 0
    assert mean == float('-inf')
    # Price-unit sim without entry/exit scale → skip (never sum -3 pts into £).
    with_sim = [{
        'scores': {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1},
        'direction': 'BUY', 'actual_pnl': 10.0,
        'simulated': {'buy': 10.0, 'sell': -3.0, 'hold': 0.0, 'method': 'path'},
    }] * 6
    mean2, n2 = AutoEvolution._replay_trade_cash(always_sell, with_sim, min_trades=6)
    assert n2 == 0
    assert mean2 == float('-inf')
    # Explicit account-£ counterfactual is allowed.
    with_cash = [{
        'scores': {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1},
        'direction': 'BUY', 'actual_pnl': 10.0,
        'simulated_cash': {'BUY': 10.0, 'SELL': -7.5, 'HOLD': 0.0},
    }] * 6
    mean3, n3 = AutoEvolution._replay_trade_cash(always_sell, with_cash, min_trades=6)
    assert n3 == 6
    assert mean3 == pytest.approx(-7.5)


def test_policy_cash_rows_join_requires_scores_on_profit():
    profit = [
        {'direction': 'BUY', 'actual_pnl': 1.2, 'epic': 'GOLD', 'currency': 'GBP',
         'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}},
        {'direction': 'SELL', 'actual_pnl': -0.4, 'epic': 'GOLD', 'currency': 'GBP'},  # no scores
        {'direction': 'BUY', 'actual_pnl': 0.8, 'epic': 'GOLD', 'currency': 'GBP',
         'scores': {'BUY': 0.55, 'SELL': 0.2, 'HOLD': 0.25}},
    ]
    joined = AutoEvolution.policy_cash_rows(profit, signal_history=[])
    assert len(joined) == 2
    assert all('scores' in r and 'direction' in r and 'actual_pnl' in r for r in joined)
