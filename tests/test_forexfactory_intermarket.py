import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx
import pytest
from app.calendar_feed import refresh_calendar,convert_forex_factory,FOREX_FACTORY_JSON
from app.external_risk import assess_news
from app.instrument_profile import instrument_profile
from app.intermarket import IntermarketMonitor,align_and_assess,pick_market

NOW=datetime(2026,10,14,17,0,tzinfo=timezone.utc)

def ff_record(*,impact='High',country='USD',when='2026-10-14T13:10:00-04:00',title='US CPI'):
    return {'title':title,'country':country,'date':when,'impact':impact,'forecast':'2.5%','previous':'2.4%'}


def test_real_forex_factory_schema_week_and_timezone(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=[ff_record()]))) as client:
            return await refresh_calendar(tmp_path/'economic_calendar.json',url=FOREX_FACTORY_JSON,client=client,now=NOW)
    result=asyncio.run(scenario())
    assert result['updated'] and result['count']==1
    raw=json.loads((tmp_path/'economic_calendar.json').read_text())
    assert raw['events'][0]['at']=='2026-10-14T17:10:00+00:00'
    assert raw['events'][0]['impact']=='HIGH'
    assert raw['source']=='Forex Factory weekly JSON'
    assert raw['coverage_start']=='2026-10-11'
    assert assess_news('EURUSD',tmp_path/'economic_calendar.json',now=NOW,instrument_currencies=['EUR','USD'])['status']=='NEWS_WINDOW'
    assert assess_news('GBPJPY',tmp_path/'economic_calendar.json',now=NOW,instrument_currencies=['GBP','JPY'])['status']=='CLEAR'
    assert assess_news('UNKNOWN',tmp_path/'economic_calendar.json',now=NOW,instrument_currencies=[])['status']=='UNMAPPED_INSTRUMENT'


def test_wrong_week_or_malformed_feed_never_written(tmp_path):
    file=tmp_path/'econ.json'
    file.write_text('important unchanged')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=[ff_record(when='2026-09-01T13:00:00-04:00')]))) as client:
            return await refresh_calendar(file,url=FOREX_FACTORY_JSON,client=client,now=NOW)
    result=asyncio.run(scenario())
    assert not result['updated'] and file.read_text()=='important unchanged'
    with pytest.raises(ValueError):convert_forex_factory([ff_record(when='2026-10-14T13:00:00')],now=NOW)


def test_high_impact_window_only_and_stale_fail_closed(tmp_path):
    file=tmp_path/'econ.json'
    file.write_text(json.dumps(convert_forex_factory([
        ff_record(when='2026-10-14T13:31:00-04:00'),
        ff_record(when='2026-10-14T13:10:00-04:00',impact='Low',country='EUR',title='minor')],now=NOW)))
    # High event 31 min away -> no block, low even within range -> no block.
    assert assess_news('EURUSD',file,now=NOW,instrument_currencies=['EUR','USD'])['status']=='CLEAR'
    assert assess_news('EURUSD',file,now=NOW+timedelta(minutes=2),instrument_currencies=['EUR','USD'])['status']=='NEWS_WINDOW'
    assert assess_news('EURUSD',file,now=NOW+timedelta(hours=2),instrument_currencies=['EUR','USD'])['status']=='CALENDAR_UNAVAILABLE'


def test_profile_multiple_asset_classes():
    assert instrument_profile('CS.D.EURUSD.CFD.IP')['currencies']==['EUR','USD']
    assert instrument_profile('GOLD')['currencies']==['USD']
    assert instrument_profile('NASDAQ')['kind']=='INDICES'
    assert instrument_profile('BITCOIN')['kind']=='CRYPTO'
    assert instrument_profile('SIM')['mapped'] is False
    assert instrument_profile('RANDOM',{'currency':'GBP'})['currencies']==['GBP']


def bars(start, *, multiplier=1., inverse=False):
    rows=[]
    price=100.
    for i in range(61):
        change=(.03 if i%3 else -.018)*multiplier
        price*=1.+change/100
        moment=start+timedelta(minutes=i)
        rows.append({'at':moment.isoformat(), 'close':price})
    return rows


def as_peer(rows, *, invert=False):
    prices=[]
    for r in rows:
        close=r['close']
        if invert:close=10000./close
        prices.append({'snapshotTimeUTC':r['at'],'closePrice':{'bid':close-.001,'ask':close+.001}})
    return {'prices':prices}


def test_intermarket_closed_bar_correlations_and_leakage():
    start=datetime(2026,10,14,15,59,tzinfo=timezone.utc)
    primary=bars(start)
    last=datetime(2026,10,14,17,0,tzinfo=timezone.utc)
    result=align_and_assess(primary,as_peer(primary),current=last)
    assert result['ready'] and result['stable'] and result['correlation']>.99
    assert result['samples']==60
    inverse=align_and_assess(primary,as_peer(primary,invert=True),current=last)
    assert inverse['ready'] and inverse['stable'] and inverse['correlation']<-.99
    # A forming 17:00 candle does not leak into statistics.
    forming={'at':last.isoformat(),'close':1000000.}
    assert align_and_assess(primary+[forming],as_peer(primary),current=last)==result
    # No continuity => no opinion from misaligned history.
    assert not align_and_assess(primary,{'prices':as_peer(primary)['prices'][:-1]},current=last)['ready']


