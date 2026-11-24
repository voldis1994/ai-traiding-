"""Robot scale-out at TP1 is long-only; shorts keep broker TP2."""
import asyncio
import tempfile
from pathlib import Path

from app.engine import AdaptiveBrain, SingleTradeEngine, Store


def test_sell_scale_out_skipped_long_only():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.mandate = {'epic': 'SIM', 'size': '1.0'}
            engine.execution_enabled = True
            engine.positions = [{
                'dealId': 'd-short', 'size': '1.0', 'direction': 'SELL',
                'level': 100.0, 'stopLevel': 102.0, 'profitLevel': 94.0,
                'epic': 'SIM', 'upl': 1.0,
            }]
            store.set('entry_brain:d-short', {
                'entry': 100.0, 'direction': 'SELL',
                'tp1_level': 97.0, 'tp2_level': 94.0,
                'scale_out_frac': 0.5, 'scale_out_done': False,
            })
            engine.market = {
                'epic': 'SIM', 'mid': 96.5, 'bid': 96.4, 'ask': 96.6,
                'spread': 0.2, 'tick': 0.01, 'min_distance': 0.15,
                'status': 'TRADEABLE',
            }
            await engine._maybe_scale_out_tp1(engine.positions[0])
            brain_after = store.get('entry_brain:d-short')
            assert brain_after.get('scale_out_skip') == 'scale_out_long_only'
            assert brain_after.get('scale_out_done') is True

    asyncio.run(scenario())
