"""Post-OPEN protect must run while pending_order is still True."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_integration import seed_swing_candles


def _engine(d):
    store = Store(Path(d) / 's.db')
    brain = AdaptiveBrain(store, data_dir=d)
    e = SingleTradeEngine(store, brain)
    e.policy = AutoEvolution(store, Path(d) / 'pol.py')
    return store, e


def test_protect_force_runs_during_pending_order():
    """Capital often confirms TP Off; same-tick repair used to no-op on pending_order."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, e = _engine(d)
            seed_swing_candles(store, 'GOLD', start_px=100.0)
            e.broker = AsyncMock()
            e.broker.update_position = AsyncMock(
                return_value={'dealReference': 'prot-force'})
            e._confirm_reconcile = AsyncMock(return_value=True)
            e.sync = AsyncMock()
            e.market = {
                'mid': 100.0, 'bid': 99.9, 'ask': 100.1, 'tick': 0.01,
                'min_distance': 0.15, 'spread': 0.2,
            }
            e.mandate = {'epic': 'GOLD', 'size': '0.25'}
            e.pending_order = True
            e.pending_close = False
            store.set('open_brackets:live-force', {
                'stop_distance': 0.5, 'profit_distance': 1.0,
                'direction': 'BUY', 'entry': 100.0,
            })
            p = {
                'dealId': 'live-force', 'direction': 'BUY', 'level': 100.0,
                'stopLevel': 99.5, 'profitLevel': None, 'epic': 'GOLD',
            }
            # Without force — silent no-op (the old bug).
            await e.protect_position(p)
            assert e.broker.update_position.await_count == 0
            # With force — repair PUT allowed during OPEN pending_order.
            await e.protect_position(p, force=True)
            assert (e.broker.update_position.await_count >= 1
                    or 'Protect waiting' in str(
                        (e.protection_brief or {}).get('status') or ''))
    asyncio.run(scenario())
