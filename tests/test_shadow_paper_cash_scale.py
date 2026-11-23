"""Paper book must not show £0 while price SL/TP closes already exist."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app.shadow_paper import ShadowPaperJournal, cash_per_price_unit


def test_summary_shows_price_closes_when_cash_scale_missing():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.history = [
            {'direction': 'BUY', 'entry': 3680.49, 'exit': 3667.55,
             'price_pnl': -12.94, 'paper_pnl': None, 'unit': 'price',
             'exit_reason': 'sl'},
            {'direction': 'BUY', 'entry': 3660.0, 'exit': 3670.0,
             'price_pnl': 10.0, 'paper_pnl': None, 'unit': 'price',
             'exit_reason': 'tp'},
        ]
        s = j.summary()
        assert s['trades'] == 2
        assert s['cash_trades'] == 0
        assert s['price_trades'] == 2
        assert s['display_trades'] == 2
        assert s['unit'] == 'price'
        assert s['display_net'] == pytest.approx(-2.94)
        assert s['wins'] == 1
        assert s['losses'] == 1


def test_update_cash_scale_revalues_price_rows_from_store_trades():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for i, pnl in enumerate((-0.33, -0.98, 0.70, -0.27, -0.18)):
            deal = f'd{i}'
            store.upsert_trade({
                'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 3600 + i,
            })
            store.close_trade(
                deal, 3600 + i + (pnl / 0.1), pnl, currency='GBP',
                source='broker', overwrite=True)
        j = ShadowPaperJournal(store)
        j.history = [
            {'direction': 'BUY', 'entry': 3680.0, 'exit': 3667.0,
             'price_pnl': -13.0, 'paper_pnl': None, 'unit': 'price',
             'exit_reason': 'sl'},
        ]
        # Empty profit_learning hist → must fall back to store broker trades.
        scale = j.update_cash_scale([], min_samples=3)
        assert scale is not None and abs(scale) > 0
        s = j.summary()
        assert s['cash_trades'] == 1
        assert s['unit'] == 'account_currency'
        assert s['display_trades'] == 1
        assert s['paper_net'] != 0
        assert j.history[0]['unit'] == 'account_currency'
        assert j.history[0]['paper_pnl'] is not None


def test_cash_per_price_unit_accepts_pnl_alias_and_skips_zero():
    rows = [
        {'actual_pnl': 0.0, 'entry': 3600, 'exit': 3590, 'direction': 'BUY'},
        {'pnl': -0.5, 'entry': 3600, 'exit': 3590, 'direction': 'BUY'},
        {'pnl': -0.4, 'entry': 3610, 'exit': 3600, 'direction': 'BUY'},
        {'pnl': 0.3, 'entry': 3600, 'exit': 3610, 'direction': 'BUY'},
    ]
    scale = cash_per_price_unit(rows, min_samples=3)
    assert scale is not None
    # Zero-poison row must not enter the median pool.
    assert abs(scale - 0.0) > 1e-9
