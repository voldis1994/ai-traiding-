"""Naked robot OPEN: attach SL/TP from open_brackets snapshot immediately."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store


def _engine(d):
    store = Store(Path(d) / 's.db')
    brain = AdaptiveBrain(store, data_dir=d)
    e = SingleTradeEngine(store, brain)
    e.policy = AutoEvolution(store, Path(d) / 'pol.py')
    return store, e


def test_protect_prefers_open_snapshot_without_waiting_for_swing():
    """Capital TP Off after OPEN — Desk must get SL/TP from OPEN distances now."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, e = _engine(d)
            # Thin hist (<20) would previously defer "waiting completed OHLC".
            e.broker = AsyncMock()
            e.broker.update_position = AsyncMock(
                return_value={'dealReference': 'prot-snap'})
            e._confirm_reconcile = AsyncMock(return_value=True)
            e.sync = AsyncMock()
            e.market = {
                'mid': 3560.0, 'bid': 3559.8, 'ask': 3560.2, 'tick': 0.01,
                'min_distance': 0.15, 'spread': 0.4,
            }
            e.mandate = {'epic': 'GOLD', 'size': '0.77'}
            e.pending_order = False
            e.pending_close = False
            deal = 'snap-first-1'
            store.set('robot_open:' + deal, True)
            store.set('open_brackets:' + deal, {
                'stop_distance': 1.2, 'profit_distance': 2.4,
                'direction': 'SELL', 'entry': 3560.0,
            })
            p = {
                'dealId': deal, 'direction': 'SELL', 'level': 3560.0,
                'stopLevel': 0, 'profitLevel': 0, 'epic': 'GOLD',
            }
            e.positions = [p]
            await e.protect_position(p, force=True)
            assert e.broker.update_position.await_count >= 1
            kwargs = e.broker.update_position.await_args.kwargs
            assert kwargs.get('stop_level') is not None
            assert kwargs.get('profit_level') is not None
            # SELL: SL above entry, TP below.
            assert float(kwargs['stop_level']) > 3560.0
            assert float(kwargs['profit_level']) < 3560.0
            msgs = [
                (ev.get('message') or '') + str(ev.get('details') or '')
                for ev in store.recent(40)
            ]
            assert any('OPEN bracket snapshot' in m for m in msgs)
    asyncio.run(scenario())


def test_open_confirm_force_protects_incomplete_brackets():
    """_confirm_reconcile OPEN with TP Off must force-protect same tick."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, e = _engine(d)
            e.broker = AsyncMock()
            e.broker.confirm = AsyncMock(return_value={
                'dealStatus': 'ACCEPTED', 'dealId': 'open-naked',
                'dealReference': 'ref-open', 'level': 100.0,
                'epic': 'GOLD', 'direction': 'BUY', 'size': 0.25,
            })
            e.broker.update_position = AsyncMock(
                return_value={'dealReference': 'prot-open'})
            e.sync = AsyncMock()
            e._order_fully_matched = lambda *a, **k: True
            e.market = {
                'mid': 100.0, 'bid': 99.9, 'ask': 100.1, 'tick': 0.01,
                'min_distance': 0.15, 'spread': 0.2,
            }
            e.mandate = {'epic': 'GOLD', 'size': '0.25'}
            e.positions = [{
                'dealId': 'open-naked', 'direction': 'BUY', 'level': 100.0,
                'stopLevel': 99.5, 'profitLevel': 0, 'epic': 'GOLD', 'size': 0.25,
            }]
            oid = store.order('OPEN', None, {
                'epic': 'GOLD', 'direction': 'BUY', 'size': '0.25',
                'stop_distance': 0.5, 'profit_distance': 1.0,
            })
            ok = await e._confirm_reconcile(
                {'dealReference': 'ref-open'},
                oid, 'OPEN', 'open-naked')
            assert ok is True
            assert store.get('open_brackets:open-naked')
            assert e.broker.update_position.await_count >= 1
            kwargs = e.broker.update_position.await_args.kwargs
            assert kwargs.get('stop_level') is not None
            assert kwargs.get('profit_level') is not None
    asyncio.run(scenario())
