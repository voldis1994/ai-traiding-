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
        # Flag alone is not enough while broker still shows TP Off — clear + cool-down.
        store.set('tp_attach_done:man1', True)
        assert engine._needs_protect_brackets(p) is True
        assert store.get('tp_attach_done:man1') is False
        rt = store.get('protect_runtime:man1')
        assert rt and rt.get('status') == 'RETRY_TP'
        # Once broker has a real TP, flag stops further protect.
        p['profitLevel'] = 3700.0
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


def test_protect_ignores_foreign_unresolved_orders():
    """Stale journal rows for other deals must not freeze this client's TP."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 3714.0
            broker.open = [{'position': {
                'dealId': 'andris-lv', 'size': '.08', 'direction': 'BUY',
                'level': 3714.61, 'stopLevel': 3705.69, 'profitLevel': 0, 'upl': -0.11,
            }, 'market': {'epic': 'GOLD'}}]

            async def _confirm(ref):
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
                return {'dealReference': 'protect-lv'}

            broker.confirm = _confirm
            broker.update_position = _update
            engine.attach(broker)
            await engine.sync()
            from tests.test_integration import seed_swing_candles
            seed_swing_candles(store, 'GOLD')
            # Leftover unresolved CLOSE/PROTECT from another account/deal.
            store.order('CLOSE', 'other-deal', {'size': 0.1})
            store.order('PROTECT', 'other-deal', {'stop': 1, 'profit': 2})
            engine.market = {
                'epic': 'GOLD', 'mid': 3713.28, 'bid': 3713.0, 'ask': 3713.5,
                'spread': 0.5, 'tick': 0.01, 'min_distance': 0.5, 'status': 'TRADEABLE',
            }
            pos = engine.positions[0]
            await engine.protect_position(pos)
            protects = [r for r in broker.requests if r[0] == 'PROTECT']
            assert protects, 'foreign unresolved must not block LV manual TP'
            assert store.get('tp_attach_done:andris-lv')
            assert store.get('robot_open:andris-lv')
            brief = engine.protection_brief or {}
            assert 'Manual adopt protected' in str(brief.get('status') or '')

    asyncio.run(scenario())


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
            # Capital omits clear the other bracket — always re-send both.
            assert stop is not None and float(stop) == 100.8  # keep live SL
            assert profit is not None and float(profit) < 100.0  # SELL TP below entry
            assert store.get('tp_attach_done:manual-tp')
            assert store.get('robot_open:manual-tp')
            meta = store.get('adaptive_protection:manual-tp')
            assert meta and meta.get('owner') == 'AUTO'

    asyncio.run(scenario())


def test_protect_restores_sl_when_tp_only_left():
    """TP-only PUT used to wipe SL; recovery must re-send BOTH in one shot."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 3717.0
            # Screenshot state: TP ON, SL OFF after a TP-only protect wipe.
            broker.open = [{'position': {
                'dealId': 'andris-sl-off', 'size': '.08', 'direction': 'BUY',
                'level': 3718.95, 'stopLevel': None, 'profitLevel': 3721.01, 'upl': -0.14,
            }, 'market': {'epic': 'GOLD'}}]

            async def _confirm(ref):
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
                return {'dealReference': 'protect-both'}

            broker.confirm = _confirm
            broker.update_position = _update
            engine.attach(broker)
            await engine.sync()
            from tests.test_integration import seed_swing_candles
            seed_swing_candles(store, 'GOLD')
            engine.market = {
                'epic': 'GOLD', 'mid': 3717.20, 'bid': 3717.0, 'ask': 3717.4,
                'spread': 0.4, 'tick': 0.01, 'min_distance': 0.5, 'status': 'TRADEABLE',
            }
            pos = engine.positions[0]
            assert engine._needs_protect_brackets(pos) is True
            await engine.protect_position(pos)
            protects = [r for r in broker.requests if r[0] == 'PROTECT']
            assert protects
            _, deal_id, stop, profit = protects[0]
            assert deal_id == 'andris-sl-off'
            assert stop is not None and float(stop) < 3718.95  # BUY SL below entry
            assert profit is not None and float(profit) == 3721.01  # keep live TP
            assert store.get('tp_attach_done:andris-sl-off')

    asyncio.run(scenario())
