import numpy as np
from app.core import NeuralCore

def test_unlimited_growth_above_128():
    core=NeuralCore(n=128)
    core.vbar=2.0
    z=np.ones(core.d)*10
    assert core.grow_if_needed(z, threshold=1, nmax=None)
    assert core.n==129

def test_growth_threshold_one():
    core=NeuralCore(n=16)
    core.vbar=1.1
    z=np.ones(core.d)*10
    assert core.grow_if_needed(z, threshold=1)
    assert core.n==17
