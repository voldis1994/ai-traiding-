"""First manual Capital trade often has SL but Take Profit Off (profitLevel=0).

Robot used to skip protect whenever stopLevel was set, so Admin showed TP OFF
and "existing SL/TP left unchanged". Manual adopt must one-shot attach OHLC TP.
"""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store, _broker_price_level
from tests.test_tp_min_distance import _OpenCaptureBroker


def test_needs_protect_when_manual_tp_off():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        p = {
            'dealId': 'man1', 'direction': 'SELL', 'level': 3716.39,
            'stopLevel': 3725.08, 'profitLevel': 0, 'epic': 'GOLD', 'size': '0.08',
        }
        assert _broker_price_level(p['profitLevel']) is None
        assert engine._needs_protect_brackets(p) is True
        store.set('tp_attach_done:man1', True)
        assert engine._needs_protect_brackets(p) is False


def test_needs_protect_skips_after_trail_runtime():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        store.set('robot_open:r1', True)
        store.set('adaptive_runtime:r1', {'status': 'CONFIRMED', 'phase': 'trail'})
        p = {
            'dealId': 'r1', 'direction': 'BUY', 'level': 100.,
            'stopLevel': 99.2, 'profitLevel': None, 'epic': 'SIM', 'size': '1',
        }
        assert engine._needs_protect_brackets(p) is False


def test_protect_attaches_tp_on_manual_sl_only_position():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            # Manual adopt: SL on, Capital Take Profit Off as 0 — no robot_open yet.
            broker.open = [{'position': {
                'dealId': 'manual-tp', 'size': '.08', 'direction': 'SELL',
                'level': 100.0, 'stopLevel': 100.8, 'profitLevel': 0, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]

            async def _confirm(ref):
                # Apply requested TP onto the live mock position so reconcile matches.
                req = [r for r in broker.requests if r[0] == 'PROTECT'][-1]
                _, deal_id, stop, profit = req
                pos = broker.open[0]['position']
                if stop is not None:
                    pos['stopLevel'] = float(stop)
                if profit is not None:
                    pos['profitLevel'] = float(profit)
                return {'dealStatus': 'ACCEPTED', 'dealId': deal_id}

            async def _update(deal_id, stop_level=None, profit_level=None):
                broker.requests.append(('PROTECT', deal_id, stop_level, profit_level))
                return {'dealReference': 'protect-manual'}

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
            pos = engine.positions[0]
            assert pos['profitLevel'] is None
            assert engine._needs_protect_brackets(pos) is True
            assert not store.get('robot_open:manual-tp')
            await engine.protect_position(pos)
            protects = [r for r in broker.requests if r[0] == 'PROTECT']
            assert protects, 'expected PROTECT to attach missing manual TP'
            _, deal_id, stop, profit = protects[0]
            assert deal_id == 'manual-tp'
            assert stop is None  # SL already live
            assert profit is not None and float(profit) < 100.0  # SELL TP below entry
            assert store.get('tp_attach_done:manual-tp')
            assert store.get('robot_open:manual-tp')
            meta = store.get('adaptive_protection:manual-tp')
            assert meta and meta.get('owner') == 'AUTO'

    asyncio.run(scenario())
