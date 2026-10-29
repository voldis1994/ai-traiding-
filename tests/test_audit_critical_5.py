"""Critical findings #1–#5: shadow weights, mandatory risk, dealId reconcile,
exec friction parity, atomic learn-once claim."""
import asyncio
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app import hard_risk
from app.core import NeuralCore
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.self_params import DEFAULTS as SP_DEFAULTS, load as load_self_params
from tests.test_learning_integrity import new_brain
from tests.test_broker_pnl_resolve import HistoryBroker
from app.autonomy import AutoEvolution


def test_hard_risk_defaults_are_mandatory_numeric():
    """Critical #2: monetary limits never start as None."""
    for k in ('max_risk_per_trade','daily_loss_limit','equity_drawdown_limit'):
        assert hard_risk.DEFAULTS[k] is not None
        assert float(hard_risk.DEFAULTS[k]) > 0
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        limits=hard_risk.load(store)
        assert limits['max_risk_per_trade']==pytest.approx(5.0)
        # Unset cash risk must block even with fresh store (no operator save).
        gate=hard_risk.entry_allowed(store,proposed_risk=None)
        assert gate['allowed'] is False
        assert 'mandatory' in gate['reason'] or 'could not be computed' in gate['reason']
        # Operator cannot clear a limit back to None.
        hard_risk.save_operator(store,{'max_risk_per_trade':None},reason='try clear')
        assert hard_risk.load(store)['max_risk_per_trade']==pytest.approx(5.0)


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
            # ACCEPTED but dealId does NOT match the live position.
            return {'dealStatus':'ACCEPTED','dealId':'deal-OTHER','dealReference':ref}

    engine.broker=Broker()
    assert asyncio.run(engine.reconcile_outstanding()) is False
    assert len(store.unresolved())==1
    row=store.db.execute('select status from orders where id=?',(oid,)).fetchone()
    assert row[0]!='CONFIRMED'

    # Matching dealId confirms.
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
        # Simulate crash after claim, before mark/save: claimed but not learned.
        e._claim_trade_learned('c5')
        assert e._trade_already_learned('c5')
        labeled_before=brain.labeled
        e._learn_closed_trade('c5',{'level':99.8,'profit':-1.0,'pnl_currency':'GBP'})
        assert brain.labeled==labeled_before
        # Fresh path: claim+mark around save.
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
