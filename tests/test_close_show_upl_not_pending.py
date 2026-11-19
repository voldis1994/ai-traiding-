"""Close at £0.01 must journal 0.01 (UPL) — never blank gaida/pending."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from app.runtime_hub import RuntimeHub
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_snapshot_parses_pound_string_upl():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        e._snapshot_open_upl({
            'dealId':'s1','upl':'£0.01','currency':'USD','level':2651.0})
        snap=store.get('last_upl:s1')
        assert snap is not None
        assert snap['upl']==pytest.approx(0.01)
        assert snap['currency']=='GBP'


def test_vanish_with_upl_journals_zero_one_not_pending():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'g01','size':'.02','direction':'SELL',
                'level':2651,'upl':0.01,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            e.market={'epic':'GOLD','mid':2650.2,'tick':.1,'spread':.3}
            store.set('entry_brain:g01',{'epic':'GOLD','direction':'SELL','entry':2651,
                'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
            store.upsert_trade({'dealId':'g01','epic':'GOLD','size':'.02',
                                'direction':'SELL','level':2651})
            broker.open=[]
            await e.sync()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(0.01)
            assert row['pnl_source']=='upl_estimate'
            # Operator: UPL 0.01 teaches immediately — no pending wait.
            assert e._trade_already_learned('g01')
            hub=RuntimeHub.__new__(RuntimeHub)
            stats=RuntimeHub._trade_stats(hub,store,'GBP')
            assert stats['working_pnl']==pytest.approx(0.01)
            assert stats['pending_cash_trades']==0
    asyncio.run(scenario())


def test_pending_close_sync_stamps_upl_immediately():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'pc01','size':'.02','direction':'BUY',
                'level':100,'upl':0.01,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            e.market={'epic':'GOLD','mid':100.5,'tick':.01,'spread':.02}
            store.set('entry_brain:pc01',{'epic':'GOLD','direction':'BUY','entry':100,
                'tick_size':.01,'transition':brain._json_transition(brain.core.last_transition)})
            e.pending_close=True
            broker.open=[]
            # Isolate stamp path: clear retry queue effect by checking after vanish
            # before a second retry would run — sync still runs retry once, which
            # must leave upl_estimate with 0.01 (never blank pending_close_sync).
            await e.sync()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(0.01)
            assert row['pnl_source']=='upl_estimate'
            pending=store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id') if isinstance(x,dict) else x)=='pc01'
                       for x in pending)
    asyncio.run(scenario())


def test_teach_pending_does_not_downgrade_upl_estimate():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'u1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.close_trade('u1',100.1,0.01,currency='GBP',source='upl_estimate',overwrite=True)
        store.set('last_upl:u1',{'upl':0.01,'currency':'GBP'})
        e._teach_pending_ticks('u1',100.1)
        row=store.trades(1)[0]
        assert row['pnl']==pytest.approx(0.01)
        assert row['pnl_source']=='upl_estimate'


def test_admin_pending_with_numeric_pnl_counts_as_working():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        store.upsert_trade({'dealId':'n1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        # Source clobbered to pending but pnl still 0.01 — must show in Kopējais.
        store.close_trade('n1',100.1,0.01,currency='GBP',source='pending_broker_pnl',
                          overwrite=True)
        hub=RuntimeHub.__new__(RuntimeHub)
        stats=RuntimeHub._trade_stats(hub,store,'GBP')
        assert stats['working_pnl']==pytest.approx(0.01)
        assert stats['pending_cash_trades']==0
        assert stats['recent_trades'][0]['pnl']==pytest.approx(0.01)
