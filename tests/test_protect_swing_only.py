"""Protect must wait for OHLC swing SL/TP — never invent ATR brackets."""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_tp_min_distance import _OpenCaptureBroker


def test_protect_defers_without_swing_no_atr_invent():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            broker.open = [{'position': {
                'dealId': 'noswing', 'size': '.1', 'direction': 'BUY',
                'level': 100.0, 'stopLevel': None, 'profitLevel': None, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]

            async def _update(deal_id, stop_level=None, profit_level=None):
                broker.requests.append(('PROTECT', deal_id, stop_level, profit_level))
                return {'dealReference': 'x'}

            broker.update_position = _update
            engine.attach(broker)
            await engine.sync()
            # Too few candles → no pivots → must defer, no PUT.
            engine.market = {
                'epic': 'SIM', 'mid': 100.0, 'bid': 99.9, 'ask': 100.1,
                'spread': 0.2, 'tick': 0.01, 'min_distance': 0.15, 'status': 'TRADEABLE',
            }
            await engine.protect_position(engine.positions[0])
            assert not [r for r in broker.requests if r[0] == 'PROTECT']
            brief = str((engine.protection_brief or {}).get('status') or '')
            assert ('swing' in brief.lower() or 'ohlc' in brief.lower()
                    or 'waiting' in brief.lower())

    asyncio.run(scenario())


def test_trail_flag_still_off():
    assert SingleTradeEngine.TRAIL_ENABLED is False


def test_propose_for_open_passes_store_min_rr(monkeypatch):
    """Protect path must use learned bracket_min_rr via store (not only OPEN)."""
    from app import bracket_learn
    from app.self_params import DEFAULTS, save

    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        save(store, {**DEFAULTS, 'bracket_min_rr': 2.0}, reason='audit')
        seen = {}

        def _capture(*args, **kwargs):
            seen['store'] = kwargs.get('store')
            seen['require_swing'] = kwargs.get('require_swing')
            return {
                'ready': False, 'reason': 'no_swings', 'source': 'swing_anchor',
                'kappa': 1.0, 'ohlc_kappa': 1.0, 'ohlc_sl': None, 'ohlc_tp': None,
            }

        monkeypatch.setattr(bracket_learn, 'decide_brackets_from_ohlc', _capture)
        bracket_learn.propose_for_open(
            store, candles=[{'high': 1, 'low': 1, 'close': 1}] * 30,
            tick=0.01, entry=100.0, action='BUY', require_swing=True)
        assert seen.get('store') is store
        assert seen.get('require_swing') is True
