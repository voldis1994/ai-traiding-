"""Desk Bid/Ask must stay alive after login clears adoptive mandate."""
import asyncio
import tempfile
from pathlib import Path

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_integration import BrokerSimulation


def test_desk_watch_epic_from_legacy_without_mandate():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'robot.sqlite3')
        store.set('legacy_brain_epic','GOLD')
        store.set('mandate',{'epic':'GOLD','size':'0.04','initial_deal_id':'old'})
        brain=AdaptiveBrain(store,data_dir=d)
        eng=SingleTradeEngine(store,brain)
        eng.attach(BrokerSimulation())
        assert eng.mandate is None
        assert eng._desk_watch_epic()=='GOLD'


def test_observe_desk_quotes_fills_bid_ask_without_authorizing_open():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            store.set('legacy_brain_epic','GOLD')
            store.set('mandate',{'epic':'GOLD','size':'0.04','initial_deal_id':'old'})
            brain=AdaptiveBrain(store,data_dir=d)
            broker=BrokerSimulation()
            broker.open=[]  # flat — do not auto-adopt
            eng=SingleTradeEngine(store,brain)
            eng.attach(broker)
            assert eng.mandate is None
            ok=await eng._observe_desk_quotes()
            assert ok is True
            assert eng.mandate is None  # still no OPEN authorization
            assert eng.market['epic']=='GOLD'
            assert eng.market['bid'] is not None
            assert eng.market['ask'] is not None
            assert eng.market.get('observe_only') is True
            # Entry still blocked on adoptive size — not on dead quotes.
            reason=eng._compose_entry_block_reason(
                readiness={'ready':True},evaluation={},decision={})
            assert 'manual trade' in reason.lower() or 'adoptive' in reason.lower()
    asyncio.run(scenario())


def test_poll_without_mandate_still_updates_market():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            store.set('legacy_brain_epic','GOLD')
            store.set('mandate',{'epic':'GOLD','size':'0.04','initial_deal_id':'old'})
            brain=AdaptiveBrain(store,data_dir=d)
            broker=BrokerSimulation()
            broker.open=[]  # flat — poll must not adopt; observe-only quotes
            eng=SingleTradeEngine(store,brain)
            eng.attach(broker)
            assert eng.mandate is None
            await eng.poll()
            assert eng.mandate is None
            assert (eng.market or {}).get('bid') is not None
            assert (eng.market or {}).get('ask') is not None
            assert (eng.market or {}).get('observe_only') is True
    asyncio.run(scenario())
