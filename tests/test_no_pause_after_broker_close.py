"""Broker SL/TP close must not leave Execution paused after a trail/protect race."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_release_after_flat_clears_protect_and_rearms():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=False;e.execution_enabled=False;e.operator_paused=False
        e.positions=[]
        oid=store.order('PROTECT','d9',{'stop':100.,'profit':None,'tick':.1})
        store.order_update(oid,'UNCERTAIN','trail-ref')
        n=e._release_execution_after_flat('d9')
        assert n==1
        assert not store.unresolved()
        assert e.armed and e.execution_enabled


def test_release_after_flat_clears_stale_open_and_rearms():
    """Leftover OPEN UNCERTAIN used to keep Execution paused after every close."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=False;e.execution_enabled=False;e.operator_paused=False
        e._unresolved_at_login=True
        e._paused_for_read=True
        e.positions=[]
        oid=store.order('OPEN',None,{'epic':'GOLD','direction':'BUY','size':'0.02'})
        store.order_update(oid,'UNCERTAIN','stale-open')
        n=e._release_execution_after_flat('closed1')
        assert n==1
        assert not store.unresolved()
        assert e.armed and e.execution_enabled
        assert e._unresolved_at_login is False
        assert e._paused_for_read is False
        reason=e.snapshot().get('entry_block_reason') or ''
        assert reason!='Execution paused'


def test_release_respects_operator_pause():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=False;e.execution_enabled=False;e.operator_paused=True
        e.positions=[]
        e._release_execution_after_flat('d9')
        assert e.operator_paused is True
        assert e.armed is False


def test_vanished_deal_rearms_after_protect_uncertain():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'race1','size':'.02','direction':'BUY',
                'level':100,'upl':0.01,'currency':'GBP'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            assert e.last_position_id=='race1'
            # Trail race left UNCERTAIN + paused while Capital SL already filled.
            oid=store.order('PROTECT','race1',{'stop':99.5,'profit':None,'tick':.1})
            store.order_update(oid,'UNCERTAIN','race-trail')
            e.armed=False;e.execution_enabled=False
            store.set('entry_brain:race1',{
                'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
                'transition':brain._json_transition(brain.core.last_transition)})
            e.market={'epic':'GOLD','mid':99.4,'tick':.01,'spread':.02}
            broker.open=[]
            broker.tx=[{'reference':'race1','pnl':-0.08,'currency':'GBP'}]
            await e.sync()
            assert e.positions==[]
            assert not store.unresolved()
            assert e.armed and e.execution_enabled
            reason=e.snapshot()['entry_block_reason'] or ''
            assert reason!='Execution paused'
            assert reason!='Unresolved broker operation'
    asyncio.run(scenario())


def test_trail_exception_when_still_open_does_not_pause():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'live1','size':'.02','direction':'BUY',
                'level':100,'upl':0.01,'currency':'GBP','stopLevel':99.0},
                'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.armed=True;e.execution_enabled=True
            e.mandate={'epic':'GOLD','size':'0.02'}
            e.positions=[{'dealId':'live1','epic':'GOLD','direction':'BUY','level':100.,
                          'stopLevel':99.0,'profitLevel':None,'size':'0.02'}]
            e.protection_brief={}
            async def boom(*a,**k):
                raise RuntimeError('broker blip')
            broker.update_position=boom
            oid=store.order('PROTECT','live1',{'stop':99.2,'profit':None,'tick':0.1})
            # Simulate adapt_protection except-path: still open → must NOT pause.
            try:
                await broker.update_position('live1',stop_level=99.2,profit_level=None)
            except Exception as exc:
                if not await e._position_still_open('live1'):
                    store.order_update(oid,'SUPERSEDED')
                    e._release_execution_after_flat('live1')
                else:
                    store.order_update(oid,'SUPERSEDED')
                    e.store.event('warning','Trail uncertain without pause',
                                  {'type':type(exc).__name__})
            assert e.armed and e.execution_enabled
            assert not store.unresolved()
    asyncio.run(scenario())


def test_trail_exception_when_flat_does_not_pause():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            e.attach(broker)
            e.armed=True;e.execution_enabled=True
            e.mandate={'epic':'GOLD','size':'0.02'}
            p={'dealId':'flat1','epic':'GOLD','direction':'BUY','level':100.,
               'stopLevel':99.0,'profitLevel':None}
            store.set('robot_open:flat1',True)
            store.set('adaptive_protection:flat1',{
                'owner':'AUTO','deal_id':'flat1','epic':'GOLD',
                'original_risk':1.0,'opened':'2026-10-30T08:00:00+00:00'})
            e.market={'status':'TRADEABLE','tick':0.1,'min_distance':0.15,
                      'bid':100.2,'ask':100.3,'spread':0.1,'time':'2026-10-30T08:01:00+00:00'}
            e.market_context={'ready':True,'bar_time':'2026-10-30T08:00:00+00:00'}
            async def boom(*a,**k):
                raise RuntimeError('position gone')
            broker.update_position=boom
            broker.open=[]  # already flat
            assert await e._position_still_open('flat1') is False
            oid=store.order('PROTECT','flat1',{'stop':99.2,'profit':None,'tick':0.1})
            try:
                await broker.update_position('flat1',stop_level=99.2,profit_level=None)
            except Exception as exc:
                if not await e._position_still_open('flat1'):
                    store.order_update(oid,'SUPERSEDED')
                    e._release_execution_after_flat('flat1')
                else:
                    raise AssertionError(exc)
            assert e.armed and e.execution_enabled
            assert not store.unresolved()
    asyncio.run(scenario())


def test_protect_missing_min_distance_does_not_disable_execution():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            e.attach(broker)
            e.armed=True;e.execution_enabled=True
            e.mandate={'epic':'GOLD','size':'0.02'}
            e.market={'tick':0.1,'mid':100.,'spread':0.1}  # no min_distance
            await e.protect_position({
                'dealId':'p1','epic':'GOLD','direction':'BUY','level':100.,
                'stopLevel':None,'profitLevel':None})
            assert e.armed and e.execution_enabled
    asyncio.run(scenario())
