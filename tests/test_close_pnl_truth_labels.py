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
    # V3 P05/T38: blank / empty is never Capital Funds cash.
    assert RuntimeHub._cash_pnl_source('') == 'unverified'
    assert RuntimeHub._cash_pnl_source(None) == 'unverified'


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
        # NESEN = Funds cash only; estimate listed separately.
        by_src = {t['deal_id']: t['pnl_source'] for t in stats['recent_trades']}
        assert by_src == {'b1': 'broker'}
        assert 'u1' not in by_src
        est = next(t for t in stats['estimate_recent'] if t['deal_id'] == 'u1')
        assert est['pnl'] == pytest.approx(-0.77)
        assert est['pnl_source'] == 'upl_estimate'


def test_fuzzy_bind_disabled_keeps_cash_unmatched():
    """P0-02: epic/size proximity must not invent a deal link."""
    import asyncio
    from app.engine import SingleTradeEngine
    from tests.test_broker_pnl_resolve import HistoryBroker
    from tests.test_learning_integrity import new_brain

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.upsert_trade({
                'dealId': 'g-exact', 'epic': 'GOLD', 'size': '.02',
                'direction': 'SELL', 'level': 3570.0,
            })
            store.close_trade('g-exact', 3572.0, -0.69, currency='GBP',
                              source='upl_estimate', overwrite=True)
            broker.tx = [
                {'reference': 'hit', 'pnl': -0.87, 'currency': 'GBP',
                 'instrumentName': 'Gold', 'size': '0.02',
                 'openLevel': 3570.0, 'closeLevel': 3572.0,
                 'dateUtc': '2026-11-20T14:10:00'},
            ]
            assert await e._pnl_fuzzy_instrument_cash('g-exact') is None
            # Exact dealId on the Funds row still resolves.
            broker.tx = [
                {'dealId': 'g-exact', 'pnl': -0.87, 'currency': 'GBP',
                 'transactionType': 'TRADE'},
            ]
            found = await e._pnl_from_broker_history('g-exact', exact_deal=True)
            assert found is not None and found[0] == pytest.approx(-0.87)
    asyncio.run(scenario())


def test_verified_zero_broker_not_requeued_placeholder_is():
    """P0-08: verified Funds £0 stays settled; unverified/placeholder zero requeues."""
    from app.engine import SingleTradeEngine
    from tests.test_learning_integrity import new_brain

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        # Verified Funds £0.00 — banked broker proof → SETTLED, not pending poison.
        store.upsert_trade({
            'dealId': 'z0', 'epic': 'GOLD', 'size': '.02',
            'direction': 'BUY', 'level': 100,
        })
        store.close_trade('z0', 100.1, 0.0, currency='GBP', source='broker', overwrite=True)
        store.set('broker_cash_banked:z0', {'pnl': 0.0, 'source': 'broker'})
        # Unverified zero (no banked proof) — still harvestable.
        store.upsert_trade({
            'dealId': 'z1', 'epic': 'GOLD', 'size': '.02',
            'direction': 'BUY', 'level': 100,
        })
        store.close_trade('z1', 100.1, 0.0, currency='GBP', source='broker', overwrite=True)
        e._requeue_unsettled_cash_closes()
        pending = store.get('pending_broker_pnl_deals') or []
        ids = {str(x.get('deal_id') if isinstance(x, dict) else x) for x in pending}
        assert 'z0' not in ids
        assert 'z1' in ids
