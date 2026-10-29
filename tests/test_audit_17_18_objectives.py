"""Audit #17–18: separate minute vs trade objectives; controlled weight updates."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.autonomy import AutoEvolution
from app.core import (
    DEFAULT_MAX_WEIGHT_DELTA_NORM,
    MINUTE_VS_TRADE_WEIGHT_RATIO,
    NeuralCore,
    OBJECTIVE_MINUTE_DIRECTION,
    OBJECTIVE_TRADE_PNL,
    OBJECTIVE_WEIGHTS,
)
from app.engine import AdaptiveBrain, Store
from app import machine_spec


def _transition(core):
    core.step(np.array([0.1, 0.2, -0.1]), dt=1.0)
    return {k: (v.copy() if hasattr(v, 'copy') else v)
            for k, v in core.last_transition.items()}


def test_objective_weights_document_trade_primary():
    assert OBJECTIVE_WEIGHTS[OBJECTIVE_TRADE_PNL] == 1.0
    assert OBJECTIVE_WEIGHTS[OBJECTIVE_MINUTE_DIRECTION] == 0.2
    assert MINUTE_VS_TRADE_WEIGHT_RATIO == pytest.approx(0.2)
    assert machine_spec.LEARN_OBJECTIVES[OBJECTIVE_TRADE_PNL] == 1.0
    assert machine_spec.MINUTE_VS_TRADE_WEIGHT_RATIO == pytest.approx(0.2)
    assert AdaptiveBrain.TRADE_OBJECTIVE_WEIGHT == 1.0
    assert AdaptiveBrain.MINUTE_OBJECTIVE_WEIGHT == 0.2
    # Trade-PnL is 5× stronger per step than minute-direction.
    assert (OBJECTIVE_WEIGHTS[OBJECTIVE_TRADE_PNL]
            / OBJECTIVE_WEIGHTS[OBJECTIVE_MINUTE_DIRECTION]) == pytest.approx(5.0)


def test_minute_and_trade_counters_stay_separate():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        for i in range(6):
            brain.tick(100 + i * 1.0, 0.5, 0.01, 'GOLD')
        minute_n = brain.minute_labeled
        assert minute_n == 5
        assert brain.labeled == minute_n  # compat alias
        assert brain.trade_labeled == 0
        # Closed-trade path must not inflate minute counters.
        tr = brain.core.last_transition
        assert tr is not None
        loss = brain.learn_from_trade(tr, label=0, lr=0.05, won=True)
        assert loss is not None
        assert brain.trade_labeled == 1
        assert brain.trade_correct == 1
        assert brain.minute_labeled == minute_n
        assert brain.labeled == minute_n
        # More minute ticks still leave trade tally intact.
        for i in range(6, 12):
            brain.tick(100 + i * 1.0, 0.5, 0.01, 'GOLD')
        assert brain.minute_labeled > minute_n
        assert brain.trade_labeled == 1
        assert brain.trade_correct == 1


def test_minute_objective_uses_weaker_effective_lr_than_trade():
    rng_seed = 11
    core_m = NeuralCore(n=8, d=3, seed=rng_seed)
    core_t = NeuralCore(n=8, d=3, seed=rng_seed)
    tr_m = _transition(core_m)
    tr_t = _transition(core_t)
    # Same transition geometry on identical inits.
    np.testing.assert_allclose(tr_m['z'], tr_t['z'])
    before_m = core_m.snapshot_weights()
    before_t = core_t.snapshot_weights()
    core_m.learn(tr_m, 0, lr=0.2, objective=OBJECTIVE_MINUTE_DIRECTION,
                 restore_if_worsens=False, max_delta_norm=100.0)
    core_t.learn(tr_t, 0, lr=0.2, objective=OBJECTIVE_TRADE_PNL,
                 restore_if_worsens=False, max_delta_norm=100.0)
    dm = NeuralCore.weight_delta_norm(before_m, core_m.snapshot_weights())
    dt = NeuralCore.weight_delta_norm(before_t, core_t.snapshot_weights())
    assert dt > dm > 0
    assert core_m.last_learn_meta['objective_weight'] == pytest.approx(0.2)
    assert core_t.last_learn_meta['objective_weight'] == pytest.approx(1.0)
    assert core_m.last_learn_meta['proven_improvement'] is False
    assert core_t.last_learn_meta['proven_improvement'] is False


def test_weight_update_bounded_by_max_delta_norm():
    core = NeuralCore(n=8, d=3, seed=3)
    tr = _transition(core)
    before = core.snapshot_weights()
    # Tiny cap forces clipping on a real gradient step.
    core.learn(tr, 1, lr=0.5, objective=OBJECTIVE_TRADE_PNL,
               max_delta_norm=0.05, restore_if_worsens=False)
    meta = core.last_learn_meta
    assert meta['clipped'] is True
    assert meta['delta_norm'] == pytest.approx(0.05, rel=1e-3, abs=1e-3)
    assert meta['proven_improvement'] is False
    after = core.snapshot_weights()
    assert NeuralCore.weight_delta_norm(before, after) == pytest.approx(
        0.05, rel=1e-3, abs=1e-3)


def test_restore_when_ce_criterion_worsens():
    with tempfile.TemporaryDirectory() as d:
        core = NeuralCore(n=8, d=3, seed=5)
        tr = _transition(core)
        before = core.snapshot_weights()
        store = Store(Path(d) / 'db.sqlite3')
        evo = AutoEvolution(store, Path(d) / 'policy.py')
        # criterion_fn higher=better; return decreasing values → restore.
        calls = {'n': 0}

        def criterion(_core):
            calls['n'] += 1
            return 10.0 - calls['n']  # 9 then 8 → worsens

        # Bypass CE restore inside learn; external criterion triggers restore.
        out = evo.controlled_weight_learn(
            core, tr, 0, lr=0.2, objective=OBJECTIVE_TRADE_PNL,
            max_delta_norm=DEFAULT_MAX_WEIGHT_DELTA_NORM,
            restore_if_worsens=False, criterion_fn=criterion)
        assert out['proven_improvement'] is False
        assert out['restored'] is True
        assert out['applied'] is False
        after = core.snapshot_weights()
        for name in before:
            np.testing.assert_allclose(after[name], before[name])


def test_ordinary_learn_never_proven_improvement():
    core = NeuralCore(n=8, d=3, seed=9)
    tr = _transition(core)
    core.learn(tr, 2, lr=0.1, objective=OBJECTIVE_MINUTE_DIRECTION)
    assert core.last_learn_meta['proven_improvement'] is False
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain.tick(100.0, 0.5, 0.01, 'GOLD')
        brain.tick(101.0, 0.5, 0.01, 'GOLD')
        meta = brain.core.last_learn_meta
        assert meta is not None
        assert meta['objective'] == OBJECTIVE_MINUTE_DIRECTION
        assert meta['proven_improvement'] is False
        brain.learn_from_trade(brain.core.last_transition, 1, lr=0.1, won=False)
        assert brain.core.last_learn_meta['proven_improvement'] is False
        assert brain.core.last_learn_meta['objective'] == OBJECTIVE_TRADE_PNL


def test_snapshot_restore_roundtrip_helpers():
    core = NeuralCore(n=8, d=3, seed=2)
    snap = AutoEvolution.snapshot_core_weights(core)
    core.P += 0.5
    assert AutoEvolution.restore_core_weights(core, snap) is True
    np.testing.assert_allclose(core.P, snap['P'])
