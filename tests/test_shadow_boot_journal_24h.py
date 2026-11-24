"""SHADOW must not show 24h 0/0 while Capital broker journal has today's closes."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import Store
from app import shadow_mistake_audit as sma
from app.runtime_hub import RuntimeHub
from app import strategy_gen


def test_lab_payload_uses_journal_when_audit_hist_empty():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        now = datetime.now(timezone.utc)
        for deal, pnl, hours_ago in (
            ('j1', 0.62, 0.5),
            ('j2', -0.19, 1.0),
            ('j3', -0.71, 2.0),
            ('j-old', -9.99, 30.0),
        ):
            store.upsert_trade({
                'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 3600,
            })
            store.close_trade(
                deal, 3601, pnl, currency='GBP', source='broker',
                overwrite=True)
            store.db.execute(
                'UPDATE trades SET closed=? WHERE deal_id=?',
                ((now - timedelta(hours=hours_ago)).isoformat(), deal))
            store.db.commit()
        # No audit hist — boot / bag wipe case from screenshots.
        lab = sma.lab_payload(store)
        w = lab['window_24h']
        assert w['from_journal'] is True
        assert w['n'] == 3
        assert w['wins'] == 1
        assert w['losses'] == 2
        assert abs(w['net'] - (0.62 - 0.19 - 0.71)) < 1e-6
        assert lab['recent_audits']
        assert lab['recent_audits'][0]['source'] == 'live_journal'
        assert lab['hurt_n'] >= 2


def test_journal_counts_upl_estimate_when_broker_pending():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        now = datetime.now(timezone.utc)
        store.upsert_trade({
            'dealId': 'u1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            'u1', 3601, -0.41, currency='GBP',
            source='upl_estimate', overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            (now.isoformat(), 'u1'))
        store.db.commit()
        w = sma._journal_window_stats(store, hours=24)
        assert w['n'] == 1
        assert w['losses'] == 1


def test_desk_shadow_lab_pulse_learning_when_journal_has_closes():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        strategy_gen.save(store, {'active_ids': ['GOLD|TREND_UP|BUY']})
        now = datetime.now(timezone.utc)
        store.upsert_trade({
            'dealId': 'x1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade('x1', 3601, -0.33, currency='GBP',
                          source='broker', overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            (now.isoformat(), 'x1'))
        store.db.commit()
        hub = RuntimeHub.__new__(RuntimeHub)

        class _RT:
            pass

        rt = _RT()
        rt.store = store
        rt.running = True
        rt.engine = None
        out = hub._desk_system_payload(
            rt, snap={'self_development': {'shadow_active': False}})
        lab = out['shadow_lab']
        assert lab['mistake_audit']['window_24h']['n'] >= 1
        assert lab['pulse'] == 'LEARNING'
