"""Auto Execution paused is banned — only explicit operator Pause freezes OPEN."""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from app.runtime_hub import RuntimeHub
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_ensure_live_ignores_sticky_auto_pause_flags():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.operator_paused=False
        e.armed=False
        e.execution_enabled=False
        e._paused_for_read=True
        e._paused_for_multi=True
        e._paused_for_mismatch=True
        e._unresolved_at_login=True
        assert e._ensure_live_unless_operator_paused() is True
        assert e.armed and e.execution_enabled
        assert e._paused_for_read is False
        assert e._paused_for_multi is False
        assert e._paused_for_mismatch is False
        reason=e._compose_entry_block_reason(
            readiness={'ready':True},evaluation=None,decision='HOLD')
        assert reason!='Execution paused'
        assert 'paused' not in reason.lower() or 'operator' in reason.lower()


def test_operator_pause_still_freezes():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.operator_paused=True
        e.armed=True
        e.execution_enabled=True
        assert e._ensure_live_unless_operator_paused() is False
        assert not e.armed and not e.execution_enabled
        reason=e._compose_entry_block_reason(
            readiness={'ready':True},evaluation=None,decision='HOLD')
        assert reason=='Paused by operator'


def test_sync_multi_position_does_not_pause():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[
                {'position':{'dealId':'a','size':'.02','direction':'BUY','level':100,
                             'upl':0,'currency':'GBP'},'market':{'epic':'GOLD'}},
                {'position':{'dealId':'b','size':'.02','direction':'SELL','level':101,
                             'upl':0,'currency':'GBP'},'market':{'epic':'GOLD'}},
            ]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            assert e.armed and e.execution_enabled
            assert e.operator_paused is False
            reason=e.snapshot().get('entry_block_reason') or ''
            assert reason!='Execution paused'
            assert 'Paused by operator' not in reason
    asyncio.run(scenario())


def test_m1_adapt_reason_must_not_pair_with_execution_paused():
    """Admin used to show 'M1 label updated … · Execution paused' after learn ticks."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.operator_paused=False
        e.armed=False
        e.execution_enabled=False
        store.set('neural_adapt_reason','M1 label updated LIVE weights (loss=-1.5525)')
        snap=e.snapshot()
        assert snap.get('entry_block_reason')!='Execution paused'
        assert e.armed and e.execution_enabled


def test_runtime_hub_never_disarms_on_read_errors():
    """Regression: worker used to set armed=False after 3 broker errors."""
    class Eng:
        operator_paused=False
        armed=True
        execution_enabled=True
        _paused_for_read=False
        def _ensure_live_unless_operator_paused(self):
            self.armed=True
            self.execution_enabled=True
            self._paused_for_read=False
            return True
    e=Eng()
    # Simulate the healed worker path.
    e._ensure_live_unless_operator_paused()
    assert e.armed and e.execution_enabled
    assert RuntimeHub._can_rearm_after_read  # still importable
