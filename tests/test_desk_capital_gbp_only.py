"""Desk shows Capital Funds £ only — never ≈UPL / pending / pts invent."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import Store
from app.runtime_hub import RuntimeHub


def test_trade_stats_recent_is_broker_gbp_only():
    with tempfile.TemporaryDirectory() as d:
        from app import cash_ledger
        store = Store(Path(d) / 'db.sqlite3')
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        now = datetime.now(timezone.utc)
        # Settled Funds £
        store.upsert_trade({
            'dealId': 'b1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            'b1', 3601, -0.89, currency='GBP', source='broker', overwrite=True)
        cash_ledger.ingest_funds_row(
            store, {'pnl': -0.89, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='b1', transaction_id='TX-b1')
        # Provisional ≈UPL — must not appear in recent_trades / ledger / working
        store.upsert_trade({
            'dealId': 'u1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'SELL', 'level': 3600,
        })
        store.close_trade(
            'u1', 3599, -0.41, currency='GBP', source='upl_estimate',
            overwrite=True)
        # Pending blank
        store.upsert_trade({
            'dealId': 'p1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade(
            'p1', 3602, None, currency='GBP', source='pending_broker_pnl',
            overwrite=True)
        for deal, hours in (('b1', 1), ('u1', 2), ('p1', 3)):
            store.db.execute(
                'UPDATE trades SET closed=? WHERE deal_id=?',
                ((now - timedelta(hours=hours)).isoformat(), deal))
        store.db.commit()
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP', open_upl=0.31)
        assert stats['cash_trades'] == 1
        assert stats['working_pnl'] == pytest.approx(-0.89)
        assert stats['working_pnl_source'] == 'broker'
        assert stats['recent_trades']
        assert all(
            str(t.get('pnl_source') or '') == 'broker'
            for t in stats['recent_trades'])
        assert all(
            str(row.get('source') or '') == 'broker'
            for row in stats['pnl_ledger'])
        # Open UPL kept for position cards but not equity invent.
        assert stats['open_upl'] == pytest.approx(0.31)
        assert stats['equity_pnl'] == pytest.approx(-0.89)


def test_estimate_not_treated_as_nesen_cash_totals():
    """NESEN / working_pnl stay Funds-only; estimates are listed separately.

    V3 P05–P13: never fill recent_trades from upl_estimate as if Capital £.
    """
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'e1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 4188,
        })
        store.close_trade(
            'e1', 4187, -0.89, currency='GBP',
            source='upl_estimate', overwrite=True)
        closed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        store.db.execute(
            'UPDATE trades SET closed=? WHERE deal_id=?', (closed, 'e1'))
        store.db.commit()
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP', open_upl=0.31)
        assert stats['estimate_trades'] >= 1
        assert stats['working_pnl'] is None
        assert stats['working_pnl_source'] in ('none', 'empty')
        assert stats['cash_trades'] == 0
        assert stats['recent_trades'] == []
        assert stats['pnl_ledger'] == []
        assert len(stats['estimate_recent']) == 1
        assert stats['estimate_recent'][0]['pnl'] == pytest.approx(-0.89)
        assert stats['estimate_recent'][0]['pnl_source'] == 'upl_estimate'
