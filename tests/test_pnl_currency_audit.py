"""Cash P&L must stay in account currency — never GOLD price×size as money."""
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def test_broker_profit_stores_account_currency():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100,.05,.01,'GOLD')
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        engine.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1',
                                     'balance':{'balance':1000}}]}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.upsert_trade({'dealId':'g1','epic':'GOLD','size':'0.1','direction':'BUY','level':2650})
        store.set('entry_brain:g1',{'epic':'GOLD','direction':'BUY','entry':2650,
            'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
        # Broker cash is GBP; price×size would be ~1.0 "USD-like".
        engine._learn_closed_trade('g1',{'level':2660,'profit':0.79})
        row=store.trades(1)[0]
        assert row['pnl']==pytest.approx(0.79)
        assert row['pnl_currency']=='GBP'
        assert row['pnl_source']=='broker'


def test_without_broker_profit_no_fake_dollar_cash():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        brain.tick(100,.05,.01,'GOLD')
        engine=SingleTradeEngine(store,brain)
        engine.market={'epic':'GOLD','tick':.01,'spread':.05}
        engine.policy=AutoEvolution(store,Path(directory)/'policy.py')
        store.upsert_trade({'dealId':'g2','epic':'GOLD','size':'0.1','direction':'BUY','level':2650})
        store.set('entry_brain:g2',{'epic':'GOLD','direction':'BUY','entry':2650,
            'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
        engine._learn_closed_trade('g2',{'level':2660})
        row=store.trades(1)[0]
        assert row['pnl'] is None
        assert row['pnl_source']=='ticks_only'


def test_pending_close_sync_does_not_clear_entry_brain():
    with tempfile.TemporaryDirectory() as directory:
        store,brain=new_brain(directory)
        engine=SingleTradeEngine(store,brain)
        engine.pending_close=True
        engine.last_position_id='dealX'
        engine.market={'mid':100.5}
        store.upsert_trade({'dealId':'dealX','epic':'GOLD','size':'1','direction':'BUY','level':100})
        store.set('entry_brain:dealX',{'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
            'transition':{'h':[0],'c':[0]}})
        # Simulate sync vanishing while CLOSE confirm is in flight.
        import asyncio
        class FakeBroker:
            async def positions(self):return []
            async def accounts(self):return {'accounts':[]}
        engine.attach(FakeBroker())
        asyncio.run(engine.sync())
        assert store.get('entry_brain:dealX') is not None
        assert store.trades(1)[0]['pnl'] is None
