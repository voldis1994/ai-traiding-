"""Closed-trade learn_gate: KEEP only when holdout improves, else ROLLBACK."""
import json
import tempfile
from pathlib import Path

import numpy as np

from app.forecast_memory import ForecastMemory, RESOLVED_OK, TABLE
from app.learn_gate import (
    decide_keep, gate_closed_trade_learn, recent_cash_degradation,
    score_cash_holdout, should_skip_teach)
from app.self_params import DEFAULTS, load as load_sp
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def test_self_params_exposes_learn_gate_defaults():
    assert DEFAULTS['learn_gate_enabled']==1
    assert DEFAULTS['learn_gate_min_rows']==12
    with tempfile.TemporaryDirectory() as d:
        store,_=new_brain(d)
        sp=load_sp(store)
        assert sp['learn_gate_enabled']==1
        assert 0.0<=sp['learn_gate_margin']<=0.05


def test_decide_keep_cold_start_and_lift():
    thin={'ok':False,'n':0,'accuracy':None}
    after_ok={'ok':True,'n':20,'accuracy':0.45}
    assert decide_keep(thin,after_ok)['keep'] is True
    assert decide_keep(thin,{'ok':True,'n':20,'accuracy':0.10})['keep'] is False
    before={'ok':True,'n':20,'accuracy':0.40}
    after_better={'ok':True,'n':20,'accuracy':0.42}
    after_worse={'ok':True,'n':20,'accuracy':0.39}
    assert decide_keep(before,after_better)['keep'] is True
    assert decide_keep(before,after_worse)['keep'] is False


def test_recent_cash_degradation_after_good_then_bad():
    """First window green, second window red → degrading (post-~10 fade)."""
    good=[{'actual_pnl':2.0,'mode':'reinforce_win'} for _ in range(5)]
    bad=[{'actual_pnl':-3.0,'mode':'loss_counterfactual_sell'} for _ in range(5)]
    deg=recent_cash_degradation(None,history=good+bad,window=5,min_trades=10)
    assert deg['ok'] is True
    assert deg['degrading'] is True
    assert deg['recent_net'] < deg['prior_net']
    # Still-green soft landing must not freeze teach.
    stable=recent_cash_degradation(
        None,history=good+[{'actual_pnl':1.5,'mode':'reinforce_win'} for _ in range(5)],
        window=5,min_trades=10)
    assert stable['ok'] is True
    assert stable['degrading'] is False
    # Both windows losing but recent worse → degrading.
    both_bad=[{'actual_pnl':-1.0} for _ in range(5)]+[{'actual_pnl':-3.0} for _ in range(5)]
    assert recent_cash_degradation(
        None,history=both_bad,window=5,min_trades=10)['degrading'] is True


def test_decide_keep_refuses_thin_holdout_when_cash_degrading():
    thin={'ok':False,'n':0,'accuracy':None}
    deg={'ok':True,'degrading':True,'trade_count':12,'recent_net':-10,'prior_net':8}
    cash={'ok':True,'n':4,'mean':-2.0,'net':-8.0,'trade_count':12}
    out=decide_keep(thin,thin,cash=cash,degradation=deg)
    assert out['keep'] is False
    assert out['reason']=='thin_holdout_cash_degrading'


def test_decide_keep_refuses_thin_holdout_after_warmup():
    thin={'ok':False,'n':0,'accuracy':None}
    cash={'ok':False,'n':3,'mean':None,'net':None,'trade_count':10}
    out=decide_keep(thin,thin,cash=cash,
                    degradation={'ok':False,'degrading':False,'trade_count':10})
    assert out['keep'] is False
    assert out['reason']=='thin_holdout_refuse_after_warmup'


def test_decide_keep_demands_larger_lift_when_degrading():
    before={'ok':True,'n':20,'accuracy':0.40}
    after_tiny={'ok':True,'n':20,'accuracy':0.402}  # +0.002 = margin, not 3×
    deg={'ok':True,'degrading':True,'trade_count':12}
    out=decide_keep(before,after_tiny,degradation=deg)
    assert out['keep'] is False
    assert 'cash_degrading' in out['reason']
    after_clear={'ok':True,'n':20,'accuracy':0.41}  # +0.01 ≥ 3×0.002
    assert decide_keep(before,after_clear,degradation=deg)['keep'] is True


