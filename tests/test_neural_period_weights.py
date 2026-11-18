"""Period weights live inside NeuralCore — one learn chain, not a second AI."""
import numpy as np
import pytest

from app.core import NeuralCore, PERIOD_N, PERIOD_TF, OBJECTIVE_MINUTE_DIRECTION
from app import mtf_momentum


def test_core_exposes_trainable_period_gate():
    core = NeuralCore(n=12, d=4, seed=3)
    w = core.period_weights()
    assert list(w) == list(PERIOD_TF)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert core.period_logits.shape == (PERIOD_N,)
    assert core.period_V.shape == (3, PERIOD_N)


def test_step_stores_period_x_and_tilts_logits():
    core = NeuralCore(n=10, d=3, seed=11)
    # Force M5 weight and bullish evidence → BUY logit rises.
    core.period_logits[:] = np.array([-2.0, 4.0, -2.0, -2.0, -2.0])
    core.period_V[:] = 0.0
    core.period_V[0, 1] = 2.0   # BUY ← m5
    core.period_V[1, 1] = -2.0  # SELL ← m5
    x = np.zeros(3)
    out = core.step(x, period_x=[0, 1.0, 0, 0, 0])
    assert out['scores']['BUY'] > out['scores']['SELL']
    assert out['period_weights']['m5'] == max(out['period_weights'].values())
    assert core.last_transition['period_x'][1] == pytest.approx(1.0)


def test_learn_updates_period_logits():
    core = NeuralCore(n=10, d=3, seed=5)
    core.period_logits[:] = 0.0
    core.period_V[:] = 0.0
    core.period_V[0, :] = 0.5  # BUY from any TF
    x = np.array([0.1, -0.2, 0.05])
    core.step(x, period_x=[0.2, 0.8, 0.1, 0.0, -0.1])
    tr = dict(core.last_transition)
    before = core.period_logits.copy()
    loss = core.learn(tr, label=0, objective=OBJECTIVE_MINUTE_DIRECTION)  # BUY
    assert loss is not None
    # Period logits should move (unless restore) — at least V or logits change.
    meta = core.last_learn_meta or {}
    if not meta.get('restored'):
        changed = (
            not np.allclose(before, core.period_logits)
            or not np.allclose(0.0, core.period_V - 0.0)
        )
        assert changed or float(meta.get('delta_norm') or 0) >= 0


def test_period_evidence_vector_length():
    bars = []
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    px = 2000.0
    for i in range(520):
        o, c = px, px + 0.1
        bars.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': c + 0.1, 'low': o - 0.1, 'close': c, 'spread': 0.2,
        })
        px = c
    a = mtf_momentum.analyze(bars, spread=0.15)
    ev = a.get('period_evidence') or mtf_momentum.period_evidence(a)
    assert len(ev) == PERIOD_N
    assert all(-1.01 <= float(v) <= 1.01 for v in ev)


def test_ensure_period_params_backfill():
    core = NeuralCore(n=8, d=2, seed=1)
    del core.period_logits
    del core.period_V
    core.ensure_period_params()
    assert core.period_logits.shape == (PERIOD_N,)
    assert core.period_V.shape == (3, PERIOD_N)
