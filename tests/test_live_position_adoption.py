"""Adopt live Capital.com positions even when payload shape is awkward."""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_integration import BrokerSimulation


def test_fresh_session_adopts_open_position_despite_stale_robot_marker():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            store.set('robot_open:gold-deal',True)
            store.set('latest_robot_deal_id','gold-deal')
            store.set('mandate',{'epic':'OLD','size':'9','initial_deal_id':'old'})
            broker=BrokerSimulation()
            broker.open=[{'position':{'dealId':'gold-deal','size':0.04,'direction':'SELL',
                'level':4017.14,'upl':-0.05},'market':{'epic':'GOLD','instrumentName':'Gold Spot'}}]
            eng=SingleTradeEngine(store,brain)
            eng.policy=AutoEvolution(store,Path(d)/'policy.py')
            eng.attach(broker)
            assert eng.mandate is None
            assert store.get('mandate') is None
            await eng.sync()
            assert eng.mandate['epic']=='GOLD'
            assert eng.mandate['size']=='0.04'
            assert eng.mandate['initial_deal_id']=='gold-deal'
            assert eng.last_closed_step==-1
    asyncio.run(scenario())


def test_missing_epic_resolved_from_instrument_name_search():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            class GoldBroker(BrokerSimulation):
                async def search_markets(self,term):
                    assert 'Gold' in term
                    return {'markets':[{'epic':'CS.D.USCGC.TODAY.IP','instrumentName':'Gold Spot'}]}
            broker=GoldBroker()
            broker.open=[{'position':{'dealId':'m-gold','size':'0.04','direction':'SELL',
                'level':4017.14,'upl':-0.04},
                'market':{'instrumentName':'Gold Spot'}}]  # no epic field
            eng=SingleTradeEngine(store,brain)
            eng.policy=AutoEvolution(store,Path(d)/'policy.py')
            eng.attach(broker)
            await eng.sync()
            assert eng.mandate['epic']=='CS.D.USCGC.TODAY.IP'
            assert eng.mandate['size']=='0.04'
            assert eng.positions[0]['epic']=='CS.D.USCGC.TODAY.IP'
    asyncio.run(scenario())


def test_direction_aliases_and_nested_payload_still_adopt():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            broker=BrokerSimulation()
            broker.open=[{'dealId':'flat1','size':'.2','direction':'short','epic':'SILVER',
                          'level':30,'upl':0}]  # flat Capital-style without nested wrappers
            eng=SingleTradeEngine(store,brain)
            eng.attach(broker)
            await eng.sync()
            assert eng.mandate['epic']=='SILVER'
            assert eng.mandate['size']=='.2'
            assert eng.positions[0]['direction']=='SELL'
    asyncio.run(scenario())
