"""LIVE neural weights must move from closed-trade learning — not cosmetic counters."""
import asyncio
import math
from pathlib import Path

import numpy as np

from app.core import NeuralCore, client_neural_seed
from app import learning_generation
from app.engine import AdaptiveBrain, SingleTradeEngine, Store


def _transition(core: NeuralCore):
    z=np.zeros(core.d);z[0]=0.4
    out=core.step(np.ones(core.d)*0.1,dt=1.0)
    assert out['scores']
    return core.last_transition


def test_closed_trade_learn_changes_live_weights_and_bumps_paaudze(tmp_path):
    d=tmp_path/'c';d.mkdir()
    store=Store(d/'robot.sqlite3')
    brain=AdaptiveBrain(store,data_dir=d,seed=client_neural_seed(3))
    engine=SingleTradeEngine(store,brain,policy_path=d/'policy.py')
    brain.epic='GOLD'
    tr=_transition(brain.core)
    before=brain.core.P.copy()
    gen0=learning_generation.current(store)
    deal='deal-adapt-1'
    store.set('entry_brain:'+deal,{
        'epic':'GOLD','direction':'BUY','entry':100.0,'tick_size':0.1,
        'entry_spread':0.05,'transition':brain._json_transition(tr),
        'captured':'test'})
    engine._learn_closed_trade(deal,{
        'level':99.0,'profit':-1.25,'pnl_currency':'GBP','source':'broker'})
    after=brain.core.P
    assert not np.allclose(before,after), 'LIVE weights must move after loss regret'
    assert learning_generation.current(store)>gen0
    reason=store.get('neural_adapt_reason') or ''
    assert 'LIVE learn' in reason or 'Closed' in reason


def test_shadow_advance_mutates_shadow_state(tmp_path):
    d=tmp_path/'s';d.mkdir()
    store=Store(d/'robot.sqlite3')
    brain=AdaptiveBrain(store,data_dir=d,seed=9)
    engine=SingleTradeEngine(store,brain,policy_path=d/'policy.py')
    brain.core.step(np.ones(brain.core.d)*0.2,dt=1.0)
    sh=engine._ensure_shadow_brain()
    # Align dim: fresh shadow may differ in n but d matches.
    if sh.d!=brain.core.d:
        # force same last_z length
        brain.core.last_z=np.zeros(sh.d)
    h0=sh.h.copy()
    scores=engine._shadow_scores_now(advance=True)
    assert scores and math.isclose(sum(scores.values()),1.0,rel_tol=1e-5)
    assert not np.allclose(h0,sh.h), 'shadow recurrent state must advance on M1'


def test_learning_generation_display_ignores_policy_file_gen(tmp_path):
    store=Store(tmp_path/'x.db')
    store.set('learning_generation',4)
    store.set('code_generation',500)
    assert learning_generation.display(store,policy_generation=500)==4
