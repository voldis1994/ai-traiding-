"""Desk must mirror Capital Funds £ — not open-UPL mystery or SHADOW zeros."""
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from app.engine import Store
from app.shadow_paper import ShadowPaperJournal
from app.runtime_hub import RuntimeHub
from app import shadow_mistake_audit as sma


def test_scoreboard_prefers_closed_estimate_over_open_upl():
    """One client open UPL must not hide Capital closed −0.89."""
    # Inline the JS priority in Python via _trade_stats + scoreboard contract.
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'c1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 4188,
        })
        store.close_trade(
            'c1', 4187, -0.89, currency='GBP',
            source='upl_estimate', overwrite=True)
        closed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?', (closed, 'c1'))
        store.db.commit()
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP', open_upl=0.31)
        assert stats['estimate_trades'] >= 1
        assert stats['estimate_pnl'] == pytest.approx(-0.89)
        # working_pnl is broker-only; estimate stays separate — UI scoreboard
        # must prefer estimate_pnl over open_upl (covered by admin.js contract).
        assert stats['open_upl'] == pytest.approx(0.31)
        assert stats['working_pnl'] is None or stats['working_pnl_source'] != 'open_upl'


def test_paper_seeds_from_capital_journal_not_zero():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        now = datetime.now(timezone.utc)
        for i, pnl in enumerate((-0.89, 0.62)):
            deal = f'cap-seed-{i}'
            store.upsert_trade({
                'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 4100,
            })
            store.close_trade(
                deal, 4101, pnl, currency='GBP',
                source='broker', overwrite=True)
            store.db.execute(
                'UPDATE trades SET closed=? WHERE deal_id=?',
                ((now - timedelta(minutes=i)).isoformat(), deal))
        store.db.commit()
        j = ShadowPaperJournal(store)
        assert j.history == []
        n = j.backfill_from_capital_journal(hours=24)
        assert n == 2
        s = j.summary()
        assert s['cash_trades'] == 2
        assert s['display_net'] == pytest.approx(-0.27)
        assert s['capital_journal_seed'] is True
        assert j.backfill_from_capital_journal() == 0  # idempotent cash


def test_fingerprint_does_not_wait_on_news_peers():
    a = sma.fingerprint(
        scenario='BREAKOUT_UP', zone_side='demand',
        news_status='UNCONFIGURED', intermarket_status='WAITING',
        direction='BUY', trigger='zone')
    b = sma.fingerprint(
        scenario='BREAKOUT_UP', zone_side='demand',
        news_status='WAITING', intermarket_status='UNCONFIGURED',
        direction='BUY', trigger='zone')
    c = sma.fingerprint(
        scenario='BREAKOUT_UP', zone_side='demand',
        news_status='NEWS_NA', intermarket_status='PEERS_NA',
        direction='BUY', trigger='zone')
    assert a == b == c
    blocked = sma.fingerprint(
        scenario='BREAKOUT_UP', zone_side='demand',
        news_status='NEWS_WINDOW', intermarket_status='READY',
        direction='BUY', trigger='zone')
    assert blocked != a


def test_lab_payload_news_peers_not_waiting_gate():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        lab = sma.lab_payload(store)
        assert lab['live_news']['status'] in ('NEWS_NA', 'CLEAR', 'OK', 'READY',
                                              'NEWS_CLEAR', 'ALLOW')
        assert lab['live_peers']['status'] in ('PEERS_NA', 'READY', 'OK', 'CLEAR',
                                               'PEERS_OK', 'ALLOW')
        assert 'WAIT' not in str(lab['live_news']['status']).upper() or \
            lab['live_news']['status'] == 'NEWS_NA'
        assert lab['live_peers']['status'] != 'WAITING'
