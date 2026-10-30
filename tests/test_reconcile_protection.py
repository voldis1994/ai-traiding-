import asyncio
from app.engine import Store,SingleTradeEngine

class Brain:
    class Core: steps=0
    core=Core()

class Broker:
    def __init__(self,positions):self._positions=positions
    async def positions(self):return self._positions

def position(stop=None,profit=None):
    return {'position':{'dealId':'deal1','epic':'GOLD','size':0.25,'direction':'SELL','level':4168.7,'stopLevel':stop,'profitLevel':profit},'market':{'epic':'GOLD'}}

def test_old_protection_cleared_after_position_closes(tmp_path):
    store=Store(tmp_path/'db.sqlite3');oid=store.order('PROTECT','deal1',{'stop':4171,'profit':4165})
    engine=SingleTradeEngine(store,Brain());engine.broker=Broker([])
    assert asyncio.run(engine.reconcile_outstanding()) is True
    assert store.unresolved()==[]
    assert store.db.execute('select status from orders where id=?',(oid,)).fetchone()[0]=='SUPERSEDED'

def test_protection_verified_without_receipt(tmp_path):
    store=Store(tmp_path/'db.sqlite3');store.order('PROTECT','deal1',{'stop':4171,'profit':4165})
    engine=SingleTradeEngine(store,Brain());engine.broker=Broker([position(4171,4165)])
    assert asyncio.run(engine.reconcile_outstanding()) is True
    assert store.unresolved()==[]

def test_incomplete_protection_stays_unresolved_but_not_paused(tmp_path):
    """PROTECT not fully matched stays in journal — sync must NOT freeze execution."""
    store=Store(tmp_path/'db.sqlite3');store.order('PROTECT','deal1',{'stop':4171,'profit':4165})
    engine=SingleTradeEngine(store,Brain());engine.broker=Broker([position(4171,None)])
    assert asyncio.run(engine.reconcile_outstanding()) is False
    assert len(store.unresolved())==1
    asyncio.run(engine.sync())
    assert engine.mandate['epic']=='GOLD'
    assert engine.execution_enabled and engine.armed
    assert engine.operator_paused is False

def test_reconcile_alone_does_not_toggle_armed(tmp_path):
    """reconcile_outstanding does not re-arm by itself; sync/_ensure_live does."""
    store=Store(tmp_path/'db.sqlite3')
    engine=SingleTradeEngine(store,Brain());engine.broker=Broker([])
    engine.operator_paused=True
    engine.armed=False;engine.execution_enabled=False
    assert asyncio.run(engine.reconcile_outstanding()) is True
    assert engine.armed is False and engine.execution_enabled is False
    engine.operator_paused=False
    engine._ensure_live_unless_operator_paused()
    assert engine.armed and engine.execution_enabled