def test_intermarket_discovery_and_contradiction():
    assert pick_market({'markets':[{'epic':'XX','instrumentName':'Gold Miner'},
                   {'epic':'GOLD','instrumentName':'Gold'}]},'Gold','EURUSD')['epic']=='GOLD'
    assert pick_market({'markets':[{'epic':'XX','instrumentName':'Gold Miner'}]},'Gold','EURUSD') is None
    monitor=IntermarketMonitor()
    monitor.state={'status':'READY','peers':[
        {'ready':True,'stable':True,'direction':-1},
        {'ready':True,'stable':True,'direction':-1},
        {'ready':True,'stable':False,'direction':1}]}
    assert monitor.assess('BUY')['status']=='CONTRADICTION'
    assert monitor.assess('SELL')['entry_allowed']
    monitor.state={'status':'UNAVAILABLE','reason':'No broker history'}
    assert not monitor.assess('BUY')['entry_allowed']


def test_live_broker_reference_discovery_without_inventing_prices():
    now=datetime(2026,10,14,17,0,tzinfo=timezone.utc)
    primary=bars(datetime(2026,10,14,15,59,tzinfo=timezone.utc))
    class Broker:
        async def search_markets(self,term):
            return {'markets':[{'epic':term.upper().replace(' ',''),'instrumentName':term}]}
        async def prices(self,epic,resolution='MINUTE',maximum=90):
            return as_peer(primary)
    async def scenario():
        m=IntermarketMonitor()
        result=await m.refresh(Broker(),'EURUSD',{'name':'EUR/USD'},primary,current=now,force=True)
        assert result['status']=='READY' and result['reliable_peers']>=2
        assert len(result['peers'])<=3
        assert all(p['samples']==60 for p in result['peers'])
    asyncio.run(scenario())


def test_engine_news_guard_applies_without_duplicate_intermarket_veto(tmp_path):
    from app.engine import Store,AdaptiveBrain,SingleTradeEngine
    s=Store(tmp_path/'robot.db');brain=AdaptiveBrain(s,data_dir=tmp_path)
    e=SingleTradeEngine(s,brain)
    e.last_calendar_refresh=1.0
    e.mandate={'epic':'EURUSD','size':'1','initial_deal_id':'manual'}
    e.instrument_profile=instrument_profile('EURUSD')
    e.news_calendar_path=tmp_path/'calendar.json'
    current=datetime.now(timezone.utc)
    event=(current+timedelta(minutes=10)).astimezone(timezone(timedelta(hours=-4))).isoformat()
    e.news_calendar_path.write_text(json.dumps(convert_forex_factory([ff_record(when=event)],now=current)))
    e.intermarket.state={'status':'READY','peers':[]}
    # Test the actual safety gate used immediately before an order is submitted.
    assert not e._external_entry_check('BUY')['entry_allowed']
    old=(current-timedelta(hours=4)).astimezone(timezone(timedelta(hours=-4))).isoformat()
    e.news_calendar_path.write_text(json.dumps(convert_forex_factory([ff_record(when=old)],now=current)))
    assert e._external_entry_check('BUY')['entry_allowed']
    e.intermarket.state={'status':'UNAVAILABLE','reason':'No trustworthy cross-market reference'}
    assert e._external_entry_check('BUY')['entry_allowed']  # Neural features already evaluate peer data.


def test_cleared_or_deleted_calendar_blocks_new_entries(tmp_path):
    from app.engine import Store,AdaptiveBrain,SingleTradeEngine
    s=Store(tmp_path/'robot.db');brain=AdaptiveBrain(s,data_dir=tmp_path)
    e=SingleTradeEngine(s,brain)
    e.last_calendar_refresh=1.0
    e.mandate={'epic':'GOLD'}
    e.instrument_profile=instrument_profile('GOLD')
    e.news_calendar_path=tmp_path/'missing.json'
    assert e._news_risk()['status']=='CALENDAR_UNAVAILABLE'
    e.calendar_feed_error='Service unavailable'
    assert e._news_risk()['status']=='CALENDAR_FEED_ERROR'


def test_explicit_new_market_requires_flat_broker_and_preserves_learning(tmp_path):
    from app.engine import Store,AdaptiveBrain,SingleTradeEngine
    s=Store(tmp_path/'robot.db');brain=AdaptiveBrain(s,data_dir=tmp_path)
    e=SingleTradeEngine(s,brain)
    class Broker:
        open_positions=[]
        async def positions(self):return self.open_positions
    b=Broker()
    e.attach(b)
    e.mandate={'epic':'GOLD','size':'0.2','initial_deal_id':'old'}
    e.store.set('mandate',e.mandate)
    e.last_closed_step=20
    e.brain.core.steps=5
    async def scenario():
        b.open_positions=[{'position':{'dealId':'active','size':.2,'direction':'BUY','level':100},'market':{'epic':'GOLD'}}]
        with pytest.raises(ValueError,match='Close all'):
            await e.prepare_new_instrument()
        assert e.mandate['epic']=='GOLD'
        b.open_positions=[]
        result=await e.prepare_new_instrument()
        assert result['ok'] and e.mandate is None and s.get('mandate') is None
        assert e.last_closed_step==-1 and e.brain.core.steps==5
        b.open_positions=[{'position':{'dealId':'new-manual','size':.5,'direction':'SELL','level':120},'market':{'epic':'EURUSD'}}]
        await e.sync()
        assert e.mandate['epic']=='EURUSD' and e.mandate['size']=='0.5'
        assert e.last_closed_step==-1 # First manual trade has not closed yet.
    asyncio.run(scenario())
