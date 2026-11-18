"""Pending Capital £ must harvest while a sibling position stays open."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from app.runtime_hub import RuntimeHub
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_sync_harvests_pending_while_sibling_open():
    """sync() must bind closed pending £ even when another deal is live."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'live','size':'.02','direction':'BUY',
                'level':2650,'upl':0.05,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'closed1','epic':'GOLD','size':'.02',
                                'direction':'SELL','level':2651})
            store.close_trade('closed1',2649,source='pending_broker_pnl',overwrite=True)
            store.set('entry_brain:closed1',{
                'epic':'GOLD','direction':'SELL','entry':2651,'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition)})
            store.set('pending_broker_pnl_deals',
                      [{'deal_id':'closed1','exit_level':2649,
                        'queued_at':'2026-10-29T10:00:00+00:00'}])
            broker.tx=[{'reference':'closed1','pnl':-0.12,'currency':'GBP'}]
            await e.sync()
            assert len(e.positions)==1
            assert e.positions[0]['dealId']=='live'
            row=next(t for t in store.trades(5) if t['deal_id']=='closed1')
            assert row['pnl']==pytest.approx(-0.12)
            assert row['pnl_source']=='broker'
            assert not (store.get('pending_broker_pnl_deals') or [])
            # UPL must not become realized cash for the live sibling.
            live_row=next((t for t in store.trades(5) if t['deal_id']=='live'),None)
            if live_row and live_row.get('closed'):
                assert live_row.get('pnl_source')!='upl_estimate'
    asyncio.run(scenario())


def test_orphan_sqlite_open_harvests_with_sibling_live():
    """SQLite open row for a gone deal settles while another position stays open."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'live','size':'.02','direction':'BUY',
                'level':2650,'upl':0.05,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'orphan','epic':'GOLD','size':'.02',
                                'direction':'SELL','level':2651})
            store.set('entry_brain:orphan',{
                'epic':'GOLD','direction':'SELL','entry':2651,'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition)})
            broker.tx=[{'reference':'orphan','pnl':0.09,'currency':'GBP'}]
            await e.sync()
            assert len(e.positions)==1 and e.positions[0]['dealId']=='live'
            row=next(t for t in store.trades(5) if t['deal_id']=='orphan')
            assert row.get('closed') is not None
            assert row['pnl']==pytest.approx(0.09)
            assert row['pnl_source']=='broker'
    asyncio.run(scenario())


def test_grow_to_multi_does_not_false_vanish_live_deal():
    """1→2 positions must not mark the still-open deal as pending_broker_pnl."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'keep','size':'.02','direction':'BUY',
                'level':2650,'upl':0.01,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            assert e.last_position_id=='keep'
            broker.open=[
                {'position':{'dealId':'keep','size':'.02','direction':'BUY',
                    'level':2650,'upl':0.01,'currency':'USD'},'market':{'epic':'GOLD'}},
                {'position':{'dealId':'add','size':'.02','direction':'BUY',
                    'level':2651,'upl':0.02,'currency':'USD'},'market':{'epic':'GOLD'}},
            ]
            await e.sync()
            keep=next(t for t in store.trades(5) if t['deal_id']=='keep')
            assert keep.get('closed') is None
            assert keep.get('pnl_source')!='pending_broker_pnl'
            pending=store.get('pending_broker_pnl_deals') or []
            assert not any(str(x.get('deal_id') if isinstance(x,dict) else x)=='keep'
                           for x in pending)
            assert 'keep' in e._open_deal_ids and 'add' in e._open_deal_ids
    asyncio.run(scenario())


def test_multi_to_one_records_vanished_sibling():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[
                {'position':{'dealId':'a','size':'.02','direction':'SELL',
                    'level':2651,'upl':-0.05,'currency':'USD'},'market':{'epic':'GOLD'}},
                {'position':{'dealId':'b','size':'.02','direction':'SELL',
                    'level':2652,'upl':-0.04,'currency':'USD'},'market':{'epic':'GOLD'}},
            ]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            store.set('entry_brain:a',{
                'epic':'GOLD','direction':'SELL','entry':2651,'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition)})
            broker.open=[{'position':{'dealId':'b','size':'.02','direction':'SELL',
                'level':2652,'upl':-0.04,'currency':'USD'},'market':{'epic':'GOLD'}}]
            broker.tx=[{'reference':'a','pnl':-0.11,'currency':'GBP'}]
            e.market={'epic':'GOLD','mid':2650.5,'tick':.1,'spread':.3}
            await e.sync()
            assert len(e.positions)==1 and e.positions[0]['dealId']=='b'
            row=next(t for t in store.trades(5) if t['deal_id']=='a')
            assert row.get('closed') is not None
            assert row['pnl']==pytest.approx(-0.11)
            assert row['pnl_source']=='broker'
    asyncio.run(scenario())


def test_admin_gaida_counts_clear_after_sibling_harvest():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        store.upsert_trade({'dealId':'p1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.close_trade('p1',99.5,source='pending_broker_pnl',overwrite=True)
        store.set('pending_broker_pnl_deals',[{'deal_id':'p1','exit_level':99.5}])
        hub=RuntimeHub.__new__(RuntimeHub)
        before=RuntimeHub._trade_stats(hub,store,'GBP')
        assert before['pending_broker_pnl']==1
        assert before['pending_trades']>=1
        recent=before['recent_trades'][0]
        assert recent.get('pending_cash') is True

        async def settle():
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'live','size':'.02','direction':'BUY',
                'level':100,'upl':0.01,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            broker.tx=[{'reference':'p1','pnl':0.15,'currency':'GBP'}]
            await e.sync()
        asyncio.run(settle())
        after=RuntimeHub._trade_stats(hub,store,'GBP')
        assert after['pending_broker_pnl']==0
        assert after['pending_trades']==0
        assert after['cash_trades']>=1
