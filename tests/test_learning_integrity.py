"""Regression tests for value units, delayed learning, growth and EPIC isolation."""
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.core import NeuralCore
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.autonomy import AutoEvolution


def new_brain(folder):
    store=Store(Path(folder)/'robot.sqlite3')
    return store,AdaptiveBrain(store,data_dir=folder)


def test_next_bar_moves_over_spread_yield_directional_labels():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        # HORIZON=1: each completed bar is labeled against the next bar.
        # Moves must clear the spread in a single step (no 8-minute lag).
        assert brain.HORIZON==1
        for i in range(40):
            brain.tick(100+i*1.0, .75, .01, 'GOLD')
        labels=[r[0] for r in store.db.execute('SELECT label FROM signals WHERE label IS NOT NULL')]
        assert len(labels)==40-brain.HORIZON
        assert labels==['0']*len(labels), 'Clear up-bars must label BUY, not HOLD'
        assert brain.core.last_loss is not None


def test_label_costs_are_consistent_across_price_scales():
    assert AdaptiveBrain._label(100.,100.8,.75,.70)==0
    assert AdaptiveBrain._label(100.,100.7,.75,.70)==2
    assert AdaptiveBrain._label(100.,99.2,.75,.70)==1
    assert AdaptiveBrain._label(1.,1.00008,.000075,.00007)==0
    assert AdaptiveBrain._label(10000.,10008.,7.5,7.)==0


def test_cross_epic_price_never_labels_previous_instrument():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        for i in range(3):brain.tick(4150+i*.2,.05,.1,'GOLD')
        assert brain.labeled==2  # HORIZON=1 → labels after each next bar
        steps_gold=brain.core.steps
        brain.tick(100.,.02,.01,'US500')
        assert brain.epic=='US500' and brain.labeled==0
        assert brain.core.steps==1
        assert store.db.execute('SELECT COUNT(*) FROM signals WHERE epic=? AND label IS NOT NULL',('GOLD',)).fetchone()[0]==2
        brain.tick(4151.8,.05,.1,'GOLD')
        assert brain.epic=='GOLD' and brain.core.steps==steps_gold+1
        assert brain.labeled==3
        assert store.db.execute('SELECT COUNT(*) FROM signals WHERE epic=? AND label IS NOT NULL',('US500',)).fetchone()[0]==0
        # GOLD resumed from its own last mid (~4150.4), not the US500 mid of 100.
        assert brain.last_mid==pytest.approx(4151.8)
        assert abs(brain.last_change - (4151.8-4150.4)/.1) < 1e-6


def test_restart_restores_labels_and_pending_horizon_per_epic():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        for i in range(18):brain.tick(100+i*1.0,.5,.01,'GOLD')
        assert brain.labeled==17 and len(brain.pending)==1
        prev_step=brain.core.steps
        other=AdaptiveBrain(store,data_dir=directory)
        assert other.epic=='GOLD'
        assert other.labeled==17
        assert len(other.pending)==1
        assert len(other.history)==18
        assert other.core.steps==prev_step
        other.tick(100+18*1.0,.5,.01,'GOLD')
        assert other.labeled==18 and other.core.steps==prev_step+1
        assert other.core.last_loss is not None


def test_switch_and_restart_keep_separate_weights_and_pending():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        for i in range(26):brain.tick(100+i*.2,.05,.01,'GOLD')
        gold_P=brain.core.P.copy()
        gold_steps=brain.core.steps
        for i in range(12):brain.tick(10000+i*2,1.,.1,'INDEX')
        assert brain.labeled==11  # HORIZON=1 → 12 INDEX ticks yield 11 labels
        assert brain.core.steps==12
        gold_brain=AdaptiveBrain(store,data_dir=directory)
        assert gold_brain.epic=='GOLD'
        assert gold_brain.core.steps==gold_steps
        np.testing.assert_allclose(gold_brain.core.P,gold_P)
        gold_brain.tick(10024,1.,.1,'INDEX')
        assert gold_brain.labeled==12
        gold_brain.tick(105.2,.05,.01,'GOLD')
        np.testing.assert_allclose(gold_brain.core.P,gold_P,rtol=0,atol=0.01)
        assert gold_brain.core.steps==gold_steps+1


def test_old_one_poll_labels_not_counted_as_new_horizon_evidence():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        store.db.execute("INSERT INTO signals(time,epic,action,scores,price,model_step,label) VALUES('t','GOLD','BUY','{}',100,1,'0')")
        store.db.commit()
        another=AdaptiveBrain(store,data_dir=directory)
        another.tick(100,.02,.01,'GOLD')
        assert another.labeled==0 and another.correct==0
        assert store.db.execute('SELECT label FROM signals WHERE label_version IS NULL').fetchone()[0]=='0'


