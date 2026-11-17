"""Local integration suite: no Capital.com credentials, no live orders."""
import asyncio
import json
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
from fastapi.testclient import TestClient
from app.core import NeuralCore
from app.math_rules import stop_take,position_size,pnl
from app.broker import CapitalBroker
from app.engine import Store,AdaptiveBrain,SingleTradeEngine
from app.autonomy import AutoEvolution
from tests.market_helpers import install_synthetic_context


def async_run(coro):return asyncio.run(coro)


def test_pdf_long_short_sl_tp_and_rounding():
    # Broker min + spread/tick buffer; no invented 20-tick floor. κ=2, sl_scale=1.
    buy=stop_take('100','BUY','.01','.02','.2',2)
    sell=stop_take('100','SELL','.01','.02','.2',2)
    assert buy['stop_distance']==Decimal('.24')
    assert buy['stop_level']==Decimal('99.76')
    assert buy['profit_level']==Decimal('100.48')
    assert sell['stop_level']==Decimal('100.24')
    assert sell['profit_level']==Decimal('99.52')
    # min_distance=0 → only buffer (2*tick), not a fake 20-tick invent.
    bare=stop_take('100','BUY','.03','.01',0)  # default κ=1 (1:1), not 1:2
    assert bare['stop_ticks']==2
    assert bare['profit_ticks']==2
    assert bare['kappa']==1.0
    assert bare['sl_scale']==1.0
    wider=stop_take('100','BUY','.03','.01',0,kappa=2,sl_scale=2)
    assert wider['stop_ticks']==4
    assert wider['profit_ticks']==8


def test_pdf_position_size_risk_equation():
    result=position_size(100,10,20,2,'.1','.1',10)
    assert result['raw']==Decimal('2.25')
    assert result['size']==Decimal('2.2')
    assert result['estimated_risk']<=100
    assert position_size(1,0,100,10,'.1','.1',10)['size']==0


def test_pdf_pnl_long_short_and_costs():
    assert pnl(100,101,'BUY',2,.01,.1,1)==Decimal(19)
    assert pnl(100,99,'SELL',2,.01,.1,1)==Decimal(19)
    assert pnl(100,99,'BUY',2,.01,.1,1)==Decimal(-21)


def test_gradient_updates_recurrent_and_memory_parameters():
    core=NeuralCore(seed=23)
    output=core.step([4,2,.4],dt=7)
    old={k:getattr(core,k).copy() for k in ('P','R','O','b','G','tau','tau_c')}
    loss=core.learn(core.last_transition,0,lr=.02)
    assert loss is not None and loss>0
    assert sum(np.linalg.norm(getattr(core,k)-old[k])>0 for k in old)>=5
    assert np.all(core.tau>0) and np.all(core.tau_c>0)


def test_growth_and_recovery_learning_after_resize():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'s.db');b=AdaptiveBrain(s,data_dir=d)
        b.tick(100,.02,.01,'FAKE');b.core.vbar=20
        assert b.core.grow_if_needed(np.ones(3)*7,threshold=1)
        for i in range(5):b.tick(100+.01*i,.02,.01,'FAKE')
        b.save();other=AdaptiveBrain(s,data_dir=d)
        assert other.core.n==b.core.n
        assert other.core.steps==b.core.steps


def test_policy_generates_new_python_and_rejects_arbitrary_import():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'s.db');e=AutoEvolution(s,Path(d)/'generated.py')
        source=e.path.read_text()
        assert e.evolve(.4,80) is None  # no directional replay evidence
        assert e.path.read_text()==source
        assert e.generation==0
        try:e._validate('import os\ndef decide(scores):return os.system("whoami")')
        except ValueError:pass
        else:raise AssertionError('Unsafe code allowed')


