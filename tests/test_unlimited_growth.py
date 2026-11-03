import numpy as np
from app.core import NeuralCore

def test_growth_honours_nmax_cap():
    """Engine always passes max_neurons; nmax=None is test-only escape."""
    core=NeuralCore(n=64)
    core.vbar=2.0
    z=np.ones(core.d)*10
    assert core.grow_if_needed(z, threshold=1, nmax=64) is False
    assert core.n==64
    assert core.grow_if_needed(z, threshold=1, nmax=None)
    assert core.n==65

def test_growth_threshold_one():
    core=NeuralCore(n=16)
    core.vbar=1.1
    z=np.ones(core.d)*10
    assert core.grow_if_needed(z, threshold=1, nmax=32)
    assert core.n==17
