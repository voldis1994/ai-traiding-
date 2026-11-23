"""Capital GOLD epic CS.D.USCGC.* must not stay UNMAPPED_INSTRUMENT forever."""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.engine import Store, SingleTradeEngine
from app.external_risk import assess_news
from app.instrument_profile import instrument_profile


def test_uscgc_news_clear_not_unmapped():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        store = Store(root / 'db.sqlite3')
        now = datetime.now(timezone.utc)
        (root / 'economic_calendar.json').write_text(json.dumps({
            'enabled': True,
            'updated_at': now.isoformat(),
            'max_age_hours': 48,
            'before_minutes': 30,
            'after_minutes': 30,
            'source': 'Forex Factory weekly JSON',
            'coverage_start': (now - timedelta(days=1)).date().isoformat(),
            'coverage_end': (now + timedelta(days=6)).date().isoformat(),
            'events': [],
        }), encoding='utf-8')
        e = SingleTradeEngine.__new__(SingleTradeEngine)
        e.store = store
        e.mandate = {'epic': 'CS.D.USCGC.TODAY.IP', 'size': '0.1'}
        e.instrument_profile = {
            'mapped': False, 'currencies': [], 'kind': 'UNKNOWN'}
        e.news_calendar_path = root / 'economic_calendar.json'
        e.calendar_feed_error = None
        e.last_calendar_refresh = 1.0
        e._last_broker_market = {}
        e.market = {'instrument_name': 'Gold Spot'}
        e.intermarket = SimpleNamespace(
            state={'status': 'WAITING'}, epic='stale', peers=['x'],
            last_discovery=1.0)
        news = e._news_risk()
        assert news.get('status') != 'UNMAPPED_INSTRUMENT', news
        assert news.get('status') in ('CLEAR', 'DISABLED', 'CALENDAR_WAITING')
        assert e.instrument_profile.get('mapped') is True
        assert e.instrument_profile.get('currencies') == ['USD']
        # Peer rediscovery forced after remap from blank.
        assert e.intermarket.epic is None


def test_assess_news_uscgc_with_mapped_currencies():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        now = datetime.now(timezone.utc)
        cal = root / 'economic_calendar.json'
        cal.write_text(json.dumps({
            'enabled': True,
            'updated_at': now.isoformat(),
            'max_age_hours': 48,
            'before_minutes': 30,
            'after_minutes': 30,
            'source': 'Forex Factory weekly JSON',
            'coverage_start': (now - timedelta(days=1)).date().isoformat(),
            'coverage_end': (now + timedelta(days=6)).date().isoformat(),
            'events': [],
        }), encoding='utf-8')
        epic = 'CS.D.USCGC.TODAY.IP'
        cur = instrument_profile(epic)['currencies']
        out = assess_news(epic, cal, now=now, instrument_currencies=cur)
        assert out['status'] == 'CLEAR'
        assert out['entry_allowed'] is True