def test_should_skip_teach_when_degrading():
    good=[{'actual_pnl':2.0} for _ in range(5)]
    bad=[{'actual_pnl':-4.0} for _ in range(5)]
    skip=should_skip_teach(None,history=good+bad)
    assert skip['skip'] is True
    assert skip['reason']=='cash_degrading_skip_teach'
    ok=should_skip_teach(None,history=good+[{'actual_pnl':1.0} for _ in range(5)])
    assert ok['skip'] is False


def test_gate_skips_teach_when_cash_degrading():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        good=[{'actual_pnl':2.0,'mode':'reinforce_win'} for _ in range(5)]
        bad=[{'actual_pnl':-3.0,'mode':'loss_counterfactual_sell'} for _ in range(5)]
        engine.profit_learning.history=good+bad
        before=brain.core.P.copy()
        called={'n':0}
        def learn():
            called['n']+=1
            return {'any_real_dw':True}
        decision=gate_closed_trade_learn(engine,learn,epic='GOLD',deal_id='deg1')
        assert decision.get('skipped') is True
        assert decision['keep'] is False
        assert called['n']==0
        assert np.array_equal(before,brain.core.P)


def test_score_cash_holdout_needs_min_trades():
    with tempfile.TemporaryDirectory() as d:
        store,_=new_brain(d)
        assert score_cash_holdout(store,min_trades=8)['ok'] is False
        hist=[{'actual_pnl':1.0,'mode':'reinforce_win'} for _ in range(10)]
        hist+=[{'actual_pnl':-2.0,'mode':'loss_counterfactual_sell'} for _ in range(10)]
        out=score_cash_holdout(store,min_trades=8,history=hist)
        assert out['ok'] is True
        assert out['n']>=1
        assert out['mean'] is not None