def test_neural_growth_and_pruning_keep_delayed_gradient():
    core=NeuralCore(n=12)
    core.step([2,.5,-1]);transition=core.last_transition.copy()
    ids=transition['neuron_ids'].copy()
    core.vbar=100
    assert core.grow_if_needed(np.ones(3)*7,threshold=1)
    assert core.n==13
    assert core.learn(transition,0) is not None
    core.age[:4]=400;core.utility[:4]=-2
    assert core.prune(min_neurons=8,min_age=200)==4
    assert core.learn(transition,1) is not None
    assert len(set(core.neuron_ids.tolist()))==core.n
    assert core.next_neuron_id>max(core.neuron_ids)
    assert len(ids)==12


def test_growth_does_not_starve_learning_in_long_session():
    with tempfile.TemporaryDirectory() as directory:
        _,brain=new_brain(directory)
        calls=[]
        original=brain.core.learn
        def tracked(*args,**kwargs):
            result=original(*args,**kwargs)
            if result is not None:calls.append(result)
            return result
        brain.core.learn=tracked
        for i in range(80):brain.tick(100+i*.05,.03,.01,'GOLD')
        assert len(calls)==80-brain.HORIZON
        assert brain.core.births<=80//brain.FAST_GROWTH_INTERVAL


def test_broker_trade_learning_uses_entry_instrument_tick_not_current_market():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100,.05,.01,'GOLD')
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':10,'spread':100.}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.set('entry_brain:deal1',{'epic':'GOLD','direction':'SELL','entry':100,
            'tick_size':.01,'entry_spread':.05,
            'transition':brain._json_transition(brain.core.last_transition)})
        store.upsert_trade({'dealId':'deal1','epic':'GOLD','size':'0.5','direction':'SELL','level':100})
        engine._learn_closed_trade('deal1',{'level':99.8})
        last=[e for e in store.recent() if 'P&L self-evaluation' in e['message']][0]
        detail=json.loads(last['details'])
        assert detail['realized_directional_ticks']==pytest.approx(20.)
        assert detail['mode']=='reinforce_win_ticks'  # tick path — not fake £ in profit ledger
        assert detail['target']==1
        assert detail['learn_repeats']==2
        # Without broker profit, cash P&L stays blank (no fake GOLD USD price×size).
        assert store.trades(1)[0]['pnl'] is None
        assert detail['pnl_source']=='ticks_only'
        assert store.get('entry_brain:deal1') is None


def test_losing_trade_regrets_opposite_action_with_stronger_update():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100,.05,.01,'GOLD')
        before=brain.core.P.copy()
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.upsert_trade({'dealId':'loss1','epic':'GOLD','size':'1','direction':'BUY','level':100})
        store.set('entry_brain:loss1',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
        # BUY that exited lower → loss → HOLD (no automatic opposite), stronger repeats.
        engine._learn_closed_trade('loss1',{'level':99.0,'profit':-12.5})
        last=[e for e in store.recent() if 'P&L self-evaluation' in e['message']][0]
        detail=json.loads(last['details'])
        assert detail['mode'] in ('loss_hold_default','loss_abstain_hold')
        assert detail['target']==2
        assert detail['learn_repeats']==5
        assert detail['scratch_hold_skip'] is False
        assert detail['broker_pnl']==-12.5
        assert detail['pnl']==-12.5
        assert store.trades(1)[0]['pnl']==pytest.approx(-12.5)
        assert not np.array_equal(before,brain.core.P)
        assert store.get('entry_brain:loss1') is None


def test_scratch_loss_does_not_spam_hold_into_core():
    """£0 / penny closes used to train HOLD × learn_loss_repeats → permanent HOLD."""
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100,.05,.01,'GOLD')
        before=brain.core.P.copy()
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.upsert_trade({'dealId':'scratch1','epic':'GOLD','size':'1','direction':'BUY','level':100})
        store.set('entry_brain:scratch1',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
        engine._learn_closed_trade('scratch1',{'level':100.0,'profit':-0.02})
        last=[e for e in store.recent() if 'P&L self-evaluation' in e['message']][0]
        detail=json.loads(last['details'])
        assert detail['target']==2
        assert detail['learn_repeats']==0
        assert detail['scratch_hold_skip'] is True
        assert detail['mode'].endswith('_scratch_skip')
        assert np.array_equal(before,brain.core.P)
        assert store.get('entry_brain:scratch1') is None


def test_mismatched_broker_trade_outcome_never_trains():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100.,.05,.01,'GOLD')
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'INDEX','tick':10,'spread':10}
        store.set('entry_brain:deal2',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
        assert brain.core.last_loss is None
        engine._learn_closed_trade('deal2',{'level':150.})
        assert brain.core.last_loss is None
        assert any('skipped' in e['message'] for e in store.recent())


def test_adaptive_spread_penalty_is_price_scale_invariant():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        engine=SingleTradeEngine(store,brain)
        brain.correct=600;brain.labeled=1000
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        engine.market={'mid':100,'spread':.1,'tick':.001}
        low=engine._adaptive_threshold()
        engine.market={'mid':10000,'spread':10,'tick':10}
        high=engine._adaptive_threshold()
        assert low==high
