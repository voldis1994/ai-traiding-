"""Audit #9: after κ/rounding, SL and TP must meet broker min distance.

Behavior chosen: stop_take raises TP (and SL if needed) onto the broker min
tick grid when κ<1 would leave TP below min. submit / protect_position /
adapt_protection still refuse to send if distances somehow remain non-compliant
("Nepieļaut ordera nosūtīšanu ar neatbilstošu SL vai TP").
"""
import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.autonomy import AutoEvolution
from app.bracket_learn import MULT_KAPPA_MIN
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.math_rules import distances_meet_broker_min, levels_meet_broker_min, stop_take


def test_kappa_half_raises_tp_to_broker_min():
    # min=0.5, buffer pushes SL above min; kappa=0.5 would halve TP below min
    # without the audit #9 floor.
    buy = stop_take('100', 'BUY', '.01', '.02', '.5', kappa=0.5)
    assert buy['stop_distance'] >= Decimal('.5')
    assert buy['profit_distance'] >= Decimal('.5')
    assert buy.get('raised_to_broker_min') is True
    assert distances_meet_broker_min(buy['stop_distance'], buy['profit_distance'], '.5')


def test_mult_kappa_min_cannot_undercut_broker_min():
    """MULT_KAPPA_MIN=0.5 on a tight instrument must still meet min after snap."""
    minimum = Decimal('0.15')
    tick = Decimal('0.01')
    spread = Decimal('0.01')
    raw_sl = minimum + max(2 * tick, 2 * spread)
    assert (MULT_KAPPA_MIN * float(raw_sl)) < float(minimum)

    out = stop_take(100, 'BUY', tick, spread, minimum, kappa=MULT_KAPPA_MIN, sl_scale=1)
    assert out['stop_distance'] >= minimum
    assert out['profit_distance'] >= minimum
    assert out['raised_to_broker_min'] is True
    assert distances_meet_broker_min(out['stop_distance'], out['profit_distance'], minimum)


def test_distances_reject_below_min():
    assert not distances_meet_broker_min('.4', '.6', '.5')
    assert not distances_meet_broker_min('.6', '.4', '.5')
    assert distances_meet_broker_min('.5', '.5', '.5')
    assert distances_meet_broker_min('.02', '.02', 0)


def test_levels_meet_broker_min_buy_and_sell():
    assert levels_meet_broker_min('BUY', 99.4, 100.6, bid=100, ask=100.1, min_distance=0.5)
    assert not levels_meet_broker_min('BUY', 99.7, 100.6, bid=100, ask=100.1, min_distance=0.5)
    assert levels_meet_broker_min('SELL', 100.6, 99.4, bid=100, ask=100.1, min_distance=0.5)
    assert not levels_meet_broker_min('SELL', 100.3, 99.4, bid=100, ask=100.1, min_distance=0.5)
    assert levels_meet_broker_min('BUY', 99.4, None, bid=100, ask=100.1, min_distance=0.5)


class _OpenCaptureBroker:
    def __init__(self):
        self.open = []
        self.requests = []
        self.price = 100.0

    async def positions(self):
        return list(self.open)

    async def market(self, epic):
        return {
            'snapshot': {'bid': self.price, 'offer': self.price + 0.02,
                         'marketStatus': 'TRADEABLE', 'decimalPlacesFactor': 2},
            'instrument': {'onePipMeans': 0.01},
            'dealingRules': {'minStopOrProfitDistance': {'unit': 'POINTS', 'value': '0.15'}},
        }

    async def accounts(self):
        return {'accounts': [{'preferred': True, 'accountId': 'SIM', 'currency': 'USD',
                              'balance': {'balance': 1000}}]}

    async def prices(self, epic, resolution='MINUTE', maximum=80):
        return {'prices': []}

    async def confirm(self, ref):
        return {'dealStatus': 'ACCEPTED', 'dealId': 'b9'}

    async def open_position(self, epic, direction, size, stop_distance=None, profit_distance=None):
        self.requests.append(('OPEN', epic, direction, size, stop_distance, profit_distance))
        sign = 1 if direction == 'BUY' else -1
        self.open = [{'position': {
            'dealId': 'b9', 'size': str(size), 'direction': direction, 'level': self.price,
            'stopLevel': self.price - sign * float(stop_distance),
            'profitLevel': self.price + sign * float(profit_distance),
        }, 'market': {'epic': epic}}]
        return {'dealReference': 'entry-9'}

    async def update_position(self, deal_id, stop_level=None, profit_level=None):
        self.requests.append(('PROTECT', deal_id, stop_level, profit_level))
        return {'dealReference': 'protect-9'}

    async def close_position(self, deal_id):
        self.open = []
        return {'dealReference': 'close-9'}


def test_submit_kappa_half_never_sends_sub_min_tp():
    """κ=0.5: distances raised to min → OPEN may send; never below broker min."""

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            # Adopt size/epic from a prior position, then clear so submit can open.
            broker.open = [{'position': {
                'dealId': 'seed', 'size': '.25', 'direction': 'BUY', 'level': 100,
                'stopLevel': 99.7, 'profitLevel': 100.5, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]
            engine.attach(broker)
            await engine.sync()
            broker.open = []
            await engine.sync()
            engine.last_closed_step = 0
            engine._external_entry_check = lambda _: {'entry_allowed': True, 'status': 'TEST'}
            engine.live_open_gates = lambda action=None: {'allowed': True, 'reason': 'TEST'}
            engine.market = {
                'epic': 'SIM', 'mid': 100.0, 'bid': 99.99, 'ask': 100.02,
                'spread': 0.03, 'tick': 0.01, 'min_distance': 0.15, 'status': 'TRADEABLE',
            }
            engine.market_context = {'ready': True}
            with patch('app.bracket_learn.propose_for_open', return_value={
                'kappa': 0.5, 'sl_scale': 1.0, 'kappa_mult': 0.5, 'sl_mult': 1.0,
                'explored': False, 'nudge_samples': 0,
            }):
                await engine.submit('BUY')
            opens = [r for r in broker.requests if r[0] == 'OPEN']
            assert opens, 'expected OPEN after raising TP to broker min'
            _, _, _, _, stop_d, profit_d = opens[0]
            assert Decimal(str(stop_d)) >= Decimal('0.15')
            assert Decimal(str(profit_d)) >= Decimal('0.15')
            assert distances_meet_broker_min(stop_d, profit_d, 0.15)

    asyncio.run(scenario())


def test_protect_skips_when_validator_fails():
    """Safety net: if distances fail the min check, no protect PUT is sent."""

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.open = [{'position': {
                'dealId': 'm9', 'size': '.25', 'direction': 'BUY', 'level': 100,
                'stopLevel': None, 'profitLevel': None, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]
            engine.attach(broker)
            await engine.sync()
            engine.market = {
                'epic': 'SIM', 'mid': 100.0, 'bid': 99.99, 'ask': 100.02,
                'spread': 0.03, 'tick': 0.01, 'min_distance': 0.15, 'status': 'TRADEABLE',
            }
            with patch('app.engine.distances_meet_broker_min', return_value=False):
                await engine.protect_position(engine.positions[0])
            assert not any(r[0] == 'PROTECT' for r in broker.requests)
            msgs = [r[0] for r in store.db.execute('SELECT message FROM events').fetchall()]
            assert any('Protection skipped: SL/TP below broker min' in (m or '') for m in msgs)

    asyncio.run(scenario())
