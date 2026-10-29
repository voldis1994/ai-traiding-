"""Force-clear stale open + bind pending Capital £ (Admin freeze)."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_sync_indent_keeps_open_age_and_force_settles():
    """Stale Capital open + history cash → clear Existing open after age≥2."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'stale1','size':'.02','direction':'SELL',
                'level':2651,'upl':-0.11,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            assert e.last_position_id=='stale1'
            store.set('entry_brain:stale1',{
                'epic':'GOLD','direction':'SELL','entry':2651,'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition)})
            e.market={'epic':'GOLD','mid':2650.5,'tick':.1,'spread':.3}
            # Same open still listed — age ticks; history already has cash close.
            broker.tx=[{'reference':'stale1','pnl':-0.11,'currency':'GBP'}]
            await e.sync()
            assert e.position_age==1
            assert e.positions  # not yet — needs age≥2
            await e.sync()
            assert e.positions==[]
            assert e.last_position_id is None
            reason=e.snapshot()['entry_block_reason'] or ''
            assert 'Existing open position' not in reason
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.11)
            assert row['pnl_source']=='broker'
    asyncio.run(scenario())


def test_lookback_prefers_opened_not_queued_at():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        store.upsert_trade({'dealId':'lb1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        # Queue time is AFTER Capital stamped the close — must not win over opened.
        look=e._history_lookback('lb1',queued_at='2099-01-01T00:00:00+00:00')
        assert look['from_time'] is not None
        assert not look['from_time'].startswith('2099')


def test_flat_sync_retries_pending_and_binds():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'pend1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
            store.close_trade('pend1',99.5,source='pending_broker_pnl',overwrite=True)
            store.set('entry_brain:pend1',{
                'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
                'transition':brain._json_transition(brain.core.last_transition)})
            store.set('pending_broker_pnl_deals',
                      [{'deal_id':'pend1','exit_level':99.5,
                        'queued_at':'2026-10-29T10:00:00+00:00'}])
            broker.tx=[{'reference':'pend1','pnl':0.22,'currency':'GBP'}]
            # Flat book path inside sync() — not only poll()'s extra retry.
            await e.sync()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(0.22)
            assert row['pnl_source']=='broker'
            assert not (store.get('pending_broker_pnl_deals') or [])
    asyncio.run(scenario())


def test_pending_upl_fallback_after_retries():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'upl1','epic':'GOLD','size':'.02','direction':'SELL','level':100})
            store.set('entry_brain:upl1',{
                'epic':'GOLD','direction':'SELL','entry':100,'tick_size':.01,
                'transition':brain._json_transition(brain.core.last_transition)})
            store.set('last_upl:upl1',{'upl':-0.19,'currency':'GBP'})
            store.set('pending_broker_pnl_deals',
                      [{'deal_id':'upl1','exit_level':99.8,'attempts':5,
                        'queued_at':'2026-10-29T10:00:00+00:00'}])
            await e._retry_pending_broker_pnl()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.19)
            assert row['pnl_source']=='broker'
            assert not (store.get('pending_broker_pnl_deals') or [])
    asyncio.run(scenario())


def test_fuzzy_single_gold_tx_binds_pending():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'uuid-no-ref','epic':'GOLD','size':'.02',
                                'direction':'SELL','level':2650})
            store.set('entry_brain:uuid-no-ref',{
                'epic':'GOLD','direction':'SELL','entry':2650,'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition)})
            # Capital short reference does not contain deal UUID.
            broker.tx=[{'reference':'ZZ99','instrumentName':'Gold Spot',
                        'pnl':0.16,'currency':'GBP'}]
            store.set('pending_broker_pnl_deals',
                      [{'deal_id':'uuid-no-ref','exit_level':2649,'attempts':1,
                        'queued_at':'2026-10-29T10:00:00+00:00'}])
            await e._retry_pending_broker_pnl()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(0.16)
            assert row['pnl_source']=='broker'
    asyncio.run(scenario())


def test_snapshot_open_reason_includes_deal_and_upl():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=True;e.execution_enabled=True
        e.mandate={'epic':'GOLD','size':'0.02'}
        e.positions=[{'dealId':'live9','epic':'GOLD','direction':'SELL',
                      'size':'0.02','upl':-0.07}]
        reason=e.snapshot()['entry_block_reason']
        assert 'Existing open position' in reason
        assert 'live9' in reason
        assert 'UPL' in reason
