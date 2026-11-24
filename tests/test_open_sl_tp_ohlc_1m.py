"""LIVE OPEN SL/TP must come from completed M1 OHLC — never invent κ×min."""
import asyncio
import inspect
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_tp_min_distance import _OpenCaptureBroker


def test_submit_requires_swing_no_neural_min_fallback():
    src = inspect.getsource(SingleTradeEngine.submit)
    assert 'require_swing=True' in src
    assert 'neural_min_fallback' not in src
    assert 'fallback_no_swing' not in src
    assert 'need OHLC 1m SL/TP' in src or 'wait_ohlc_swing' in src


def test_submit_refuses_open_without_ohlc_swing_brackets():
    """Thin tape → no POST; never invent broker-min × κ SL/TP."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            brain.core.steps = 50
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            engine.last_closed_step = 0
            engine.last_entry_signal_step = 0
            engine.armed = True
            engine.execution_enabled = True
            engine.mandate = {'epic': 'GOLD', 'size': '0.1'}
            engine.market_context = {
                'ready': True, 'close': 100.0, 'bar_time': '2026-11-24T12:00:00+00:00',
                'features': [0.0] * 8, 'trend': 'up',
            }
            engine.last_signal = {
                'action': 'BUY', 'scores': {'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1},
                'scores_raw': {'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1},
            }
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            engine.attach(broker)
            engine.market = {
                'epic': 'GOLD', 'mid': 100.0, 'bid': 99.9, 'ask': 100.1,
                'spread': 0.2, 'tick': 0.01, 'min_distance': 0.15,
                'status': 'TRADEABLE',
            }
            # No / few candles → swing/regime cannot place OHLC levels.
            await engine.submit('BUY', {
                'snapshot': {'bid': 99.9, 'offer': 100.1, 'marketStatus': 'TRADEABLE'},
                'instrument': {'epic': 'GOLD', 'name': 'Gold'},
            })
            opens = [r for r in broker.requests if r[0] == 'OPEN']
            assert opens == [], f'OPEN must wait for OHLC 1m SL/TP, got {opens}'
            reason = str(engine.last_entry_reason or '').lower()
            assert ('ohlc' in reason or 'swing' in reason or 'wait' in reason
                    or reason == ''), reason

    asyncio.run(scenario())
