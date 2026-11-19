import asyncio
import json
import httpx
from app.calendar_feed import refresh_calendar
from app.external_risk import assess_news


def test_calendar_https_refresh(tmp_path):
    doc={'enabled':True,'updated_at':'2026-10-14T17:00:00Z','events':[]}
    def handler(req):return httpx.Response(200,json=doc)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await refresh_calendar(tmp_path/'calendar.json',url='https://calendar.example.org/events',client=client)
    result=asyncio.run(run())
    assert result['updated']
    assert json.loads((tmp_path/'calendar.json').read_text())==doc


def test_reject_insecure_feed(tmp_path):
    result=asyncio.run(refresh_calendar(tmp_path/'calendar.json',url='http://calendar.example.org/events'))
    assert not result['updated']
    assert not (tmp_path/'calendar.json').exists()


def test_reject_disabled_feed(tmp_path):
    def handler(req):return httpx.Response(200,json={'enabled':False,'events':[],'updated_at':'2026-10-14T17:00:00Z'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await refresh_calendar(tmp_path/'calendar.json',url='https://calendar.example.org/events',client=client)
    assert not asyncio.run(run())['updated']


def test_rate_limit_429_keeps_cache_and_flags(tmp_path):
    from app.calendar_feed import FOREX_FACTORY_JSON
    cache=tmp_path/'economic_calendar.json'
    cache.write_text('{"enabled":true,"events":[]}')
    def handler(req):
        return httpx.Response(429,headers={'Retry-After':'120'},text='Too Many Requests')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await refresh_calendar(cache,url=FOREX_FACTORY_JSON,client=client)
    result=asyncio.run(run())
    assert not result['updated']
    assert result.get('rate_limited') is True
    assert cache.read_text()=='{"enabled":true,"events":[]}'
