"""Critical findings #1–#5 + pre-test checks: operator limits, restart learn,
SL/TP confirm under broker faults."""
import asyncio
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app import hard_risk
from app.autonomy import AutoEvolution
from app.core import NeuralCore
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.self_params import DEFAULTS as SP_DEFAULTS, load as load_self_params
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def _op_limits(**kw):
    base={'max_risk_per_trade':10,'daily_loss_limit':50,
          'equity_drawdown_limit':100,'trading_halted':False}
    base.update(kw)
    return base


def test_hard_risk_no_forced_surrogate_floors():
    """Pre-test #1: PR #34 must not invent 5/25/100 over operator config."""
    for k in ('max_risk_per_trade','daily_loss_limit','equity_drawdown_limit'):
        assert hard_risk.DEFAULTS[k] is None
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        assert hard_risk.entry_allowed(store,proposed_risk=0.1)['allowed'] is False
        hard_risk.save_operator(store,_op_limits(
            max_risk_per_trade=7.5,daily_loss_limit=33,equity_drawdown_limit=150),
            reason='chosen')
        lim=hard_risk.load(store)
        assert lim['max_risk_per_trade']==pytest.approx(7.5)
        assert lim['daily_loss_limit']==pytest.approx(33)
        assert lim['equity_drawdown_limit']==pytest.approx(150)
        assert hard_risk.entry_allowed(store,proposed_risk=1.0)['allowed'] is True


def test_reconcile_open_requires_deal_id_binding(tmp_path):
    """Critical #3: OPEN reconcile must bind confirm dealId to the position."""
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD'}
    oid=store.order('OPEN',None,{'epic':'GOLD','direction':'BUY','size':'1'})
    store.order_update(oid,'PENDING','ref-open')

    class Broker:
        def __init__(self):
            self._positions=[{
                'position':{'dealId':'deal-A','epic':'GOLD','direction':'BUY','size':1,
                            'level':100.},
                'market':{'epic':'GOLD'}}]
        async def positions(self):
            return self._positions
        async def confirm(self,ref):
            return {'dealStatus':'ACCEPTED','dealId':'deal-OTHER','dealReference':ref}

    engine.broker=Broker()
    assert asyncio.run(engine.reconcile_outstanding()) is False
    assert len(store.unresolved())==1
    row=store.db.execute('select status from orders where id=?',(oid,)).fetchone()
    assert row[0]!='CONFIRMED'

    async def ok_confirm(ref):
        return {'dealStatus':'ACCEPTED','dealId':'deal-A','dealReference':ref}
    engine.broker.confirm=ok_confirm
    assert asyncio.run(engine.reconcile_outstanding()) is True
    assert store.unresolved()==[]


