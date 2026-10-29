"""Audit #1–2: UPL is not realized P&L; each deal trains at most once."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_resolve_does_not_promote_upl_to_broker_cash():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            e.attach(HistoryBroker())
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.set('last_upl:d2',{'upl':0.45,'currency':'GBP'})
            pnl,src,ccy=await e._resolve_broker_pnl('d2',{})
            assert pnl is None
            assert src=='pending_broker_pnl'
    asyncio.run(scenario())


def test_late_broker_pnl_refines_without_relearn():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            e.market={'epic':'GOLD','mid':100.,'tick':.01,'spread':.02}
            broker=HistoryBroker()
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'once1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
            store.set('entry_brain:once1',{'epic':'GOLD','direction':'BUY','entry':100,
                'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
            broker.tx=[{'reference':'once1','pnl':'-0.10','currency':'GBP','transactionType':'TRADE'}]
            pnl,src,ccy=await e._resolve_broker_pnl('once1',{'level':99.8})
            assert src=='broker' and pnl==pytest.approx(-0.10)
            e._learn_closed_trade('once1',{'level':99.8,'profit':pnl,'pnl_currency':ccy})
            assert e._trade_already_learned('once1')
            labeled_after=brain.labeled
            # Late refine with different cash must not train again.
            e._learn_closed_trade('once1',{'level':99.8,'profit':-0.50,'pnl_currency':'GBP'})
            assert brain.labeled==labeled_after
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.50)
            assert row['pnl_source']=='broker'
    asyncio.run(scenario())


def test_vanish_queues_pending_when_history_empty():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'gold1','size':'.02','direction':'SELL',
                'level':2651,'upl':-0.02,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            e.market={'epic':'GOLD','mid':2650.2,'tick':.1,'spread':.3}
            store.set('entry_brain:gold1',{'epic':'GOLD','direction':'SELL','entry':2651,
                'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
            store.upsert_trade({'dealId':'gold1','epic':'GOLD','size':'.02','direction':'SELL','level':2651})
            broker.open=[]
            await e.sync()
            row=store.trades(1)[0]
            assert row.get('pnl_source')=='pending_broker_pnl' or row.get('pnl') is None
            pending=store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id') if isinstance(x,dict) else x)=='gold1' for x in pending)
            assert not e._trade_already_learned('gold1')
    asyncio.run(scenario())
