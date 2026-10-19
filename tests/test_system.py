import tempfile
from pathlib import Path
from app.core import NeuralCore
from app.engine import Store,AdaptiveBrain,SingleTradeEngine
from app.autonomy import AutoEvolution

def test_core_probabilities():
    c=NeuralCore();r=c.step([1,.2,0]);assert abs(sum(r['scores'].values())-1)<1e-8

def test_store():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'test.sqlite3');s.event('test','hello');assert s.recent()[0]['message']=='hello'
        s.set('mandate',{'epic':'X'});assert s.get('mandate')['epic']=='X'

def test_no_order_without_broker():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'test.sqlite3');e=SingleTradeEngine(s,AdaptiveBrain(s));assert not e.armed

def test_evolution_parse_rejects_invalid():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'test.sqlite3');ev=AutoEvolution(s,Path(d)/'policy.py')
        try:ev._validate('def broken(')
        except SyntaxError:pass
        else:raise AssertionError('invalid code accepted')


import asyncio
from decimal import Decimal

class FakeBroker:
    def __init__(self):
        self.open=[{'position':{'dealId':'mobile1','size':'0.2','direction':'BUY','level':100},'market':{'epic':'TEST'}}]
        self.orders=[]
    async def positions(self):return list(self.open)
    async def market(self,epic):return {'snapshot':{'bid':100.0,'offer':100.02},'instrument':{'onePipMeans':0.01},'dealingRules':{'minNormalStopOrLimitDistance':{'unit':'POINTS','value':0.2}}}
    async def open_position(self,epic,direction,size,stop_distance=None,profit_distance=None):
        self.orders.append((epic,direction,size,stop_distance,profit_distance))
        self.open=[{'position':{'dealId':'bot2','size':str(size),'direction':direction,'level':100},'market':{'epic':epic}}]
        return {'dealReference':'ref1'}
    async def confirm(self,ref):return {'dealStatus':'ACCEPTED'}

def test_auto_adopt_and_order_cycle():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'state.sqlite3');e=SingleTradeEngine(store,AdaptiveBrain(store));broker=FakeBroker()
            e.attach(broker)
            assert e.armed and e.execution_enabled
            await e.sync()
            assert e.mandate['epic']=='TEST' and e.mandate['size']=='0.2'
            broker.open=[]
            await e.sync()
            assert e.last_closed_step>=0
            await e.poll()
            assert not broker.orders  # no order until fresh decision
            e.market_context={'ready':True}  # direct broker-router unit test
            await e.submit('BUY')
            assert broker.orders[0][2]==Decimal('0.2')
            assert broker.orders[0][4]>0
            assert len(e.positions)==1
    asyncio.run(scenario())

def test_neuron_prune_tensor_consistency():
    c=NeuralCore(n=12)
    c.age[:4]=300;c.utility[:4]=-0.5
    assert c.prune(min_neurons=8)==4
    assert c.n==8 and c.W.shape==(8,8) and c.R.shape==(3,8)
    c.step([1,.2,0])

def test_automatic_python_evolution():
    from app.autonomy import AutoEvolution
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        evolution=AutoEvolution(store,Path(d)/'active_policy.py')
        assert evolution.decision({'BUY':.95,'SELL':.03,'HOLD':.02})=='BUY'
        result=evolution.evolve(.40,80)
        assert result is None and evolution.generation==0
        assert evolution.decision({'BUY':.95,'SELL':.03,'HOLD':.02})=='BUY'
        assert not (Path(d)/'active_policy.previous.py').exists()

def test_automatic_exit_on_reversal():
    class ClosingBroker(FakeBroker):
        def __init__(self):super().__init__();self.closed=[]
        async def close_position(self,deal_id):
            self.closed.append(deal_id);self.open=[];return {'dealReference':'exit-ref'}
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'db.sqlite3');e=SingleTradeEngine(store,AdaptiveBrain(store));broker=ClosingBroker()
            e.attach(broker);await e.sync()
            e.position_age=5
            await e.exit_position('mobile1')
            assert broker.closed==['mobile1']
            assert not e.positions and e.last_closed_step>=0
    asyncio.run(scenario())
