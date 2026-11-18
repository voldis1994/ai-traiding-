"""Komandcentrs live refresh: open UPL + mid must surface when cash book is empty."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.engine import Store
from app.runtime_hub import RuntimeHub


def test_trade_stats_no_closes_keeps_working_pnl_none_and_open_upl():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP', open_upl=0.24)
        assert stats['closed_trades'] == 0
        assert stats['working_pnl'] is None
        assert stats['working_pnl_source'] == 'empty'
        assert stats['open_upl'] == pytest.approx(0.24)
        assert stats['equity_pnl'] == pytest.approx(0.24)
        assert stats['realized_pnl'] is None


def test_admin_overview_surfaces_open_upl_when_no_cash():
    hub = RuntimeHub.__new__(RuntimeHub)
    hub.platform = MagicMock()
    hub.platform.list_clients.return_value = [
        {'id': 1, 'username': 'guntis', 'display_name': 'guntis'},
        {'id': 2, 'username': 'voldis', 'display_name': 'voldis'},
    ]

    def card(client):
        cid = int(client['id'])
        upl = 0.45 if cid == 1 else -0.21
        return {
            **client,
            'robot_running': True,
            'account_currency': 'GBP',
            'results': {
                'closed_trades': 0,
                'cash_trades': 0,
                'pending_cash_trades': 0,
                'proxy_trades': 0,
                'estimate_trades': 0,
                'realized_pnl': None,
                'working_pnl': None,
                'working_pnl_source': 'empty',
                'open_upl': upl,
                'pnl_currency': 'GBP',
                'pnl_by_currency': {},
                'display_wins': 0,
                'display_losses': 0,
            },
            'activity': {
                'positions': [{
                    'dealId': f'd{cid}', 'epic': 'GOLD', 'direction': 'BUY',
                    'size': 0.1, 'level': 3790., 'upl': upl, 'mid': 3791.2,
                    'currentLevel': 3791.2,
                }],
            },
        }

    hub.admin_client_card = card
    out = hub.admin_overview()
    s = out['summary']
    assert s['open_trades_all'] == 2
    assert s['open_upl_all'] == pytest.approx(0.24)
    assert s['working_pnl_all'] is None
    assert s['working_pnl_source'] == 'open_upl'
    assert s['pnl_currency'] == 'GBP'
