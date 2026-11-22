"""P0 trading-logic audit follow-ups: harvest race, £0 settle, trail CF, protect sides."""
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


def test_counterfactual_trail_follows_trail_enabled():
    """Trail hard OFF — CF path never simulates trail even if class flipped."""
    with tempfile.TemporaryDirectory() as d:
        store, e = _engine(d)
        assert e.TRAIL_ENABLED is False
        assert e._trail_is_enabled() is False
        ctx = e._counterfactual_context(
            'd1', {'stop_distance': 1.0, 'profit_distance': 2.0},
            epic='GOLD', entry=100.0, exit_level=100.5, tick=0.01, spread=0.02)
        assert ctx['trail'] is False
        e.TRAIL_ENABLED = True
        ctx2 = e._counterfactual_context(
            'd1', {'stop_distance': 1.0, 'profit_distance': 2.0},
            epic='GOLD', entry=100.0, exit_level=100.5, tick=0.01, spread=0.02)
        assert ctx2['trail'] is False
        assert e._trail_is_enabled() is False


def test_history_settle_rejects_zero_pnl():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, e = _engine(d)
            e.broker = AsyncMock()
            e.positions = [{
                'dealId': 'live1', 'direction': 'BUY', 'level': 100.0,
                'size': '0.25', 'epic': 'GOLD',
            }]
            e._open_deal_ids = {'live1'}
            e._pnl_from_broker_history = AsyncMock(return_value=(0.0, 'GBP'))
            ok = await e._settle_deal_from_history_cash('live1')
            assert ok is False
            assert len(e.positions) == 1
            assert e.positions[0]['dealId'] == 'live1'
    asyncio.run(scenario())


def test_retry_pending_skips_teach_while_pending_close():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, e = _engine(d)
            e.broker = AsyncMock()
            e.pending_close = True
            store.set('pending_broker_pnl_deals', [{
                'deal_id': 'c1', 'attempts': 0, 'exit_level': 100.2,
            }])
            store.close_trade('c1', 100.2, source='pending_close_sync')
            e._last_upl_as_realized = lambda deal: (1.25, 'GBP')
            taught = []
            e._learn_closed_trade = lambda deal, conf: taught.append((deal, conf))
            await e._retry_pending_broker_pnl()
            assert taught == []
            pending = store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id')) == 'c1' for x in pending)
    asyncio.run(scenario())


def test_protect_refuses_unknown_direction():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, e = _engine(d)
            e.broker = AsyncMock()
            e.market = {
                'mid': 100.0, 'bid': 99.9, 'ask': 100.1, 'tick': 0.01,
                'min_distance': 0.15, 'spread': 0.2,
            }
            e.pending_order = False
            e.pending_close = False
            p = {
                'dealId': 'x1', 'direction': None, 'level': 100.0,
                'stopLevel': None, 'profitLevel': None, 'epic': 'GOLD',
            }
            await e.protect_position(p)
            assert 'unknown direction' in str(
                (e.protection_brief or {}).get('reason') or
                (e.protection_brief or {}).get('status') or '')
            e.broker.update_position.assert_not_called()
    asyncio.run(scenario())


def test_protect_ignores_wrong_side_live_sl():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            from tests.test_integration import seed_swing_candles
            store, e = _engine(d)
            seed_swing_candles(store, 'GOLD', start_px=100.0)
            e.broker = AsyncMock()
            e.broker.update_position = AsyncMock(
                return_value={'dealReference': 'prot'})
            e._confirm_reconcile = AsyncMock(return_value=True)
            e.market = {
                'mid': 100.0, 'bid': 99.9, 'ask': 100.1, 'tick': 0.01,
                'min_distance': 0.15, 'spread': 0.2,
            }
            e.pending_order = False
            e.pending_close = False
            e.mandate = {'epic': 'GOLD', 'size': '0.25'}
            # BUY with SL above entry (wrong side) and no TP — must rebuild.
            p = {
                'dealId': 'w1', 'direction': 'BUY', 'level': 100.0,
                'stopLevel': 101.5, 'profitLevel': None, 'epic': 'GOLD',
            }
            await e.protect_position(p)
            if e.broker.update_position.await_count:
                kwargs = e.broker.update_position.await_args.kwargs
                assert kwargs.get('stop_level') is None or kwargs['stop_level'] < 100.0
                if kwargs.get('profit_level') is not None:
                    assert kwargs['profit_level'] > 100.0
            else:
                # Deferred swing is OK — must not have kept wrong-side as done.
                assert not e.store.get('tp_attach_done:w1')
    asyncio.run(scenario())
