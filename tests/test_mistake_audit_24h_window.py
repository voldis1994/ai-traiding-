"""Mistake-audit cycle must read full 24h wins+losses, not only last hurt."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.engine import Store, SingleTradeEngine
from app import shadow_mistake_audit as sma


def test_backfill_audits_wins_and_losses_in_24h():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        # Avoid AdaptiveBrain boot — only need journal backfill + store.
        e = SingleTradeEngine.__new__(SingleTradeEngine)
        e.store = store
        e.mandate = {'epic': 'GOLD', 'size': '0.1'}
        e.brain = SimpleNamespace(epic='GOLD')
        e.intermarket = SimpleNamespace(state={})
        e._decision_candles = lambda epic, n=80: []
        e._news_risk = lambda: {}
        now = datetime.now(timezone.utc)
        closes = [
            ('d-win', 0.70, now - timedelta(hours=1)),
            ('d-loss', -0.33, now - timedelta(hours=2)),
            ('d-win2', 0.60, now - timedelta(hours=3)),
            ('d-old', -0.99, now - timedelta(hours=30)),  # outside 24h
        ]
        for deal, pnl, when in closes:
            store.upsert_trade({
                'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 3600,
            })
            store.close_trade(
                deal, 3601, pnl, currency='GBP', source='broker',
                overwrite=True)
            # Force closed timestamp into the intended window.
            store.db.execute(
                'UPDATE trades SET closed=? WHERE deal_id=?',
                (when.isoformat(), deal))
            store.db.commit()
        n = e._backfill_mistake_audits_from_journal(limit=80, hours=24)
        assert n >= 3  # win + loss + win2 (not the 30h-old)
        lab = sma.lab_payload(store)
        w = lab['window_24h']
        assert w['reads'] == 'wins+losses'
        assert w['n'] >= 3
        assert w['wins'] >= 2
        assert w['losses'] >= 1


def test_hist_window_stats_counts_both_sides():
    now = datetime.now(timezone.utc).isoformat()
    hist = [
        {'at': now, 'pnl': 0.70, 'source': 'live', 'mistakes': []},
        {'at': now, 'pnl': -0.33, 'source': 'live', 'mistakes': ['outcome_hurt']},
        {'at': now, 'pnl': 0.60, 'source': 'shadow', 'mistakes': []},
    ]
    w = sma._hist_window_stats(hist, hours=24)
    assert w['n'] == 3
    assert w['wins'] == 2
    assert w['losses'] == 1
    assert abs(w['net'] - (0.70 - 0.33 + 0.60)) < 1e-6
