"""SHADOW must learn from prior Capital closes — not only the last 24h bar."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app import ohlc_entry_zones
from app.engine import Store, SingleTradeEngine
from app import shadow_mistake_audit as sma


def _engine(store):
    e = SingleTradeEngine.__new__(SingleTradeEngine)
    e.store = store
    e.account = {'accounts': [
        {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
    e.mandate = {'epic': 'GOLD', 'size': '0.1'}
    e.brain = SimpleNamespace(epic='GOLD')
    e.intermarket = SimpleNamespace(state={})
    e._decision_candles = lambda epic, n=80: []
    e._news_risk = lambda: {}
    return e


def test_backfill_default_includes_closes_older_than_24h():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        e = _engine(store)
        now = datetime.now(timezone.utc)
        # 30h-old close — outside desk 24h pulse, inside 7d learn window.
        store.upsert_trade({
            'dealId': 'old-1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            'old-1', 3601, -0.55, currency='GBP', source='broker',
            overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            ((now - timedelta(hours=30)).isoformat(), 'old-1'))
        store.db.commit()
        n = e._backfill_mistake_audits_from_journal(limit=80)  # default 168h
        assert n >= 1
        lab = sma.lab_payload(store)
        assert lab['hist_n'] >= 1


def test_backfill_adapts_break_knobs_once_from_entry_brain():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        e = _engine(store)
        now = datetime.now(timezone.utc)
        deal = 'brk-past'
        store.upsert_trade({
            'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            deal, 3601, -1.20, currency='GBP', source='broker',
            overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            ((now - timedelta(hours=40)).isoformat(), deal))
        store.set('entry_brain:' + deal, {
            'direction': 'BUY', 'entry': 3600., 'entry_spread': 0.2,
            'ohlc_entry': {
                'action': 'BUY', 'trigger': 'break',
                'reason': 'ohlc_break_trigger',
            },
        })
        store.db.commit()
        before = ohlc_entry_zones.load_params(store)
        tape0 = int(before.get('tape_need') or 1)
        n = e._backfill_mistake_audits_from_journal(limit=80, hours=168)
        assert n >= 1
        after = ohlc_entry_zones.load_params(store)
        assert int(after.get('tape_need') or 0) > tape0
        # Second pass must not tighten again (once-key).
        mid = int(ohlc_entry_zones.load_params(store).get('tape_need') or 0)
        e._backfill_mistake_audits_from_journal(limit=80, hours=168)
        assert int(ohlc_entry_zones.load_params(store).get('tape_need') or 0) == mid
        stamped = store.get('ohlc_gates_observed:' + deal)
        assert isinstance(stamped, dict) and stamped.get('adapted') is True


def test_hours_24_still_skips_30h_old():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        e = _engine(store)
        now = datetime.now(timezone.utc)
        store.upsert_trade({
            'dealId': 'skip-old', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            'skip-old', 3601, -0.99, currency='GBP', source='broker',
            overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            ((now - timedelta(hours=30)).isoformat(), 'skip-old'))
        store.db.commit()
        n = e._backfill_mistake_audits_from_journal(limit=80, hours=24)
        assert n == 0