def test_order_fully_matched_rejects_missing_deal_id(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD'}
    engine.positions=[{'dealId':'d1','epic':'GOLD','direction':'BUY','size':1}]
    req={'epic':'GOLD','direction':'BUY','size':'1'}
    assert engine._order_fully_matched('OPEN',req,None,{},'r') is False
    assert engine._order_fully_matched(
        'OPEN',req,None,{'dealId':'d1'},'r') is True


def test_exec_friction_self_params_nonzero():
    """Critical #4: exec slip/delay seeds are non-zero."""
    assert SP_DEFAULTS['exec_slip_bps'] > 0
    assert SP_DEFAULTS['exec_fill_delay_bars'] >= 1
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        sp=load_self_params(store)
        assert sp['exec_slip_bps'] > 0
        assert sp['exec_fill_delay_bars'] >= 1


def test_learn_claim_before_save_blocks_relearn():
    """Critical #5: claim/mark before save — mid-crash cannot double-train."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.market={'epic':'GOLD','mid':100.,'tick':.01,'spread':.02}
        e.attach(HistoryBroker())
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'c5','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.set('entry_brain:c5',{
            'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
            'transition':brain._json_transition(brain.core.last_transition)})
        e._claim_trade_learned('c5')
        assert e._trade_already_learned('c5')
        labeled_before=brain.labeled
        e._learn_closed_trade('c5',{'level':99.8,'profit':-1.0,'pnl_currency':'GBP'})
        assert brain.labeled==labeled_before
        store.set('learned_trade:c5b',None)
        store.upsert_trade({'dealId':'c5b','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.set('entry_brain:c5b',{
            'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
            'transition':brain._json_transition(brain.core.last_transition)})
        e._learn_closed_trade('c5b',{'level':101.0,'profit':1.5,'pnl_currency':'GBP'})
        meta=store.get('learned_trade:c5b')
        assert meta and meta.get('learned') and meta.get('claimed')
        labeled_mid=brain.labeled
        e._learn_closed_trade('c5b',{'level':101.0,'profit':9.9,'pnl_currency':'GBP'})
        assert brain.labeled==labeled_mid


def test_trade_learn_survives_restart_no_relearn():
    """Pre-test #2: after restart, weights + learned_trade persist; deal not re-trained."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.market={'epic':'GOLD','mid':100.,'tick':.01,'spread':.02}
        e.attach(HistoryBroker())
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'rst1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.set('entry_brain:rst1',{
            'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
            'transition':brain._json_transition(brain.core.last_transition)})
        before=brain.core.snapshot_weights()
        e._learn_closed_trade('rst1',{'level':101.0,'profit':2.0,'pnl_currency':'GBP'})
        after=brain.core.snapshot_weights()
        assert NeuralCore.weight_delta_norm(before,after)>0
        assert e._trade_already_learned('rst1')
        trade_n=brain.trade_labeled
        assert trade_n>=1
        # Restart: new AdaptiveBrain + engine on same store/files.
        brain2=AdaptiveBrain(store,data_dir=d)
        e2=SingleTradeEngine(store,brain2)
        e2.policy=AutoEvolution(store,Path(d)/'policy.py')
        e2.market={'epic':'GOLD','mid':100.,'tick':.01,'spread':.02}
        e2.attach(HistoryBroker())
        e2.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        assert e2._trade_already_learned('rst1')
        assert NeuralCore.weight_delta_norm(after,brain2.core.snapshot_weights())==pytest.approx(0)
        # Re-invoke learn — must not change weights or train again.
        trade_before=brain2.trade_labeled
        e2._learn_closed_trade('rst1',{'level':101.0,'profit':99.0,'pnl_currency':'GBP'})
        assert brain2.trade_labeled==trade_before
        assert NeuralCore.weight_delta_norm(after,brain2.core.snapshot_weights())==pytest.approx(0)


def test_shadow_weights_persist_across_engine_restart():
    """Pre-test #2 companion: shadow matrices survive recreate from store."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.core.last_z=np.linspace(-0.3,0.3,brain.core.d)
        e=SingleTradeEngine(store,brain)
        shadow=e._ensure_shadow_brain()
        e._shadow_scores_now(advance=True,bar_time='t0')
        before=shadow.snapshot_weights()
        assert e._shadow_learn(None,0,lr=0.2) is not None
        after=shadow.snapshot_weights()
        assert NeuralCore.weight_delta_norm(before,after)>0
        e.shadow_brain=None
        e2=SingleTradeEngine(store,brain)
        shadow2=e2._ensure_shadow_brain()
        assert NeuralCore.weight_delta_norm(after,shadow2.snapshot_weights())==pytest.approx(0)


def test_protect_put_timeout_marks_uncertain_retains_levels():
    """Pre-test #3: broker timeout on protect PUT → UNCERTAIN, levels unchanged."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'db.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain)
            e.armed=True;e.execution_enabled=True
            e.mandate={'epic':'GOLD','size':'.25'}
            e.market={'epic':'GOLD','bid':100.,'ask':100.2,'spread':0.2,'tick':0.1,
                      'min_distance':0.3,'status':'TRADEABLE','mid':100.1}
            pos={'dealId':'p1','epic':'GOLD','direction':'BUY','level':100.,
                 'stopLevel':None,'profitLevel':None,'size':'.25'}
            e.positions=[pos]
            store.set('robot_open:p1',True)

            class Broker:
                def __init__(self):
                    self.open=[{'position':dict(pos),'market':{'epic':'GOLD'}}]
                    self.calls=0
                async def positions(self):
                    return self.open
                async def update_position(self,deal_id,stop_level=None,profit_level=None):
                    self.calls+=1
                    raise TimeoutError('broker timeout')
                async def confirm(self,ref):
                    raise AssertionError('confirm must not run after PUT timeout')

            e.broker=Broker()
            await e.protect_position(pos)
            assert e.broker.calls==1
            assert not e.execution_enabled and not e.armed
            unresolved=store.unresolved()
            assert len(unresolved)==1 and unresolved[0]['status']=='UNCERTAIN'
            # Broker book untouched — no false SL/TP applied.
            assert e.broker.open[0]['position']['stopLevel'] is None
            assert e.broker.open[0]['position']['profitLevel'] is None
    asyncio.run(scenario())


