"""Regression for overnight no-entry reports (offline; never calls Capital.com)."""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_integration import BrokerSimulation
from tests.market_helpers import install_synthetic_context, priced_horizon_history


def test_directional_sampling_does_not_alias_every_eighth_minute():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'robot.sqlite3')
        p=AutoEvolution(store,Path(d)/'policy.py')
        # The old resolved[::8] sampled only HOLD at minute 0 modulo 8.
        # At minute 1 modulo 8 there is a SELL that predicted the direction.
        rows=[]
        for i in range(96):
            is_sell=i%8==1
            rows.append({'scores':{'BUY':.20,'SELL':.55,'HOLD':.25} if is_sell else
                         {'BUY':.15,'SELL':.25,'HOLD':.60},
                         'label':1 if is_sell else 2,'spread_bps':2})
        rows=priced_horizon_history(rows)
        threshold=p.adaptive_threshold(.7,96,2)
        evidence=p.directional_evidence('SELL',rows,threshold)
        assert evidence['allowed'] is True
        assert evidence['samples']>=8
        assert evidence['precision']==1


def test_manual_close_restores_mandate_only_when_durably_confirmed():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation()
            e.attach(broker)
            await e.sync()  # adopt initial manual trade
            assert e.mandate and e.last_closed_step==-1
            # Restarting while the manual trade is still open cannot grant
            # autonomous-entry authorization.
            fresh=SingleTradeEngine(store,brain)
            fresh.attach(broker)
            assert fresh.mandate is None and fresh.last_closed_step==-1
            broker.open=[]
            await e.sync()  # broker-confirmed manual close, persisted to SQLite
            assert store.trades()[0]['closed'] is not None
            fresh.attach(broker)
            assert fresh.mandate['initial_deal_id']=='m1'
            assert fresh.last_closed_step==brain.core.steps
            await fresh.sync()
            assert fresh.mandate['epic']=='SIM'
            assert fresh.snapshot()['entry_block_reason']!='Waiting for first manually opened trade to close'
    asyncio.run(scenario())


def test_no_old_mandate_restored_when_closed_row_missing():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'robot.sqlite3')
        store.set('mandate',{'epic':'GOLD','size':'.25','initial_deal_id':'not-confirmed'})
        brain=AdaptiveBrain(store,data_dir=d)
        e=SingleTradeEngine(store,brain)
        e.attach(BrokerSimulation())
        assert e.mandate is None
        assert e.last_closed_step==-1


def test_entry_diagnostics_emit_one_event_per_completed_model_step(monkeypatch):
    install_synthetic_context(monkeypatch)
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation()
            e.attach(broker)
            await e.poll()  # initial position adopted and protected
            broker.open=[]
            await e.sync()
            await e.poll()  # no labels -> blocked with specific diagnostics
            count=lambda: store.db.execute("SELECT COUNT(*) FROM events WHERE kind='entry'").fetchone()[0]
            first=count()
            assert first==1
            # Simulate unchanged model bar: event should not spam.
            e.last_entry_audit_key=(brain.core.steps,e.last_entry_reason)
            # On the next minute/bar there should be at most one additional entry event.
            await e.poll()
            assert count()==first+1
            state=e.snapshot()
            assert state['entry_signal_validated'] is False
            assert state['entry_can_submit_now'] is False
            assert 'News guard:' in state['entry_policy_reason'] or 'Neural' in state['entry_policy_reason']
    asyncio.run(scenario())


def test_live_size_refreshes_on_restart_without_reusing_historical_size():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            broker=BrokerSimulation()
            first=SingleTradeEngine(store,brain)
            first.attach(broker)
            await first.sync()
            assert first.mandate['size']==str(broker.open[0]['position']['size'])
            # Broker changes the active position size while the bot is offline.
            broker.open[0]['position']['size']=0.5
            restarted=SingleTradeEngine(store,brain)
            restarted.attach(broker)
            await restarted.sync()
            assert restarted.mandate['size']=='0.5'
            assert store.get('mandate')['size']=='0.5'
            assert not restarted._paused_for_mismatch
            assert restarted.positions[0]['size']==0.5
    asyncio.run(scenario())
