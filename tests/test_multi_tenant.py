"""Admin + client multi-tenant platform and portals."""
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core import client_neural_seed
from app.engine import AdaptiveBrain, Store
from app.platform import PlatformStore
from app.runtime_hub import RuntimeHub
from app.security import hash_password, verify_password
from app.brokers import list_brokers, create_broker
from tests.test_integration import BrokerSimulation


def test_client_neural_seeds_are_distinct():
    """Fresh brains must not all clone seed=7 — identical weights ⇒ identical BUY."""
    a=client_neural_seed(1)
    b=client_neural_seed(2)
    c=client_neural_seed(99)
    assert a!=7 and b!=7 and c!=7
    assert a!=b!=c
    assert client_neural_seed(1)==a  # stable


def test_adaptive_brain_seed_diverges_fresh_weights(tmp_path):
    da=tmp_path/'a';db=tmp_path/'b';da.mkdir();db.mkdir()
    s1=Store(da/'robot.sqlite3');s2=Store(db/'robot.sqlite3')
    b1=AdaptiveBrain(s1,data_dir=da,seed=client_neural_seed(1))
    b2=AdaptiveBrain(s2,data_dir=db,seed=client_neural_seed(2))
    assert b1.init_seed!=b2.init_seed
    assert not np.allclose(b1.core.P,b2.core.P)


def test_hub_assigns_per_client_seed(tmp_path):
    import asyncio
    platform=PlatformStore(tmp_path/'platform.sqlite3',tmp_path/'secret.key')
    hub=RuntimeHub(platform)
    c1=platform.create_client('alice','pass1234','Alice','en')
    c2=platform.create_client('bob','pass1234','Bob','en')
    a1=platform.attach_broker(c1['id'],'capital_com','A','key-a','id-a','pw-a')
    a2=platform.attach_broker(c2['id'],'capital_com','B','key-b','id-b','pw-b')

    class LoginBroker(BrokerSimulation):
        def __init__(self,*a,**k):
            super().__init__();self.open=[]
        async def connect(self):return {'authenticated':True}
        async def close(self):pass
        async def accounts(self):
            return {'accounts':[{'accountId':'1','balance':{'balance':10,'available':10}}]}

    import app.runtime_hub as rh
    orig=rh.create_broker
    rh.create_broker=lambda *a,**k:LoginBroker()

    async def scenario():
        try:
            r1=await hub.start_account(a1)
            r2=await hub.start_account(a2)
            assert r1.brain.init_seed==client_neural_seed(c1['id'])
            assert r2.brain.init_seed==client_neural_seed(c2['id'])
            assert r1.brain.init_seed!=r2.brain.init_seed
            assert not np.allclose(r1.brain.core.P,r2.brain.core.P)
            view=r1.client_view()
            assert 'entry_block_reason' in view
            assert view['neural_seed']==r1.brain.init_seed
            assert f"client_{c1['id']}" in str(r1.engine.policy.path)
            assert f"client_{c2['id']}" in str(r2.engine.policy.path)
            assert r1.engine.policy.path!=r2.engine.policy.path
        finally:
            await hub.shutdown()
            rh.create_broker=orig

    asyncio.run(scenario())


def test_duplicate_api_key_rejected(tmp_path):
    store=PlatformStore(tmp_path/'platform.sqlite3',tmp_path/'secret.key')
    a=store.create_client('one','pass1234','One','lv')
    b=store.create_client('two','pass1234','Two','lv')
    store.attach_broker(a['id'],'capital_com','One LIVE','SAME-KEY','id1','secret')
    with pytest.raises(ValueError,match='API atslēga'):
        store.attach_broker(b['id'],'capital_com','Two LIVE','SAME-KEY','id2','secret')
    # Same client may re-attach / update own key
    store.attach_broker(a['id'],'capital_com','One LIVE','SAME-KEY','id1','secret2')


def test_password_hash_roundtrip():
    encoded=hash_password('secret-pass')
    assert verify_password('secret-pass',encoded)
    assert not verify_password('wrong',encoded)


def test_platform_client_and_encrypted_broker(tmp_path):
    store=PlatformStore(tmp_path/'platform.sqlite3',tmp_path/'secret.key')
    store.ensure_admin('admin','admin123')
    assert store.verify_admin('admin','admin123')
    assert store.verify_admin('admin','nope') is None
    client=store.create_client('janis','pass1234','Jānis','lv')
    assert client['username']=='janis'
    account=store.attach_broker(client['id'],'capital_com','Janis LIVE',
                                'key','id','secret')
    creds=store.broker_credentials(account)
    assert creds['api_key']=='key'
    assert creds['password']=='secret'
    assert store.get_broker_for_client(client['id'])['display_name']=='Janis LIVE'


