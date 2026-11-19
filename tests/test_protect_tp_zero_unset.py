"""Capital Take Profit Off often arrives as profitLevel=0, not null.

protect_position used to treat 0 as 'already protected' and skip attaching
OHLC TP — Admin then showed poz. TP 0 and the live deal had no take-profit.
"""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store, _broker_price_level
from tests.test_tp_min_distance import _OpenCaptureBroker


def test_broker_price_level_treats_zero_as_unset():
    assert _broker_price_level(0) is None
    assert _broker_price_level(0.0) is None
    assert _broker_price_level('0') is None
    assert _broker_price_level(None) is None
    assert _broker_price_level(-1) is None
    assert _broker_price_level(3720.5) == 3720.5


def test_positions_normalize_profit_level_zero_to_none():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            broker = _OpenCaptureBroker()
            broker.open = [{'position': {
                'dealId': 'z0', 'size': '.1', 'direction': 'BUY', 'level': 3720.0,
                'stopLevel': 3710.0, 'profitLevel': 0, 'upl': 0,
            }, 'market': {'epic': 'GOLD'}}]
            engine.attach(broker)
            await engine.sync()
            pos = engine.positions[0]
            assert pos['stopLevel'] == 3710.0
            assert pos['profitLevel'] is None

    asyncio.run(scenario())


def test_protect_attaches_tp_when_broker_shows_profit_level_zero():
    """SL already on — robot must still PUT OHLC TP (0 must not skip protect)."""

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            broker.open = [{'position': {
                'dealId': 'm-tp0', 'size': '.25', 'direction': 'BUY', 'level': 100.0,
                'stopLevel': 99.2, 'profitLevel': 0, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]
            engine.attach(broker)
            await engine.sync()
            from tests.test_integration import seed_swing_candles
            seed_swing_candles(store, 'SIM')
            engine.market = {
                'epic': 'SIM', 'mid': 100.0, 'bid': 99.99, 'ask': 100.02,
                'spread': 0.03, 'tick': 0.01, 'min_distance': 0.15, 'status': 'TRADEABLE',
            }
            assert engine.positions[0]['profitLevel'] is None
            await engine.protect_position(engine.positions[0])
            protects = [r for r in broker.requests if r[0] == 'PROTECT']
            assert protects, 'expected PROTECT PUT to attach missing TP'
            _, deal_id, stop, profit = protects[0]
            assert deal_id == 'm-tp0'
            # SL already live — only TP should be sent.
            assert stop is None
            assert profit is not None and float(profit) > 100.0

    asyncio.run(scenario())


def test_admin_truth_zero_profit_is_not_has_tp():
    from app.runtime_hub import AccountRuntime

    class _E:
        store = type('S', (), {'get': staticmethod(lambda *a, **k: None)})()
        positions = []
        protection_brief = {}

    p = {
        'dealId': 'd0', 'direction': 'BUY', 'size': '0.1', 'level': 3720.,
        'stopLevel': 3710., 'profitLevel': 0, 'epic': 'GOLD',
    }
    t = AccountRuntime._deal_protection_truth(_E(), p)
    assert t['has_sl'] is True
    assert t['has_tp'] is False
