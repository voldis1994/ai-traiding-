"""Clients must not diverge into runaway neuron counts after losses.

Live symptom: one client 196 neurons / gen 127, peers ~55–60, M1 ~36% —
loss eased grow threshold + nmax=None + Paaudze on every teach.
"""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.core import NeuralCore
from app.engine import Store
from app.self_params import DEFAULTS, BOUNDS, evolve_from_feedback, load, get


def test_max_neurons_default_and_bounds():
    assert DEFAULTS['max_neurons'] == 64
    assert BOUNDS['max_neurons'][0] >= 24
    assert BOUNDS['max_neurons'][1] <= 96
    assert DEFAULTS['birth_cooldown_steps'] >= 8
    assert DEFAULTS['hold_shadow_repeats'] <= 2


def test_meaningful_loss_calms_lr_and_hardens_birth():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        lr0 = get(store, 'learn_lr')
        grow0 = load(store)['grow_error_threshold']
        evolve_from_feedback(store, broker_pnl=-2.0)
        after = load(store)
        assert after['learn_lr'] == pytest.approx(round(lr0 - 0.01, 2))
        assert after['grow_error_threshold'] >= grow0


def test_grow_respects_nmax_and_prune_to_cap():
    core = NeuralCore(n=16)
    core.vbar = 5.0
    z = np.ones(core.d) * 20
    assert core.grow_if_needed(z, threshold=0.1, nmax=16) is False
    assert core.n == 16
    # Force over size then trim.
    core.utility = np.linspace(-1, 1, core.n)
    trimmed = core.prune_to_cap(12, min_neurons=8)
    assert trimmed == 4
    assert core.n == 12


def test_newborn_g_cuts_reconstruction():
    """Zero-G births used to leave error unchanged → re-birth every interval."""
    core = NeuralCore(n=8)
    core.vbar = 5.0
    z = np.linspace(1.0, 2.0, core.d)
    err0 = float(np.linalg.norm(z - core.G @ core.h))
    assert core.grow_if_needed(z, threshold=0.01, nmax=64)
    err1 = float(np.linalg.norm(z - core.G @ core.h))
    assert err1 < err0
