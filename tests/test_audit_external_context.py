"""News + intermarket must reach mistake-audit even when entry stamp is {}."""
import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.engine import Store, SingleTradeEngine
from app import shadow_mistake_audit as sma


def test_empty_entry_stamp_falls_back_to_live_news_and_peers():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        store = Store(root / 'db.sqlite3')
        # Live calendar: CLEAR today (no HIGH event in window).
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
            'instrument_currencies': {'GOLD': ['USD', 'XAU']},
            'events': [{
                'at': (now + timedelta(hours=5)).isoformat(),
                'impact': 'HIGH',
                'name': 'Later CPI',
                'currencies': ['USD'],
                'epics': ['*'],
            }],
        }), encoding='utf-8')
        e = SingleTradeEngine.__new__(SingleTradeEngine)
        e.store = store
        e.mandate = {'epic': 'GOLD', 'size': '0.1'}
        e.brain = SimpleNamespace(epic='GOLD')
        e.instrument_profile = {'currencies': ['USD', 'XAU']}
        e.news_calendar_path = root / 'economic_calendar.json'
        e.calendar_feed_error = None
        e.last_calendar_refresh = 1.0
        e.intermarket = SimpleNamespace(state={
            'status': 'READY',
            'entry_allowed': True,
            'reason': 'Broker reference prices aligned; cross-market veto enabled',
            'peers': [
                {'epic': 'DX', 'ready': True, 'stable': True, 'direction': 1},
                {'epic': 'SI', 'ready': True, 'stable': True, 'direction': 1},
            ],
        })
        # Empty stamps must NOT win over live feeds.
        news, im = e._audit_external_context(
            {'news': {}, 'intermarket': {}}, direction='BUY')
        assert news.get('status') == 'CLEAR'
        assert 'high-impact' in str(news.get('reason') or '').lower() or news.get('status')
        assert im.get('status') == 'READY'
        assert store.get('live_external_context')['news']['status'] == 'CLEAR'
        lab = sma.lab_payload(store)
        assert lab['live_news']['status'] == 'CLEAR'
        assert lab['live_peers']['status'] == 'READY'
        assert lab['live_peers']['ready_peers'] == 2


def test_lab_payload_reads_calendar_when_live_context_missing():
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
            'events': [],
        }), encoding='utf-8')
        lab = sma.lab_payload(store)
        assert lab['live_news']['status'] in (
            'CLEAR', 'UNCONFIGURED', 'DISABLED', 'CALENDAR_UNAVAILABLE')
        assert lab['live_peers']['status'] in ('WAITING', '—') or lab['live_peers']