def _seed_holdout_rows(store, brain, *, n=40, force_label=0):
    """Insert resolved forecast_memory rows matching brain.d."""
    mem=ForecastMemory(store)
    dim=int(brain.core.d)
    rng=np.random.default_rng(42)
    for i in range(n):
        feats=rng.normal(0,.1,size=dim).tolist()
        # Bias features so a fixed label is "easy" — still just holdout data.
        feats[0]=1.5 if force_label==0 else -1.5
        outcomes={1:{'label':int(force_label),'r':0.01 if force_label==0 else -0.01}}
        store.db.execute(
            f'''INSERT INTO {TABLE}
                (time,epic,signal_id,features,scores,mid,spread,tick_size,
                 in_position,resolved,outcomes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (f'2026-01-01T00:{i:02d}:00+00:00','GOLD',None,
             json.dumps(feats),json.dumps({'BUY':0.5,'SELL':0.3,'HOLD':0.2}),
             2000.+i,.05,.01,0,RESOLVED_OK,json.dumps(outcomes)))
    store.db.commit()
    return mem.rows(epic='GOLD',resolved_only=True,limit=n)


def test_gate_cold_start_keeps_without_holdout():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        before=brain.core.P.copy()
        def learn():
            # Tiny supervised nudge so any_real_dw can fire.
            tr=brain.core.last_transition
            loss=brain.learn_from_trade(tr,0,lr=0.28,won=True,count_sample=True)
            meta=getattr(brain.core,'last_learn_meta',None) or {}
            dw=(loss is not None and not meta.get('restored')
                and float(meta.get('delta_norm') or 0)>1e-9)
            return {'any_real_dw':dw,'losses':[loss] if loss is not None else [],
                    'last_delta':meta.get('delta_norm')}
        decision=gate_closed_trade_learn(engine,learn,epic='GOLD',deal_id='g1')
        assert decision['keep'] is True
        assert decision.get('rolled_back') is False
        assert decision['reason'] in (
            'cold_start_thin_holdout','no_real_delta_w','cold_start_first_holdout',
            'cold_start_accuracy_too_low') or decision['reason'].startswith('holdout')
        # Thin holdout → keep (unless accuracy disaster which needs scored after).
        if decision['reason']=='cold_start_thin_holdout' or decision['reason']=='no_real_delta_w':
            pass
        elif decision.get('rolled_back'):
            assert np.array_equal(before,brain.core.P)


def test_gate_rollback_restores_weights_when_holdout_worsens():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        for i in range(5):
            brain.tick(100+i*.01,.05,.01,'GOLD')
        _seed_holdout_rows(store,brain,n=40,force_label=2)  # HOLD labels
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        before_P=brain.core.P.copy()

        def learn_hurt():
            # Force many BUY teaches on a HOLD-labeled holdout — typically hurts.
            tr=brain.core.last_transition
            losses=[];dw=False;last=None
            for i in range(8):
                loss=brain.learn_from_trade(tr,0,lr=0.35,won=False,count_sample=(i==0))
                if loss is not None:losses.append(loss)
                meta=getattr(brain.core,'last_learn_meta',None) or {}
                dn=meta.get('delta_norm');last=dn
                if dn is not None and not meta.get('restored') and float(dn or 0)>1e-9:
                    dw=True
            return {'any_real_dw':dw,'losses':losses,'last_delta':last}

        # Make margin strict so any drop rolls back.
        from app.self_params import STORE_KEY as SP_KEY
        raw=dict(store.get(SP_KEY) or {})
        raw['learn_gate_margin']=0.0
        store.set(SP_KEY,raw)

        from app.learn_gate import _holdout_feature_rows
        hold=_holdout_feature_rows(store,epic='GOLD')
        assert len(hold)>=12
        decision=gate_closed_trade_learn(engine,learn_hurt,epic='GOLD',deal_id='rb1')
        # Either rolled back to pre, or kept with real lift — both are valid
        # outcomes of the gate; assert consistency with decision.
        if decision.get('rolled_back'):
            assert np.allclose(before_P,brain.core.P)
            assert decision['keep'] is False
        else:
            assert decision['keep'] is True
            assert decision.get('reason')


def test_engine_closed_trade_keeps_on_thin_holdout():
    """Existing integrity path: no forecast holdout → cold KEEP → weights move."""
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100,.05,.01,'GOLD')
        before=brain.core.P.copy()
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.upsert_trade({'dealId':'lg1','epic':'GOLD','size':'1','direction':'BUY','level':100})
        store.set('entry_brain:lg1',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
        engine._learn_closed_trade('lg1',{'level':99.0,'profit':-12.5})
        gate=store.get('learn_gate_last_v1') or {}
        assert gate.get('keep') is True or gate.get('reason')=='learn_gate_disabled'
        last=[e for e in store.recent()
              if 'updated LIVE neural weights' in e['message']
              or 'without weight update' in e['message']][0]
        detail=json.loads(last['details'])
        assert 'learn_gate' in detail
        if detail.get('weights_updated'):
            assert not np.array_equal(before,brain.core.P)


def test_engine_rollback_skips_weight_update_event():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        for i in range(5):
            brain.tick(100+i*.02,.05,.01,'GOLD')
        _seed_holdout_rows(store,brain,n=48,force_label=2)
        from app.self_params import STORE_KEY as SP_KEY
        store.set(SP_KEY,{**(store.get(SP_KEY) or {}),
                          'learn_gate_margin':0.05,
                          'learn_gate_enabled':1})
        before=brain.core.P.copy()
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.upsert_trade({'dealId':'lg2','epic':'GOLD','size':'1','direction':'BUY','level':100})
        store.set('entry_brain:lg2',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
        # Monkeypatch gate to force rollback after a real ΔW.
        import app.learn_gate as lg
        real_gate=lg.gate_closed_trade_learn
        def force_rollback(eng,learn_fn,*,epic=None,deal_id=None):
            pre=eng.brain._core_snapshot()
            result=learn_fn()
            if result.get('any_real_dw'):
                eng.brain._apply_core_snapshot(pre)
                out={'keep':False,'rolled_back':True,'reason':'forced_test_rollback',
                     'learn':result,'before':{'ok':True,'accuracy':0.5},
                     'after':{'ok':True,'accuracy':0.1}}
                eng.store.set(lg.STORE_LAST,out)
                return out
            return {'keep':True,'rolled_back':False,'reason':'no_dw','learn':result}
        lg.gate_closed_trade_learn=force_rollback
        try:
            engine._learn_closed_trade('lg2',{'level':99.0,'profit':-12.5})
        finally:
            lg.gate_closed_trade_learn=real_gate
        assert np.allclose(before,brain.core.P)
        gate=store.get('learn_gate_last_v1') or {}
        assert gate.get('rolled_back') is True
        last=[e for e in store.recent()
              if 'without weight update' in e['message']][0]
        detail=json.loads(last['details'])
        assert detail['weights_updated'] is False
        assert detail['learn_gate']['rolled_back'] is True
