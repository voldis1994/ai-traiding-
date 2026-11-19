"""Failed/pending protect must not re-PUT every 3s poll (Capital SL/TP jerk).

Trail already cools REJECTED for 15s; protect_position had no equivalent, so a
single LV/manual account with Capital rejecting TP hammered update_position up
to 3× per poll while peers with attached TP stayed quiet.
"""
import asyncio
import tempfile
import time
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_tp_min_distance import _OpenCaptureBroker


def _engine(d):
    store = Store(Path(d) / 's.db')
    brain = AdaptiveBrain(store, data_dir=d)
    engine = SingleTradeEngine(store, brain)
    engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
    return engine, store


def test_protect_reject_backs_off_before_next_put():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            engine, store = _engine(d)
            broker = _OpenCaptureBroker()
            broker.price = 3714.0
            broker.open = [{'position': {
                'dealId': 'andris-hammer', 'size': '.08', 'direction': 'BUY',
                'level': 3714.61, 'stopLevel': 3705.69, 'profitLevel': 0, 'upl': -0.11,
            }, 'market': {'epic': 'GOLD'}}]

            async def _confirm(ref):
                return {'dealStatus': 'REJECTED', 'dealId': 'andris-hammer',
                        'reason': 'ATTACH_ERROR'}

            async def _update(deal_id, stop_level=None, profit_level=None):
                broker.requests.append(('PROTECT', deal_id, stop_level, profit_level))
                return {'dealReference': 'protect-rej'}

            broker.confirm = _confirm
            broker.update_position = _update
            engine.attach(broker)
            await engine.sync()
            from tests.test_integration import seed_swing_candles
            seed_swing_candles(store, 'GOLD')
            engine.market = {
                'epic': 'GOLD', 'mid': 3713.28, 'bid': 3713.0, 'ask': 3713.5,
                'spread': 0.5, 'tick': 0.01, 'min_distance': 0.5, 'status': 'TRADEABLE',
            }
            pos = engine.positions[0]
            await engine.protect_position(pos)
            assert len([r for r in broker.requests if r[0] == 'PROTECT']) == 1
            rt = store.get('protect_runtime:andris-hammer')
            assert rt and rt.get('status') == 'REJECTED'
            assert float(rt.get('wait_sec') or 0) >= 15.0
            # Same poll / next 3s tick must not hammer.
            await engine.protect_position(pos)
            await engine.protect_position(pos)
            assert len([r for r in broker.requests if r[0] == 'PROTECT']) == 1
            # After cool-down, one more attempt is allowed.
            rt['mono'] = time.monotonic() - 20
            store.set('protect_runtime:andris-hammer', rt)
            await engine.protect_position(pos)
            assert len([r for r in broker.requests if r[0] == 'PROTECT']) == 2

    asyncio.run(scenario())


def test_in_flight_blocks_same_poll_triple_protect():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            engine, store = _engine(d)
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            broker.open = [{'position': {
                'dealId': 'triple', 'size': '.08', 'direction': 'SELL',
                'level': 100.0, 'stopLevel': 100.8, 'profitLevel': 0, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]
            gate = {'n': 0}

            async def _confirm(ref):
                # Stay pending / reject on first so TP remains Off.
                gate['n'] += 1
                return {'dealStatus': 'REJECTED', 'dealId': 'triple'}

            async def _update(deal_id, stop_level=None, profit_level=None):
                broker.requests.append(('PROTECT', deal_id, stop_level, profit_level))
                return {'dealReference': 'protect-triple'}

            broker.confirm = _confirm
            broker.update_position = _update
            engine.attach(broker)
            await engine.sync()
            from tests.test_integration import seed_swing_candles
            seed_swing_candles(store, 'SIM')
            engine.market = {
                'epic': 'SIM', 'mid': 100.0, 'bid': 99.98, 'ask': 100.01,
                'spread': 0.03, 'tick': 0.01, 'min_distance': 0.15, 'status': 'TRADEABLE',
            }
            # Simulate poll's early + after-candles + mid-manage calls.
            for _ in range(3):
                if engine._needs_protect_brackets(engine.positions[0]):
                    await engine.protect_position(engine.positions[0])
            assert len([r for r in broker.requests if r[0] == 'PROTECT']) == 1

    asyncio.run(scenario())


def test_tp_attach_flap_does_not_clear_during_backoff():
    with tempfile.TemporaryDirectory() as d:
        engine, store = _engine(d)
        store.set('tp_attach_done:flap1', True)
        store.set('protect_runtime:flap1', {
            'mono': time.monotonic(), 'status': 'REJECTED', 'wait_sec': 15.0,
        })
        p = {
            'dealId': 'flap1', 'direction': 'BUY', 'level': 100.,
            'stopLevel': 99.0, 'profitLevel': None, 'epic': 'SIM', 'size': '1',
        }
        assert engine._needs_protect_brackets(p) is True
        # Flag stays until cool-down ends — avoids clear→PUT→reject→clear loop.
        assert store.get('tp_attach_done:flap1') is True
