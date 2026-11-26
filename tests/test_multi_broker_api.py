"""Multi-broker registry: Capital, IG, Capital-compatible custom host."""
import json

import httpx
import pytest

from app.broker import CapitalBroker
from app.brokers import create_broker, list_brokers
from app.brokers.ig import IgBroker
from app.platform import PlatformStore


def test_list_brokers_includes_ig_and_generic():
    ids={b['id'] for b in list_brokers()}
    assert 'capital_com' in ids
    assert 'ig_markets' in ids
    assert 'ig_demo' in ids
    assert 'generic_rest' in ids
    generic=next(b for b in list_brokers() if b['id']=='generic_rest')
    assert generic['needs_base_url'] is True
    assert generic['status']=='live'


def test_create_capital_and_ig():
    c=create_broker('capital_com','k','u','p')
    assert isinstance(c,CapitalBroker)
    assert c.streaming_url
    ig=create_broker('ig_markets','k','u','p')
    assert isinstance(ig,IgBroker)
    assert ig.streaming_url is None
    demo=create_broker('ig_demo','k','u','p')
    assert isinstance(demo,IgBroker)
    assert 'demo-api.ig.com' in demo.base


def test_generic_rest_requires_https_base_url():
    with pytest.raises(ValueError,match='base URL'):
        create_broker('generic_rest','k','u','p')
    with pytest.raises(ValueError,match='https'):
        create_broker('generic_rest','k','u','p',base_url='http://evil.example')
    b=create_broker('generic_rest','k','u','p',
                    base_url='https://api-capital.backend-capital.com')
    assert isinstance(b,CapitalBroker)
    assert b.base=='https://api-capital.backend-capital.com'
    assert b.streaming_url is None


def test_platform_stores_base_url(tmp_path):
    store=PlatformStore(tmp_path/'platform.sqlite3',tmp_path/'secret.key')
    client=store.create_client('anna','pass1234','Anna','lv')
    account=store.attach_broker(
        client['id'],'generic_rest','Anna custom','key','id','secret',
        base_url='https://broker.example.com')
    creds=store.broker_credentials(account)
    assert creds['broker_type']=='generic_rest'
    assert creds['base_url']=='https://broker.example.com'
    # Capital path still works without endpoint.
    store.attach_broker(client['id'],'capital_com','Anna Cap','key2','id2','secret2')
    creds2=store.broker_credentials(store.get_broker_for_client(client['id']))
    assert creds2['base_url']==''


def test_ig_positions_normalize_upl():
    transport=httpx.MockTransport(lambda request:httpx.Response(
        200,
        json={'positions':[{
            'position':{'dealId':'D1','direction':'BUY','size':1,'level':100,
                        'currency':'GBP'},
            'market':{'epic':'CS.D.EURUSD.CFD.IP','bid':101,'offer':101.2},
        }]},
        headers={'CST':'c','X-SECURITY-TOKEN':'s'}))
    broker=IgBroker('k','u','p',client=httpx.AsyncClient(
        transport=transport,base_url='https://demo-api.ig.com/gateway/deal'))
    broker.cst='c';broker.security='s'

    import asyncio
    rows=asyncio.run(broker.positions())
    assert rows[0]['position']['upl']==pytest.approx(1.0)


def test_ig_connect_reads_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith('/session')
        assert request.headers.get('X-IG-API-KEY')=='k'
        assert json.loads(request.content)=={'identifier':'u','password':'p'}
        return httpx.Response(
            200,json={'currentAccountId':'ACC1'},
            headers={'CST':'tok','X-SECURITY-TOKEN':'sec'})
    broker=IgBroker('k','u','p',client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url='https://demo-api.ig.com/gateway/deal'))
    import asyncio
    body=asyncio.run(broker.connect())
    assert body['currentAccountId']=='ACC1'
    assert broker.cst=='tok' and broker.security=='sec'
