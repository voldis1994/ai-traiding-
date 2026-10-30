"""Admin Resume must not stay frozen on stale PENDING/UNCERTAIN broker rows."""
import asyncio
import tempfile
from pathlib import Path

from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_clear_protect_uncertain_on_resume_when_deal_still_live():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=False;e.execution_enabled=False;e.operator_paused=True
        e.positions=[{'dealId':'d1','epic':'GOLD','direction':'SELL','size':'0.02',
                      'stopLevel':2640.0,'profitLevel':None}]
        oid=store.order('PROTECT','d1',{'stop':2640.0,'profit':None,'tick':0.1})
        store.order_update(oid,'UNCERTAIN','prot-ref')
        assert store.unresolved()
        cleared=e.clear_unresolved_on_operator_resume()
        assert cleared and cleared[0]['kind']=='PROTECT'
        assert not store.unresolved()
        assert e._unresolved_at_login is False


def test_clear_open_uncertain_when_flat():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.positions=[]
        oid=store.order('OPEN',None,{'epic':'GOLD','direction':'BUY','size':'0.02'})
        store.order_update(oid,'UNCERTAIN',None)
        cleared=e.clear_unresolved_on_operator_resume()
        assert len(cleared)==1
        assert not store.unresolved()


def test_admin_resume_path_clears_then_rearms():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            e.attach(broker)
            e.mandate={'epic':'GOLD','size':'0.02'}
            e.operator_paused=True
            e.armed=False;e.execution_enabled=False
            # Stale protect after close — Capital flat, journal still UNCERTAIN.
            oid=store.order('PROTECT','gone',{'stop':100.0,'profit':101.0,'tick':0.1})
            store.order_update(oid,'UNCERTAIN','old-prot')
            await e.reconcile_outstanding()
            await e.sync()
            # reconcile may already SUPERSEDE protect-for-gone-deal; clear is fallback.
            e.clear_unresolved_on_operator_resume()
            assert not store.unresolved()
            e.armed=True;e.execution_enabled=True;e.operator_paused=False
            reason=e.snapshot()['entry_block_reason'] or ''
            assert reason!='Execution paused'
            assert reason!='Unresolved broker operation'
    asyncio.run(scenario())
