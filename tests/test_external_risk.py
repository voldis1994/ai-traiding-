import json
from datetime import datetime, timezone
from app.external_risk import assess_news

NOW=datetime(2026,10,14,17,0,tzinfo=timezone.utc)

def test_unconfigured_explicitly_inactive(tmp_path):
    r=assess_news('ANY',tmp_path/'missing.json',now=NOW)
    assert r['entry_allowed'] and r['status']=='UNCONFIGURED'

def test_enabled_high_impact_is_info_not_open_veto(tmp_path):
    """V5 D05/L04: NEWS_WINDOW is context — never HOLD_NEWS / OPEN block."""
    p=tmp_path/'calendar.json'
    p.write_text(json.dumps({'enabled':True,'updated_at':'2026-10-14T16:55:00Z',
      'instrument_currencies':{'EURUSD':['EUR','USD'],'BTC':['BTC']},
      'events':[{'at':'2026-10-14T17:10:00Z','impact':'HIGH','currencies':['USD'],'name':'US CPI'}]}))
    win=assess_news('EURUSD',p,now=NOW)
    assert win['status']=='NEWS_WINDOW'
    assert win['entry_allowed'] is True
    assert assess_news('BTC',p,now=NOW)['entry_allowed']

def test_stale_calendar_does_not_block_open(tmp_path):
    """V5 D08/T04: CALENDAR_UNAVAILABLE must not REQUIRED→HOLD OPEN."""
    p=tmp_path/'calendar.json'
    p.write_text(json.dumps({'enabled':True,'updated_at':'2026-10-12T17:00:00Z','events':[]}))
    r=assess_news('ANY',p,now=NOW)
    assert r['status']=='CALENDAR_UNAVAILABLE'
    assert r['entry_allowed'] is True

def test_invalid_event_does_not_block_open(tmp_path):
    p=tmp_path/'calendar.json'
    p.write_text(json.dumps({'enabled':True,'updated_at':'2026-10-14T17:00:00Z','events':[{}]}))
    r=assess_news('ANY',p,now=NOW)
    assert r['entry_allowed'] is True
    assert r['status']=='CALENDAR_UNAVAILABLE'
