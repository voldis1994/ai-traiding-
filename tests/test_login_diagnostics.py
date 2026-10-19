"""Login errors must distinguish each broker phase without leaking secrets."""
import httpx
from app.main import _capital_connect_error


def make_error(status):
    r=httpx.Response(status, request=httpx.Request('POST','https://capital.invalid/api/v1/session'),
                     json={'password':'SECRET_PASSWORD','apiKey':'SECRET_API_KEY'})
    return httpx.HTTPStatusError('contains SECRET_PASSWORD', request=r.request, response=r)


def test_invalid_credentials_only_when_session_rejected():
    e=_capital_connect_error('session', make_error(401))
    assert e.status_code==401 and 'LIVE autentifikāciju' in e.detail


def test_accounts_read_failure_not_called_wrong_password():
    e=_capital_connect_error('accounts', make_error(403))
    assert e.status_code==502 and 'konta nolasīšana' in e.detail
    assert 'paroli' not in e.detail


def test_position_read_failure_is_distinct():
    e=_capital_connect_error('positions', make_error(500))
    assert e.status_code==503 and 'pozīciju nolasīšana' in e.detail


def test_rate_limit_message():
    e=_capital_connect_error('accounts', make_error(429))
    assert e.status_code==429 and 'limits' in e.detail


def test_timeout_message():
    e=_capital_connect_error('session', httpx.ReadTimeout('SECRET_PASSWORD'))
    assert e.status_code==503 and 'neatbildēja' in e.detail


def test_no_secret_in_messages():
    for stage in ('session','accounts','positions'):
        for status in (400,401,403,429,500):
            e=_capital_connect_error(stage, make_error(status))
            assert 'SECRET_' not in e.detail


def test_login_route_reports_exact_broker_stage(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    class FailingBroker:
        stage='session'
        def __init__(self,*args,**kwargs):pass
        async def connect(self):
            if self.stage=='session':raise make_error(401)
        async def accounts(self):
            if self.stage=='accounts':raise make_error(429)
            return {'accounts':[]}
        async def positions(self):
            if self.stage=='positions':raise make_error(503)
            return []
        async def close(self):pass

    monkeypatch.setattr(main,'CapitalBroker',FailingBroker)
    with TestClient(main.app) as client:
        for stage, expected_code, phrase in [('session',401,'autentifikāciju'),
                                              ('accounts',429,'limits'),
                                              ('positions',503,'pozīciju nolasīšana')]:
            FailingBroker.stage=stage
            reply=client.post('/api/connect',json={
                'api_key':'SECRET_API_KEY','identifier':'SENSITIVE_EMAIL',
                'password':'SECRET_PASSWORD'})
            assert reply.status_code==expected_code
            assert phrase in reply.json()['detail']
            assert 'SECRET_' not in reply.text
            assert 'SENSITIVE_EMAIL' not in reply.text