def test_broker_http_payload_and_session_headers():
    calls=[]
    def handle(request):
        calls.append((request.method,request.url.path,request.content))
        if request.url.path=='/api/v1/session':
            return httpx.Response(200,json={'clientId':'abc'},headers={'CST':'abc','X-SECURITY-TOKEN':'def'})
        if request.url.path.endswith('/positions') and request.method=='GET':return httpx.Response(200,json={'positions':[]})
        if request.url.path=='/api/v1/positions' and request.method=='POST':
            assert request.headers['CST']=='abc'
            payload=json.loads(request.content)
            assert payload['profitDistance']==.4 and payload['stopDistance']==.2
            assert payload['size']==.2 and 'limitDistance' not in payload
            return httpx.Response(200,json={'dealReference':'ref-1'})
        if request.url.path.endswith('/positions/deal'):return httpx.Response(200,json={'dealReference':'close-ref'})
        if request.url.path.endswith('/confirms/ref-1'):return httpx.Response(200,json={'dealStatus':'ACCEPTED'})
        return httpx.Response(200,json={'accounts':[]})
    async def scenario():
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='https://broker.invalid')
        broker=CapitalBroker('secret','user','pass',client=client)
        await broker.connect();await broker.positions()
        result=await broker.open_position('EPIC','BUY',Decimal('.2'),Decimal('.2'),Decimal('.4'))
        assert result['dealReference']=='ref-1'
        await broker.close()
    async_run(scenario())
    assert any(m=='POST' and path=='/api/v1/positions' for m,path,_ in calls)


class BrokerSimulation:
    def __init__(self):
        self.open=[{'position':{'dealId':'m1','size':'.25','direction':'BUY','level':100,
                    'stopLevel':None,'profitLevel':None,'upl':0},'market':{'epic':'SIM'}}]
        self.requests=[];self.price=100.;self.fail_on_order=False
    async def positions(self):return json.loads(json.dumps(self.open))
    async def market(self,epic):return {'snapshot':{'bid':self.price,'offer':self.price+.02,'marketStatus':'TRADEABLE',
        'decimalPlacesFactor':2},
        # valueOfOnePip required so hard-risk cash_risk can be computed for OPEN.
        'instrument':{'onePipMeans':.01,'valueOfOnePip':1.0},
        'dealingRules':{'minStopOrProfitDistance':{'unit':'POINTS','value':'.15'}}}
    async def accounts(self):return {'accounts':[{'preferred':True,'accountId':'SIM','currency':'USD','balance':{'balance':1000}}]}
    async def prices(self,epic,resolution='MINUTE',maximum=80):return {'prices':[]}
    async def confirm(self,ref):return {'dealStatus':'ACCEPTED','dealId':'b2'}
    async def update_position(self,deal_id,stop_level=None,profit_level=None):
        self.requests.append(('PROTECT',deal_id,stop_level,profit_level))
        if stop_level is not None:self.open[0]['position']['stopLevel']=float(stop_level)
        if profit_level is not None:self.open[0]['position']['profitLevel']=float(profit_level)
        return {'dealReference':'protection'}
    async def open_position(self,epic,direction,size,stop_distance=None,profit_distance=None):
        self.requests.append(('OPEN',epic,direction,size,stop_distance,profit_distance))
        if self.fail_on_order:raise TimeoutError('Request outcome unknown')
        self.open=[{'position':{'dealId':'b2','size':str(size),'direction':direction,'level':self.price,
            'stopLevel':self.price-float(stop_distance),
            'profitLevel':self.price+float(profit_distance)},'market':{'epic':epic}}]
        return {'dealReference':'entry'}
    async def close_position(self,deal_id):
        self.requests.append(('CLOSE',deal_id));self.open=[]
        return {'dealReference':'exit'}


def test_full_mock_broker_protect_close_reenter_cycle():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'s.db');brain=AdaptiveBrain(store,data_dir=d)
            engine=SingleTradeEngine(store,brain);broker=BrokerSimulation()
            # Keep auto-evolved policy state from this test out of the real project.
            engine.policy=AutoEvolution(store,Path(d)/'policy.py')
            engine.attach(broker);await engine.sync()
            assert engine.mandate['size']=='.25'
            await engine.poll()
            assert broker.requests[0][0]=='PROTECT'
            assert not store.unresolved()
            assert engine.positions[0]['stopLevel'] is not None
            await engine.exit_position('m1')
            assert engine.last_closed_step>=0 and not engine.positions
            await engine.poll()
            assert len(broker.requests)==2
            engine.market_context={'ready':True}  # exercise router, not market readiness
            brain.mark_prep_ready('test fixture')
            await engine.submit('SELL')
            assert broker.requests[-1][0]=='OPEN'
            assert broker.requests[-1][3]==Decimal('.25')
            assert broker.requests[-1][4]>0 and broker.requests[-1][5]>0
            assert len(engine.positions)==1
            assert not store.unresolved()
            assert store.trades()[0]['epic']=='SIM'
    async_run(scenario())


