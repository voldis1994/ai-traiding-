"""Admin + client multi-tenant platform and portals."""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.platform import PlatformStore
from app.runtime_hub import RuntimeHub
from app.security import hash_password, verify_password
from app.brokers import list_brokers, create_broker
from tests.test_integration import BrokerSimulation


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
        assert client.get('/').status_code==200
        assert client.get('/console',follow_redirects=False).status_code==302
        assert client.get('/console',follow_redirects=False).headers['location']=='/admin'
        assert client.get('/admin').status_code==200
        assert client.get('/app').status_code==200
        health=client.get('/api/health').json()
        assert health['portals']['client']=='/app'
        assert health['portals']['operator']=='/admin'
        assert health['portals']['admin']=='/admin'
        assert health['portals']['home']=='/'

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