def test_broker_registry_capital_and_slot():
    ids={b['id'] for b in list_brokers()}
    assert 'capital_com' in ids
    assert 'generic_rest' in ids
    broker=create_broker('capital_com','a','b','c')
    assert broker.__class__.__name__=='CapitalBroker'
    try:
        create_broker('generic_rest','a','b','c')
        assert False, 'slot must raise'
    except ValueError as exc:
        assert 'slot' in str(exc).lower() or 'adapter' in str(exc).lower()


def test_admin_and_client_portals(monkeypatch,tmp_path):
    from app import main
    main.platform=PlatformStore(tmp_path/'platform.sqlite3',tmp_path/'secret.key')
    main.hub=RuntimeHub(main.platform)
    main.platform.ensure_admin('admin','admin123')

    class LoginBroker(BrokerSimulation):
        def __init__(self,*args,**kwargs):
            super().__init__();self.open=[]
        async def connect(self):return {'authenticated':True}
        async def close(self):pass
        async def accounts(self):
            return {'accounts':[{'accountId':'1','balance':{'balance':10,'available':10}}]}

    monkeypatch.setattr(main,'CapitalBroker',LoginBroker)
    monkeypatch.setattr(main,'create_broker',lambda *a,**k:LoginBroker())
    monkeypatch.setattr('app.brokers.CapitalBroker',LoginBroker)
    monkeypatch.setattr('app.runtime_hub.create_broker',lambda *a,**k:LoginBroker())

    with TestClient(main.app) as client:
        # `/` opens admin directly (operator local UX — no portal chooser).
        assert client.get('/',follow_redirects=False).status_code==302
        assert client.get('/',follow_redirects=False).headers['location']=='/admin'
        assert client.get('/').status_code==200
        assert client.get('/console',follow_redirects=False).status_code==302
        assert client.get('/console',follow_redirects=False).headers['location']=='/admin'
        assert client.get('/admin').status_code==200
        assert client.get('/app').status_code==200
        assert client.get('/portal').status_code==200
        health=client.get('/api/health').json()
        assert health['portals']['client']=='/app'
        assert health['portals']['operator']=='/admin'
        assert health['portals']['admin']=='/admin'
        assert health['portals']['home']=='/admin'

        assert client.post('/api/admin/login',json={'username':'admin','password':'x'}).status_code==401
        assert client.post('/api/admin/login',json={'username':'admin','password':'admin123'}).status_code==200
        assert client.get('/api/admin/me').status_code==200

        created=client.post('/api/admin/clients',json={
            'username':'anna','password':'anna-pass','display_name':'Anna','language':'en'})
        assert created.status_code==200
        cid=created.json()['client']['id']

        attach=client.post(f'/api/admin/clients/{cid}/broker',json={
            'broker_type':'capital_com','display_name':'Anna Capital',
            'api_key':'k','identifier':'i','password':'p'})
        assert attach.status_code==200, attach.text

        overview=client.get('/api/admin/overview')
        assert overview.status_code==200, overview.text
        body=overview.json()
        assert body['summary']['clients']>=1
        assert body['summary']['running_robots']>=1
        card=next(c for c in body['clients'] if c['id']==cid)
        assert 'evolution' in card and 'results' in card and 'scores' in card
        assert 'neurons' in card['evolution']
        assert client.post(f'/api/admin/clients/{cid}/pause').status_code==200
        assert client.post(f'/api/admin/clients/{cid}/resume').status_code==200

        # Same browser jar can hold admin + client cookies (different names).
        login=client.post('/api/client/login',json={'username':'anna','password':'anna-pass'})
        assert login.status_code==200
        status=client.get('/api/client/status')
        assert status.status_code==200
        assert status.json()['phase'] in ('guide','starting','robot')
        assert client.post('/api/client/language',json={'language':'ru'}).json()['client']['language']=='ru'

        rt=main.hub.get(cid)
        assert rt is not None
        rt.engine.broker.open=[{'position':{'dealId':'m1','size':0.04,'direction':'SELL',
            'level':100,'upl':-0.01},'market':{'epic':'GOLD','instrumentName':'Gold Spot'}}]
        refreshed=client.post('/api/client/refresh')
        assert refreshed.status_code==200, refreshed.text
        assert refreshed.json()['phase']=='robot'
        assert refreshed.json()['view']['mandate']['epic']=='GOLD'
        assert client.post('/api/client/pause').json()['ok']
        assert client.post('/api/client/resume').json()['ok']