def test_uncertain_order_does_not_retry():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'s.db');b=AdaptiveBrain(store,data_dir=d);e=SingleTradeEngine(store,b)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation();e.attach(broker)
            await e.sync();broker.open=[];await e.sync();await e.poll()
            broker.fail_on_order=True
            e.market_context={'ready':True}  # router uncertainty regression
            b.mark_prep_ready('test fixture')
            await e.submit('BUY');await e.submit('BUY')
            assert len([r for r in broker.requests if r[0]=='OPEN'])==1
            assert not e.execution_enabled and store.unresolved()
    async_run(scenario())


def test_multi_position_detected_pauses_execution():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'s.db');e=SingleTradeEngine(s,AdaptiveBrain(s,data_dir=d));b=BrokerSimulation()
            b.open.append({'position':{'dealId':'m2','size':.25,'direction':'SELL'},'market':{'epic':'SIM'}})
            e.attach(b);await e.sync()
            assert not e.armed and not e.execution_enabled
    async_run(scenario())


def test_auth_required_and_frontend_assets_exist():
    from app.main import app
    with TestClient(app) as c:
        assert c.get('/').status_code==200
        assert c.get('/static/ui.js').status_code==200
        assert c.get('/static/style.css').status_code==200
        assert c.get('/api/status').status_code==401
        assert c.post('/api/pause').status_code==401

def test_quote_polls_without_completed_history_cannot_fake_learning():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'s.db');brain=AdaptiveBrain(s,data_dir=d)
            e=SingleTradeEngine(s,brain);b=BrokerSimulation()
            e.policy=AutoEvolution(s,Path(d)/'policy.py');e.attach(b)
            for i in range(20):
                b.price=100+(.04 if i%3==0 else -.02)*(i/100+1)
                await e.poll()
            assert brain.core.steps==0 and brain.labeled==0
            assert s.db.execute('SELECT count(*) FROM signals').fetchone()[0]==0
            assert s.candles('SIM')  # graph still gets live quotes
            assert not e.market_context['ready']
    async_run(scenario())


def test_login_reconciles_prior_confirmed_request_without_retry():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'s.db');b=BrokerSimulation()
            oid=s.order('OPEN',None,{'epic':'SIM','direction':'BUY','size':'.25'})
            s.order_update(oid,'PENDING','known-ref')
            e=SingleTradeEngine(s,AdaptiveBrain(s,data_dir=d));e.policy=AutoEvolution(s,Path(d)/'policy.py')
            e.attach(b)
            assert not e.execution_enabled
            assert await e.reconcile_outstanding()
            assert not s.unresolved()
            assert e.execution_enabled
            await e.sync();assert e.mandate['epic']=='SIM'
            assert not [r for r in b.requests if r[0]=='OPEN']
    async_run(scenario())

def test_live_login_cookie_and_automatic_mobile_adoption_without_extra_toggle(monkeypatch):
    from app import main
    class LoginBroker(BrokerSimulation):
        def __init__(self,*args,**kwargs):super().__init__();self.open=[]
        async def connect(self):return {'authenticated':True}
        async def close(self):pass
    monkeypatch.setattr(main,'CapitalBroker',LoginBroker)
    with TestClient(main.app) as client:
        response=client.post('/api/connect',json={'api_key':'fake','identifier':'fake','password':'fake'})
        assert response.status_code==200
        assert response.json()['automatic'] is True
        assert 'httponly' in response.headers.get('set-cookie','').lower()
        assert client.get('/api/status').status_code==200
        assert main.engine.execution_enabled
        assert main.engine.mandate is None
        main.engine.broker.open=[{'position':{'dealId':'manual-1','size':'.25','direction':'BUY','level':100},'market':{'epic':'SIM'}}]
        client.post('/api/refresh')
        assert main.engine.mandate['initial_deal_id']=='manual-1'
        assert main.engine.execution_enabled  # no second operator switch
        assert client.post('/api/pause').json()['ok']
        assert not main.engine.execution_enabled
        assert client.post('/api/resume').json()['ok']
        assert main.engine.execution_enabled
        assert client.post('/api/logout').json()['ok']
        assert client.get('/api/status').status_code==401

