import json
from datetime import datetime, timezone
from app.external_risk import assess_news

NOW=datetime(2026,10,14,17,0,tzinfo=timezone.utc)

def test_unconfigured_explicitly_inactive(tmp_path):
    r=assess_news('ANY',tmp_path/'missing.json',now=NOW)
    assert r['entry_allowed'] and r['status']=='UNCONFIGURED'

def test_enabled_high_impact_only_for_matching_instrument(tmp_path):
    p=tmp_path/'calendar.json'
    p.write_text(json.dumps({'enabled':True,'updated_at':'2026-10-14T16:55:00Z',
      'instrument_currencies':{'EURUSD':['EUR','USD'],'BTC':['BTC']},
      'events':[{'at':'2026-10-14T17:10:00Z','impact':'HIGH','currencies':['USD'],'name':'US CPI'}]}))
    assert not assess_news('EURUSD',p,now=NOW)['entry_allowed']
    assert assess_news('BTC',p,now=NOW)['entry_allowed']

def test_stale_calendar_blocks_when_enabled(tmp_path):
    p=tmp_path/'calendar.json'
    p.write_text(json.dumps({'enabled':True,'updated_at':'2026-10-12T17:00:00Z','events':[]}))
    assert assess_news('ANY',p,now=NOW)['status']=='CALENDAR_UNAVAILABLE'
    assert not assess_news('ANY',p,now=NOW)['entry_allowed']

def test_invalid_event_blocks_when_enabled(tmp_path):
    p=tmp_path/'calendar.json'
    p.write_text(json.dumps({'enabled':True,'updated_at':'2026-10-14T17:00:00Z','events':[{}]}))
    assert not assess_news('ANY',p,now=NOW)['entry_allowed']
