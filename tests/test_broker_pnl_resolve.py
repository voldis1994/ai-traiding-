"""Closed trades must store Capital cash P&L — ticks_only is a last resort bug path."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


class HistoryBroker:
    def __init__(self):
        self.open=[]
        self.tx=[]
        self.acts=[]
    async def positions(self):return list(self.open)
    async def accounts(self):
        return {'accounts':[{'preferred':True,'currency':'GBP','accountId':'1',
                             'balance':{'balance':1000}}]}
    async def transactions(self,*,last_period=600,from_time=None,to_time=None,tx_type='TRADE'):
        return {'transactions':list(self.tx)}
    async def activity(self,*,last_period=600,from_time=None,to_time=None,detailed=True,
                       deal_id=None):
        acts=list(self.acts)
        if deal_id:
            acts=[a for a in acts if str(a.get('dealId') or '')==str(deal_id)]
        return {'activities':acts}


def test_resolve_uses_activity_profit_not_ticks_only():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            broker.acts=[{'dealId':'d1','details':{'profit':-0.21},'currency':'GBP'}]
            e.attach(broker)
            # attach() clears account — restore like live accounts() refresh.
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            pnl,src,ccy=await e._resolve_broker_pnl('d1',{})
            assert pnl==pytest.approx(-0.21)
            assert src=='broker' and ccy=='GBP'
    asyncio.run(scenario())


def test_resolve_does_not_use_last_upl_as_realized():
    """Audit #1: last UPL snapshot must not become confirmed realized cash."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            e.attach(HistoryBroker())
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.set('last_upl:d2',{'upl':0.45,'currency':'GBP'})
            pnl,src,_ccy=await e._resolve_broker_pnl('d2',{})
            assert pnl is None and src=='pending_broker_pnl'
    asyncio.run(scenario())


def test_sl_tp_vanish_writes_broker_cash_not_ticks_only():
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
            # Adopt clears market{}; restore quote context like a live poll would.
            e.market={'epic':'GOLD','mid':2650.2,'tick':.1,'spread':.3}
            assert store.get('last_upl:gold1')['upl']==pytest.approx(-0.02)
            assert store.get('last_upl:gold1')['currency']=='GBP'
            store.set('entry_brain:gold1',{'epic':'GOLD','direction':'SELL','entry':2651,
                'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
            store.upsert_trade({'dealId':'gold1','epic':'GOLD','size':'.02','direction':'SELL','level':2651})
            # Position hit TP/SL at broker — disappears without our CLOSE confirm.
            broker.open=[]
            broker.acts=[{'dealId':'gold1','details':{'profit':0.0},'currency':'USD'}]
            await e.sync()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(0.0)
            assert row['pnl_source']=='broker'
            assert row['pnl_currency']=='GBP'
    asyncio.run(scenario())


def test_confirm_close_fills_missing_profit_from_transactions():
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
            store.upsert_trade({'dealId':'c1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
            store.set('entry_brain:c1',{'epic':'GOLD','direction':'BUY','entry':100,
                'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
            broker.tx=[{'reference':'c1','pnl':'-0.06','currency':'USD','transactionType':'TRADE'}]
            pnl,src,ccy=await e._resolve_broker_pnl('c1',{'level':99.8,'dealStatus':'ACCEPTED'})
            assert pnl==pytest.approx(-0.06) and src=='broker' and ccy=='GBP'
            e._learn_closed_trade('c1',{'level':99.8,'profit':pnl,'pnl_currency':ccy})
            row=store.trades(1)[0]
            assert row['pnl_source']=='broker'
            assert row['pnl']==pytest.approx(-0.06)
            assert row['pnl_currency']=='GBP'
    asyncio.run(scenario())
