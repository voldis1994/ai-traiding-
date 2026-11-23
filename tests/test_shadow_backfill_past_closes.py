"""SHADOW learns from ALL closed trades + mirrors LIVE opens — no window blocks."""
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
    e.market = {
        'mid': 3600., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
        'bid': 3599.9, 'ask': 3600.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
    }
    e.positions = []
    e.shadow_brain = None
    e.shadow_paper = None
    e._decision_candles = lambda epic=None, n=80: []
    e._news_risk = lambda: {}
    e._ensure_shadow_brain = lambda: None
    return e


def test_backfill_default_includes_all_ages():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        e = _engine(store)
        now = datetime.now(timezone.utc)
        store.upsert_trade({
            'dealId': 'old-1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            'old-1', 3601, -0.55, currency='GBP', source='broker',
            overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            ((now - timedelta(hours=200)).isoformat(), 'old-1'))
        store.db.commit()
        n = e._backfill_mistake_audits_from_journal()  # hours=0 default
        assert n >= 1
        assert sma.lab_payload(store)['hist_n'] >= 1


def test_backfill_adapts_even_without_trigger_meta():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        e = _engine(store)
        now = datetime.now(timezone.utc)
        deal = 'no-trig'
        store.upsert_trade({
            'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            deal, 3601, -1.20, currency='GBP', source='upl_estimate',
            overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            (now.isoformat(), deal))
        # No ohlc_entry trigger — must still fall back and adapt.
        store.set('entry_brain:' + deal, {
            'direction': 'BUY', 'entry': 3600., 'entry_spread': 0.2,
        })
        store.db.commit()
        before = int(ohlc_entry_zones.load_params(store).get('tape_need') or 1)
        n = e._backfill_mistake_audits_from_journal()
        assert n >= 1
        after = int(ohlc_entry_zones.load_params(store).get('tape_need') or 0)
        assert after > before
        stamped = store.get('ohlc_gates_observed:' + deal)
        assert isinstance(stamped, dict) and stamped.get('adapted') is True


def test_hours_24_still_skips_30h_old_when_asked():
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


def test_shadow_mirrors_live_open_into_paper():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        from app.engine import AdaptiveBrain
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': '0.1'}
        e.market = {
            'mid': 3600., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
            'bid': 3599.9, 'ask': 3600.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        e._ensure_shadow_brain()
        deal = 'live-open-1'
        store.set('entry_brain:' + deal, {
            'direction': 'BUY', 'entry': 3600., 'tick_size': 0.1,
            'entry_spread': 0.2, 'stop_distance': 5.0, 'profit_distance': 10.0,
            'bracket_kappa': 2.0, 'bracket_sl_scale': 1.0,
            'bar_time': '2026-11-23T14:00:00+00:00',
            'ohlc_entry': {'trigger': 'break', 'reason': 'ohlc_break_trigger'},
        })
        e._shadow_mirror_live_open(deal)
        pos = e.shadow_paper.position
        assert pos is not None
        assert pos.get('direction') == 'BUY'
        assert abs(pos['entry'] - pos['stop']) == 5.0 or abs(
            abs(pos['entry'] - pos['stop']) - 5.0) < 1.0
