"""Forex Factory's public weekly JSON export -> validated local news snapshot.

The feed is fetched over HTTPS, never by scraping HTML. Times are parsed with
UTC offsets exactly as published. Fetch failure is returned to the engine,
which blocks new entries. No manufactured economic events or API credentials.
"""
import json
import os
from datetime import datetime,timezone,timedelta
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .external_risk import _utc

FOREX_FACTORY_JSON='https://nfs.faireconomy.media/ff_calendar_thisweek.json'


def convert_forex_factory(events, *, now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(events,list) or not events or len(events)>2500:
        raise ValueError('Forex Factory feed is empty or too large')
    out=[];local_dates=[]
    for e in events:
        if not isinstance(e,dict):raise ValueError('Invalid Forex Factory event')
        country=str(e.get('country','')).strip().upper()
        impact=str(e.get('impact','')).strip().upper()
        stamp=e.get('date');at=_utc(stamp)
        if not country or not e.get('title') or impact not in ('HIGH','MEDIUM','LOW','HOLIDAY','NON-ECONOMIC'):
            raise ValueError('Forex Factory event missing currency, title or valid impact')
        # Week is identified using provider's own source-local date, not our PC time zone.
        local_dates.append(datetime.fromisoformat(stamp.replace('Z','+00:00')).date())
        out.append({'at':at.isoformat(),'name':str(e['title'])[:160],
                    'impact':impact,'currencies':[country], 'epics':[]})
    earliest=min(local_dates)
    # Forex Factory 'this week' starts Sunday; validate date coverage.
    sunday=earliest-timedelta(days=(earliest.weekday()+1)%7)
    end=sunday+timedelta(days=7)
    if not (sunday<=now.date()<end):
        raise ValueError('Forex Factory calendar is from a different week')
    return {'enabled':True,'source':'Forex Factory weekly JSON',
            'source_url':FOREX_FACTORY_JSON,'updated_at':now.isoformat(),
            'coverage_start':sunday.isoformat(),'coverage_end':end.isoformat(),
            'max_age_hours':1,'before_minutes':30,'after_minutes':30,
            'events':out}


async def refresh_calendar(path, *, url=None, client=None, now=None):
    url = url if url is not None else os.getenv('NEURAL_ECONOMIC_CALENDAR_URL', FOREX_FACTORY_JSON).strip()
    if not url:
        return {'configured':False,'updated':False,'reason':'No economic calendar URL configured'}
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        return {'configured':True,'updated':False,'reason':'Calendar URL must be HTTPS without credentials in URL'}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=12,follow_redirects=False) as session:
                response=await session.get(url)
        else:response=await client.get(url)
        response.raise_for_status()
        if len(response.content)>2_000_000:raise ValueError('calendar payload too large')
        raw=response.json()
        if isinstance(raw,list):
            # Require known source, not arbitrary untrusted HTML endpoints.
            if parsed.hostname!='nfs.faireconomy.media' or parsed.path!='/ff_calendar_thisweek.json':
                raise ValueError('Unexpected raw calendar provider')
            payload=convert_forex_factory(raw,now=now)
        elif isinstance(raw,dict):
            # Backwards-compatible custom JSON calendar URL.
            payload=raw
            if payload.get('enabled') is not True or not isinstance(payload.get('events'),list):
                raise ValueError('feed is not an enabled calendar document')
            _utc(payload['updated_at'])
        else:raise ValueError('Unsupported calendar payload')
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.download')
        temp.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
        temp.replace(path)
        return {'configured':True,'updated':True,'reason':'Calendar feed refreshed',
                'count':len(payload['events']),'source':payload.get('source','Custom JSON calendar')}
    except (httpx.HTTPError,ValueError,KeyError,OSError,TypeError) as exc:
        return {'configured':True,'updated':False,
                'reason':'Calendar feed refresh failed: '+type(exc).__name__+': '+str(exc)[:110]}