def test_protect_confirm_disconnect_not_false_confirmed():
    """Pre-test #3: confirm ConnectionError → UNCERTAIN, not CONFIRMED."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'db.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain)
            e.armed=True;e.execution_enabled=True
            e.mandate={'epic':'GOLD','size':'.25'}
            e.market={'epic':'GOLD','bid':100.,'ask':100.2,'spread':0.2,'tick':0.1,
                      'min_distance':0.3,'status':'TRADEABLE','mid':100.1}
            pos={'dealId':'p2','epic':'GOLD','direction':'BUY','level':100.,
                 'stopLevel':None,'profitLevel':None,'size':'.25'}
            e.positions=[pos]
            store.set('robot_open:p2',True)

            class Broker:
                def __init__(self):
                    self.open=[{'position':dict(pos),'market':{'epic':'GOLD'}}]
                async def positions(self):
                    return self.open
                async def update_position(self,deal_id,stop_level=None,profit_level=None):
                    # Accept PUT locally but confirm will disconnect.
                    self.open[0]['position']['stopLevel']=stop_level
                    self.open[0]['position']['profitLevel']=profit_level
                    return {'dealReference':'prot-ref'}
                async def confirm(self,ref):
                    raise ConnectionError('broker disconnect')

            e.broker=Broker()
            await e.protect_position(pos)
            unresolved=store.unresolved()
            assert len(unresolved)==1
            assert unresolved[0]['status']=='UNCERTAIN'
            assert not e.execution_enabled
            row=store.db.execute(
                "select status from orders where kind='PROTECT' order by id desc limit 1"
            ).fetchone()
            assert row[0]=='UNCERTAIN'
    asyncio.run(scenario())


def test_open_confirm_without_deal_id_not_reconciled(tmp_path):
    """Pre-test #3: OPEN ACCEPTED without dealId must not confirm."""
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD','size':'1'}
    oid=store.order('OPEN',None,{'epic':'GOLD','direction':'BUY','size':'1'})
    store.order_update(oid,'PENDING','ref-x')

    class Broker:
        async def positions(self):
            return [{'position':{'dealId':'d1','epic':'GOLD','direction':'BUY','size':1,
                                 'level':100.},'market':{'epic':'GOLD'}}]
        async def confirm(self,ref):
            return {'dealStatus':'ACCEPTED','dealReference':ref}  # no dealId

    engine.broker=Broker()
    assert asyncio.run(engine.reconcile_outstanding()) is False
    assert store.db.execute('select status from orders where id=?',(oid,)).fetchone()[0]!='CONFIRMED'


def test_stale_protect_reconcile_after_disconnect(tmp_path):
    """Pre-test #3: after disconnect, reconcile confirms only when live levels match."""
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    oid=store.order('PROTECT','deal1',{'stop':99.0,'profit':102.0,'tick':0.1})
    # No reference yet — protect-without-receipt path verifies live levels.

    class BrokerOk:
        async def positions(self):
            return [{'position':{'dealId':'deal1','epic':'GOLD','size':0.25,'direction':'BUY',
                                 'level':100.,'stopLevel':99.0,'profitLevel':102.0},
                     'market':{'epic':'GOLD'}}]

    engine.broker=BrokerOk()
    assert asyncio.run(engine.reconcile_outstanding()) is True
    assert store.db.execute('select status from orders where id=?',(oid,)).fetchone()[0]=='CONFIRMED'

    # Fresh uncertain protect with mismatched live levels stays blocked.
    oid2=store.order('PROTECT','deal1',{'stop':98.0,'profit':103.0,'tick':0.1})
    assert asyncio.run(engine.reconcile_outstanding()) is False
    assert store.db.execute('select status from orders where id=?',(oid2,)).fetchone()[0]!='CONFIRMED'
