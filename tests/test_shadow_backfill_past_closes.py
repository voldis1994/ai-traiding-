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
            deal, 3601, -1.20, currency='GBP', source='broker',
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


def test_ohlc_learn_archive_survives_entry_brain_clear():
    """Backfill must still see full OPEN snapshot after entry_brain wipe."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        eng = _engine(store)
        deal = 'arch1'
        brain = {
            'direction': 'BUY',
            'entry': 100.0,
            'entry_spread': 0.1,
            'epic': 'IX.D.FTSE.IFM.IP',
            'scores': {'BUY': 0.55, 'SELL': 0.2, 'HOLD': 0.25},
            'news': {'status': 'CLEAR', 'entry_allowed': True},
            'intermarket': {'status': 'READY'},
            'candles_at_open': [
                {'open': 99 + i * 0.01, 'high': 99.2 + i * 0.01,
                 'low': 98.8 + i * 0.01, 'close': 99.1 + i * 0.01,
                 'at': f'2026-11-24T10:{i:02d}:00+00:00'}
                for i in range(24)
            ],
            'ohlc_entry': {
                'action': 'BUY',
                'trigger': 'break',
                'reason': 'blind_break',
                'zone': {'lo': 99.0, 'hi': 100.0},
                'scenario': {'scenario': 'RANGE', 'ready': True},
            },
            'structure_at_open': {'bias': 'RANGE'},
            'snapshot_version': 1,
        }
        store.set('entry_brain:' + deal, brain)
        payload = eng._archive_ohlc_for_shadow(deal, brain)
        assert payload and payload.get('ohlc_entry', {}).get('trigger') == 'break'
        assert len(payload.get('candles_at_open') or []) >= 16
        assert payload.get('scores', {}).get('BUY') == 0.55
        assert store.get('decision_snapshot:' + deal)
        store.set('entry_brain:' + deal, None)
        assert store.get('entry_brain:' + deal) is None
        loaded = eng._load_ohlc_learn_context(deal)
        assert loaded.get('ohlc_entry', {}).get('scenario', {}).get('scenario') == 'RANGE'
        assert len(loaded.get('candles_at_open') or []) >= 16
        assert loaded.get('news', {}).get('status') == 'CLEAR'
        meta = eng._shadow_ohlc_meta_for_learn(loaded.get('ohlc_entry'))
        assert meta.get('trigger') == 'break'


def test_mistake_audit_prefers_open_candles_snapshot():
    """Diagnose must use frozen OPEN tape, not a later live regime."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        eng = _engine(store)
        deal = 'snap1'
        open_bars = [
            {'open': 100.0, 'high': 100.2, 'low': 99.8, 'close': 100.0,
             'at': f'2026-11-24T09:{i:02d}:00+00:00'}
            for i in range(30)
        ]
        eng._archive_ohlc_for_shadow(deal, {
            'direction': 'BUY', 'entry': 100.0, 'entry_spread': 0.1,
            'epic': 'GOLD',
            'ohlc_entry': {
                'action': 'BUY', 'trigger': 'break',
                'zone': {'lo': 99.0, 'hi': 100.0},
                'scenario': {'scenario': 'RANGE'},
            },
            'candles_at_open': open_bars,
            'news': {'status': 'CLEAR', 'entry_allowed': True},
            'intermarket': {'status': 'WAITING'},
        })
        eng._decision_candles = lambda epic=None, n=80: [
            {'open': 100 + i, 'high': 101 + i, 'low': 99 + i, 'close': 100.5 + i,
             'at': f'2026-11-24T12:{i:02d}:00+00:00'}
            for i in range(40)
        ]
        out = eng._run_mistake_audit_close(
            deal, pnl=-0.4, direction='BUY', entry=100.0,
            source='live_backfill')
        assert out and out.get('audited')
        once = store.get('mistake_audited_close:' + deal) or {}
        assert once.get('open_snapshot') is True
        hist = store.get(sma.HIST_KEY) or []
        assert hist and 'open_snapshot_used' in (hist[-1].get('notes') or [])
