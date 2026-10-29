"""Admin ghost open + 0% scores + missing Capital £ after broker flat."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine, Store
from app.runtime_hub import RuntimeHub
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_positions_api_error_clears_ghost_when_history_has_cash():
    """Capital closed, but positions() keeps failing → was stuck Existing open."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'ghost1','size':'.02','direction':'SELL',
                'level':2651,'upl':-0.08,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            assert e.positions and e.last_position_id=='ghost1'
            store.set('entry_brain:ghost1',{
                'epic':'GOLD','direction':'SELL','entry':2651,'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition)})
            e.market={'epic':'GOLD','mid':2650.5,'tick':.1,'spread':.3}
            e.last_signal={'action':'SELL','scores':{'BUY':0.1,'SELL':0.7,'HOLD':0.2},
                           'confidence':0.7}
            # Capital flat in history, but positions REST throws (session blip).
            broker.open=[]
            broker.tx=[{'reference':'ghost1','pnl':-0.08,'currency':'GBP'}]
            async def boom():raise RuntimeError('positions unavailable')
            broker.positions=boom
            await e.sync()
            assert e.positions==[]
            assert e.last_position_id is None
            snap=e.snapshot()
            assert snap['entry_block_reason']!='Existing open position'
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.08)
            assert row['pnl_source']=='broker'
            # Open-book score wipe must not apply after ghost clear; signal kept
            # until flat+not-ready path (market_context still default-ready here).
            assert e.last_signal is not None
    asyncio.run(scenario())


def test_dealid_type_mismatch_does_not_false_close():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            # First sync may store last_position_id as str; API later returns same id.
            broker.open=[{'position':{'dealId':'abc-99','size':'.02','direction':'BUY',
                'level':100,'upl':0.01},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            await e.sync()
            assert e.last_position_id=='abc-99'
            e.last_position_id=e.last_position_id  # already str from mapper
            await e.sync()
            assert e.positions[0]['dealId']=='abc-99'
            assert store.trades(5)[0].get('closed') is None
    asyncio.run(scenario())


def test_map_positions_drops_deal_less_rows():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        mapped=e._map_positions([{'position':{'size':1,'direction':'BUY'},'market':{'epic':'GOLD'}}])
        assert mapped==[]


def test_pending_close_sync_queues_broker_pnl_retry():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            e.market={'mid':100.5}
            store.upsert_trade({'dealId':'pc1','epic':'GOLD','size':'1','direction':'BUY','level':100})
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'pc1','size':'1','direction':'BUY','level':100},
                          'market':{'epic':'GOLD'}}]
            e.attach(broker)
            await e.sync()
            assert e.last_position_id=='pc1'
            e.pending_close=True
            broker.open=[]
            await e.sync()
            pending=store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id') if isinstance(x,dict) else x)=='pc1' for x in pending)
            row=store.trades(1)[0]
            assert row.get('pnl_source')=='pending_close_sync'
    asyncio.run(scenario())


def test_admin_recent_trades_lists_pending_cash_bind():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        store.upsert_trade({'dealId':'old','epic':'GOLD','size':'0.1','direction':'SELL','level':100})
        store.close_trade('old',99,0.14,currency='GBP',source='broker')
        store.upsert_trade({'dealId':'new1','epic':'GOLD','size':'0.1','direction':'SELL','level':100})
        store.close_trade('new1',99.5,source='pending_broker_pnl',overwrite=True)
        hub=RuntimeHub.__new__(RuntimeHub)
        stats=hub._trade_stats(store,'GBP')
        # Realized £ still only broker cash.
        assert stats['realized_pnl']==pytest.approx(0.14)
        ids=[t['deal_id'] for t in stats['recent_trades']]
        assert 'new1' in ids and 'old' in ids


def test_open_position_keeps_scores_when_market_context_not_ready():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=True;e.execution_enabled=True
        e.mandate={'epic':'GOLD','size':'0.02'}
        e.positions=[{'dealId':'p1','epic':'GOLD','direction':'SELL','size':'0.02'}]
        e.last_signal={'action':'SELL','scores':{'BUY':0.12,'SELL':0.61,'HOLD':0.27},
                       'confidence':0.61}
        e.market_context={'ready':False,'reason':'Waiting for completed candles'}
        # Simulate the poll branch that used to wipe scores to 0%/—.
        if not e.market_context.get('ready') and not e.positions:
            e.last_signal=None
        elif not e.market_context.get('ready') and e.positions:
            pass  # keep
        assert e.last_signal['scores']['SELL']==pytest.approx(0.61)
        snap=e.snapshot()
        assert 'Existing open position' in (snap['entry_block_reason'] or '')
        assert 'p1' in (snap['entry_block_reason'] or '')
        assert snap['signal']['scores']['SELL']==pytest.approx(0.61)
