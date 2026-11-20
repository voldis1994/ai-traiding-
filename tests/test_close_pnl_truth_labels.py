"""CLIENT CLOSES must distinguish Capital £ from provisional ≈UPL."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app.runtime_hub import RuntimeHub


def test_cash_pnl_source_classifies_upl_as_estimate():
    assert RuntimeHub._cash_pnl_source('broker') == 'cash'
    assert RuntimeHub._cash_pnl_source('upl_estimate') == 'estimate'
    assert RuntimeHub._cash_pnl_source('pending_broker_pnl') == 'pending'
    assert RuntimeHub._cash_pnl_source('ticks_only') == 'proxy'


def test_mixed_broker_and_upl_keeps_sources_honest():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'b1', 'epic': 'GOLD', 'size': '0.02',
            'direction': 'SELL', 'level': 3570,
        })
        store.close_trade('b1', 3568, 0.14, currency='GBP', source='broker')
        store.upsert_trade({
            'dealId': 'u1', 'epic': 'GOLD', 'size': '0.02',
            'direction': 'SELL', 'level': 3572,
        })
        store.close_trade('u1', 3574, -0.77, currency='GBP', source='upl_estimate')
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['cash_trades'] == 1
        assert stats['estimate_trades'] == 1
        assert stats['realized_pnl'] == pytest.approx(0.14)
        assert stats['working_pnl'] == pytest.approx(0.14)
        assert stats['working_pnl_source'] == 'broker'
        assert stats['pnl_status'] == 'broker_cash_partial'
        by_src = {t['deal_id']: t['pnl_source'] for t in stats['recent_trades']}
        assert by_src['b1'] == 'broker'
        assert by_src['u1'] == 'upl_estimate'
        # Admin Capital mismatch: UPL figure must stay visible on the estimate row.
        est = next(t for t in stats['recent_trades'] if t['deal_id'] == 'u1')
        assert est['pnl'] == pytest.approx(-0.77)
