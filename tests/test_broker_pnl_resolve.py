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


def test_resolve_ignores_upl_and_confirm_waits_for_funds():
    """No UPL / confirm.profit invent — wait for Capital Funds/history £."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            e.attach(HistoryBroker())
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.set('last_upl:d2',{'upl':0.45,'currency':'GBP'})
            pnl,src,ccy=await e._resolve_broker_pnl(
                'd2',{'profit':-0.81,'currency':'GBP'})
            assert pnl is None
            assert src=='pending_broker_pnl'
    asyncio.run(scenario())


def test_sl_tp_vanish_waits_when_capital_cash_is_zero_placeholder():
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
            # £0 activity placeholder must not settle as broker cash.
            broker.acts=[{'dealId':'gold1','details':{'profit':0.0},'currency':'USD'}]
            await e.sync()
            row=store.trades(1)[0]
            assert row['pnl'] is None
            assert row['pnl_source']=='pending_broker_pnl'
            # Real Funds £ later settles.
            broker.tx=[{'reference':'gold1','pnl':'-0.12','currency':'GBP',
                        'transactionType':'TRADE'}]
            await e._retry_pending_broker_pnl()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.12)
            assert row['pnl_source']=='broker'
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


def test_funds_transactions_beat_conflicting_activity():
    """Capital Funds TRADE pnl/cash wins over activity details.profit."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            # Activity invents a different figure; Funds is Capital phone truth.
            broker.acts=[{'dealId':'fx1','details':{'profit':-0.50},'currency':'GBP'}]
            broker.tx=[{'reference':'fx1','pnl':'-0.12','cash':'-0.12',
                        'currency':'GBP','transactionType':'TRADE'}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            pnl,src,ccy=await e._resolve_broker_pnl('fx1',{})
            assert pnl==pytest.approx(-0.12)
            assert src=='broker' and ccy=='GBP'
    asyncio.run(scenario())


def test_row_cash_pnl_prefers_funds_pnl_not_account_profit_loss():
    """TRADE-row profitLoss is phone Funds £; pnl/cash still win when both present."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        assert e._row_cash_pnl({'pnl':'-0.27','profitLoss':1.55})==pytest.approx(-0.27)
        assert e._row_cash_pnl({'cash':'£0.33'})==pytest.approx(0.33)
        # Live Capital sometimes only stamps profitLoss on the TRADE row.
        assert e._row_cash_pnl({'profitLoss':'-1.55'})==pytest.approx(-1.55)
