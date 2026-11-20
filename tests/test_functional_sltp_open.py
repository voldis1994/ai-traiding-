"""Every long/short must carry working SL+TP — not UI cosmetics."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from app.autonomy import AutoEvolution
from app.broker import CapitalBroker
from app.engine import AdaptiveBrain, SingleTradeEngine, Store, _broker_price_level
from app.runtime_hub import AccountRuntime


def _engine(d):
    store = Store(Path(d) / 's.db')
    brain = AdaptiveBrain(store, data_dir=d)
    e = SingleTradeEngine(store, brain)
    e.policy = AutoEvolution(store, Path(d) / 'pol.py')
    return store, e


def test_broker_open_requires_both_distances():
    async def scenario():
        b = CapitalBroker('k', 'u', 'p', client=AsyncMock())
        try:
            await b.open_position('GOLD', 'BUY', 0.25, stop_distance=None, profit_distance=0.4)
            raise AssertionError('naked OPEN allowed')
        except ValueError as exc:
            assert 'stopDistance' in str(exc) or 'both' in str(exc).lower()
        try:
            await b.update_position('d1', stop_level=99.0, profit_level=None)
            raise AssertionError('TP-only-clear PUT allowed')
        except ValueError as exc:
            assert 'both' in str(exc).lower() or 'profitLevel' in str(exc)
    asyncio.run(scenario())


def test_needs_protect_when_tp_off_even_with_sl():
    with tempfile.TemporaryDirectory() as d:
        store, e = _engine(d)
        p = {
            'dealId': 't1', 'direction': 'BUY', 'level': 100.0,
            'stopLevel': 99.0, 'profitLevel': 0,  # Capital TP Off
            'epic': 'GOLD',
        }
        assert _broker_price_level(0) is None
        assert e._needs_protect_brackets(p) is True


def test_needs_protect_when_sides_swapped():
    with tempfile.TemporaryDirectory() as d:
        store, e = _engine(d)
        p = {
            'dealId': 't2', 'direction': 'BUY', 'level': 100.0,
            'stopLevel': 101.5, 'profitLevel': 98.5,  # swapped
            'epic': 'GOLD',
        }
        assert e._needs_protect_brackets(p) is True


def test_unresolved_open_does_not_block_protect_of_live_deal():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            from tests.test_integration import seed_swing_candles
            store, e = _engine(d)
            seed_swing_candles(store, 'GOLD', start_px=100.0)
            e.broker = AsyncMock()
            e.broker.update_position = AsyncMock(
                return_value={'dealReference': 'prot'})
            e._confirm_reconcile = AsyncMock(return_value=True)
            e.sync = AsyncMock()
            e.market = {
                'mid': 100.0, 'bid': 99.9, 'ask': 100.1, 'tick': 0.01,
                'min_distance': 0.15, 'spread': 0.2,
            }
            e.mandate = {'epic': 'GOLD', 'size': '0.25'}
            e.pending_order = False
            e.pending_close = False
            # Stale UNCERTAIN OPEN in journal — previously froze protect forever.
            oid = store.order('OPEN', None, {
                'epic': 'GOLD', 'direction': 'BUY', 'size': '0.25',
                'stop_distance': '0.5', 'profit_distance': '1.0',
            })
            store.order_update(oid, 'UNCERTAIN', 'ref-x')
            assert store.unresolved()
            store.set('open_brackets:live1', {
                'stop_distance': 0.5, 'profit_distance': 1.0,
                'direction': 'BUY', 'entry': 100.0,
            })
            p = {
                'dealId': 'live1', 'direction': 'BUY', 'level': 100.0,
                'stopLevel': 99.5, 'profitLevel': None, 'epic': 'GOLD',
            }
            await e.protect_position(p)
            # Must attempt PUT (or defer for swing) — not hard-stop on OPEN journal.
            reasons = [
                (ev.get('message') or '') + str(ev.get('details') or '')
                for ev in store.recent(80)
            ]
            assert not any('OPEN still unresolved' in r for r in reasons)
            assert (e.broker.update_position.await_count >= 1
                    or 'Protect waiting' in str((e.protection_brief or {}).get('status') or ''))
    asyncio.run(scenario())


def test_trail_off_truth_does_not_advertise_armed():
    with tempfile.TemporaryDirectory() as d:
        store, e = _engine(d)
        assert e.TRAIL_ENABLED is False
        store.set('robot_open:d1', True)
        store.set('adaptive_protection:d1', {'owner': 'AUTO', 'original_risk': 1.0})
        store.set('adaptive_runtime:d1', {'status': 'CONFIRMED', 'phase': 'trail'})
        p = {
            'dealId': 'd1', 'direction': 'BUY', 'level': 100.0,
            'stopLevel': 99.0, 'profitLevel': 101.0,
        }
        truth = AccountRuntime._deal_protection_truth(e, p, brief={
            'status': 'Confirmed trail', 'deal_id': 'd1'})
        assert truth['has_sl'] is True and truth['has_tp'] is True
        assert truth['trail_phase'] == 'flat'
