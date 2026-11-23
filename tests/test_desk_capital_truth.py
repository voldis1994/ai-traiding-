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


def test_capital_live_never_enters_independent_paper_cash():
    """P0: Capital Funds £ must not pollute Paper Book W/L / net."""
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
        # Simulate the bad PR #240 seed already in history.
        j = ShadowPaperJournal(store)
        j.history = [
            {'paper_pnl': -0.89, 'unit': 'account_currency',
             'source': 'capital_journal', 'deal_id': 'cap-seed-0'},
            {'paper_pnl': 0.62, 'unit': 'account_currency',
             'source': 'capital_journal', 'deal_id': 'cap-seed-1'},
        ]
        j._persist()
        assert j.purge_capital_journal_rows() == 2
        s = j.summary()
        assert s['cash_trades'] == 0
        assert s['display_trades'] == 0
        assert s['capital_journal_seed'] is False
        assert s['independent_of_live'] is True
        # Capital still visible via journal stats / CLIENT CLOSES path.
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['cash_trades'] == 2
        assert stats['realized_pnl'] == pytest.approx(-0.27)


def test_audit_hist_replaces_same_deal_id_on_funds_upgrade():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        first = sma.on_close(
            store, pnl=-0.81, direction='BUY', entry=4188.0,
            source='live', deal_id='deal-dup', apply_ohlc_penalties=False)
        assert first.get('audited')
        second = sma.on_close(
            store, pnl=-0.89, direction='BUY', entry=4188.0,
            source='live_funds_mirror', deal_id='deal-dup',
            apply_ohlc_penalties=False)
        assert second.get('audited')
        hist = [h for h in (store.get(sma.HIST_KEY) or []) if isinstance(h, dict)]
        same = [h for h in hist if str(h.get('deal_id') or '') == 'deal-dup']
        assert len(same) == 1
        assert same[0]['pnl'] == pytest.approx(-0.89)
        assert same[0].get('unit') == 'account_currency'
        # Scenario memory must count the trade once (not −0.81 then −0.89).
        assert second.get('memory', {}).get('undone_prior') is True
        bag = store.get(sma.SCENARIOS_KEY) or {}
        fp = same[0].get('fingerprint')
        row = bag.get(fp) if isinstance(bag.get(fp), dict) else {}
        assert int(row.get('seen') or 0) == 1
        assert int(row.get('mistake_hits') or 0) <= 1


def test_working_pnl_prefers_24h_capital_window():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        now = datetime.now(timezone.utc)
        # Old close outside 24h — must not dominate Desk vs Funds today.
        store.upsert_trade({
            'dealId': 'old', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 4000,
        })
        store.close_trade(
            'old', 3990, -12.0, currency='GBP',
            source='broker', overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            ((now - timedelta(hours=48)).isoformat(), 'old'))
        store.upsert_trade({
            'dealId': 'today', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 4188,
        })
        store.close_trade(
            'today', 4187, -0.89, currency='GBP',
            source='broker', overwrite=True)
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?',
            ((now - timedelta(minutes=5)).isoformat(), 'today'))
        store.db.commit()
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['realized_pnl'] == pytest.approx(-12.89)
        assert stats['realized_pnl_24h'] == pytest.approx(-0.89)
        assert stats['working_pnl'] == pytest.approx(-0.89)
        assert stats['working_pnl_window'] == '24h'
        assert stats['display_losses'] == 1


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


def test_zero_poison_broker_not_counted_as_capital_cash():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'z0', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 4100,
        })
        store.close_trade(
            'z0', 4100, 0.0, currency='GBP',
            source='broker', overwrite=True)
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['cash_trades'] == 0
        assert stats['working_pnl'] is None
        assert stats['realized_pnl'] is None
        assert stats['pending_cash_trades'] >= 1 or stats['blank_pnl_trades'] >= 1


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