def test_ai_signal_opens_second_trade_automatically_after_broker_confirmed_close(monkeypatch):
    install_synthetic_context(monkeypatch)
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(s,data_dir=d)
            e=SingleTradeEngine(s,brain);e.policy=AutoEvolution(s,Path(d)/'policy.py')
            broker=BrokerSimulation();e.attach(broker)
            await e.poll()
            assert e.positions[0]['dealId']=='m1'
            broker.open=[];await e.sync()
            assert e.last_closed_step>=0
            e._external_entry_check=lambda _: {'entry_allowed':True,'status':'TEST'}
            brain.mark_prep_ready('test fixture')
            brain.core.b=np.array([7.,-4.,-4.])
            await e.poll()  # very next completed new M1 candle; no historical-label minimum
            assert len([x for x in broker.requests if x[0]=='OPEN'])==1
            assert e.positions[0]['dealId']=='b2'
            assert Decimal(str(e.positions[0]['size']))==Decimal('.25')
            await e.poll()
            assert len([x for x in broker.requests if x[0]=='OPEN'])==1
    async_run(scenario())

def test_broker_confirmed_exit_level_drives_delayed_trade_feedback(monkeypatch):
    install_synthetic_context(monkeypatch)
    class RewardBroker(BrokerSimulation):
        async def confirm(self,ref):
            return {'dealStatus':'ACCEPTED','dealId':'m1','level':100.4 if ref=='exit' else 100.}
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'state.db');brain=AdaptiveBrain(s,data_dir=d)
            e=SingleTradeEngine(s,brain);e.policy=AutoEvolution(s,Path(d)/'policy.py')
            broker=RewardBroker();e.attach(broker)
            await e.poll()
            assert s.get('entry_brain:m1') is not None
            await e.exit_position('m1')
            assert brain.core.last_loss is not None
            assert any('P&L self-evaluation' in e['message'] for e in s.recent())
    async_run(scenario())


def test_position_update_uses_documented_profit_level_and_preserves_existing_stop():
    requests=[]
    def handler(req):
        if req.url.path=='/api/v1/session':
            return httpx.Response(200,json={},headers={'CST':'c','X-SECURITY-TOKEN':'s'})
        if req.method=='PUT' and req.url.path=='/api/v1/positions/gold-deal':
            payload=json.loads(req.content)
            requests.append(payload)
            if 'limitLevel' in payload:return httpx.Response(400,json={'errorCode':'invalid.field'})
            return httpx.Response(200,json={'dealReference':'update-ref'})
        return httpx.Response(404)
    async def scenario():
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='https://broker.invalid')
        broker=CapitalBroker('api','id','password',client=client)
        await broker.connect()
        result=await broker.update_position('gold-deal',profit_level=4171.11)
        assert result['dealReference']=='update-ref'
        await broker.close()
    async_run(scenario())
    assert requests==[{'profitLevel':4171.11}]


def test_position_update_both_levels_correct_keys():
    received=[]
    def handler(req):
        if req.url.path=='/api/v1/session':return httpx.Response(200,json={},headers={'CST':'c','X-SECURITY-TOKEN':'s'})
        received.append(json.loads(req.content))
        return httpx.Response(200,json={'dealReference':'ref'})
    async def scenario():
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='https://broker.invalid')
        broker=CapitalBroker('api','id','password',client=client)
        await broker.connect()
        await broker.update_position('deal',stop_level=4177.09,profit_level=4171.11)
        await broker.close()
    async_run(scenario())
    assert received==[{'stopLevel':4177.09,'profitLevel':4171.11}]
