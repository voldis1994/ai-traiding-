"""Admin must not hide 13 closes behind a silent £0 when broker cash never landed."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app.runtime_hub import RuntimeHub


def test_trade_stats_flags_closed_without_broker_cash():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for i in range(13):
            store.upsert_trade({
                'dealId': f'd{i}', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 2000,
            })
            # Closed in journal but Capital £ never bound.
            store.close_trade(f'd{i}', 1999, source='pending_broker_pnl', overwrite=True)
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['closed_trades'] == 13
        assert stats['cash_trades'] == 0
        assert stats['pending_cash_trades'] == 13
        assert stats['realized_pnl'] is None
        assert stats['pnl_status'] == 'closed_without_broker_cash'


def test_trade_stats_broker_cash_ok():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'ok1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 2000,
        })
        store.close_trade('ok1', 2001, 0.17, currency='GBP', source='broker')
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['cash_trades'] == 1
        assert stats['realized_pnl'] == 0.17
        assert stats['pnl_status'] == 'broker_cash_ok'


def test_upl_estimate_counts_as_working_not_broker_cash():
    """UPL shows in working P&L but is NOT Capital cash (Admin honesty)."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'u1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'SELL', 'level': 2000,
        })
        store.close_trade('u1', 1998, -0.09, currency='GBP', source='upl_estimate')
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['cash_trades'] == 0
        assert stats['estimate_trades'] == 1
        assert stats['realized_pnl'] is None
        assert stats['pnl_status'] == 'upl_estimate_pending'
        assert stats['working_pnl'] == pytest.approx(-0.09)
        assert stats['working_pnl_source'] == 'upl_estimate'
        assert stats['recent_trades'][0]['pnl_source'] == 'upl_estimate'
        assert stats['display_wins'] == 0
        assert stats['display_losses'] == 1


def test_working_pnl_and_wl_from_mixed_upl_peļņa():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for i, pnl in enumerate([0.28, -0.57, 0.33, -0.32]):
            store.upsert_trade({
                'dealId': f'e{i}', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 2000,
            })
            store.close_trade(f'e{i}', 2001, pnl, currency='GBP', source='upl_estimate')
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['cash_trades'] == 0
        assert stats['estimate_trades'] == 4
        assert stats['working_pnl'] == pytest.approx(-0.28)
        assert stats['working_pnl_source'] == 'upl_estimate'
        assert stats['realized_pnl'] is None
        assert stats['display_wins'] == 2
        assert stats['display_losses'] == 2
        assert stats['wins'] == 0
        ledger = stats['pnl_ledger']
        assert len(ledger) == 4
        assert all(row['source'] == 'upl_estimate' for row in ledger)
        assert sum(row['pnl'] for row in ledger) == pytest.approx(-0.28)
