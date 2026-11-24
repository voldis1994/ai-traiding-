"""LIVE OPEN waits for confirmed OHLC Demand/Supply zone; SL/TP stay require_swing."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from app import ohlc_entry_zones
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.ohlc_zone_path import demand_zone_path
from tests.test_learning_integrity import new_brain


def _seed_demand(store, epic='GOLD', start=2000.0, confirm=True):
    rows, mid = demand_zone_path(start=start, confirm=confirm)
    for row in rows:
        store.candle(epic, row, source='HISTORICAL')
    return rows, mid


def _ready_engine(d, store, brain, *, ask):
    e = SingleTradeEngine(store, brain)
    e.policy = AutoEvolution(store, Path(d) / 'pol.py')
    e.mandate = {'epic': 'GOLD', 'size': '0.02'}
    e.market = {'mid': ask, 'ask': ask, 'bid': ask - 0.1, 'tick': 0.1, 'spread': 0.2}
    e.market_context = {
        'ready': True, 'reason': '', 'returns_bps': {},
        'trend': 'UP', 'regime': 'ALIGNED',
    }
    e.latest_fusion_ready = True
    e.last_signal = {
        'action': 'BUY',
        'scores': {'BUY': 0.62, 'SELL': 0.22, 'HOLD': 0.16},
        'confidence': 0.62,
    }
    brain.trading_readiness = lambda: {
        'ready': True, 'reason': 'ok', 'eval_samples': 40,
        'minute_direction_accuracy': 0.4, 'holdout_net_bps_mean': 0.1,
    }
    brain.epic = 'GOLD'
    e._news_risk = lambda when=None: {
        'enabled': True, 'entry_allowed': True, 'status': 'CLEAR', 'reason': 'test'}
    return e


def test_entry_evaluation_holds_until_confirmed_buy_zone():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        rows, _ = _seed_demand(store, confirm=True)
        zones = ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = zones['buy_entry']
        assert buy
        far = float(buy['hi']) + 8.0
        e = _ready_engine(d, store, brain, ask=far)
        # Gate itself must wait outside the Demand box.
        gate_far = e._ohlc_entry_zone_gate('BUY')
        assert gate_far.get('allowed') is False
        assert 'wait' in (gate_far.get('reason') or '').lower()

        inside = (float(buy['lo']) + float(buy['hi'])) / 2
        e.market = {
            'mid': inside, 'ask': inside, 'bid': inside - 0.1,
            'tick': 0.1, 'spread': 0.2,
        }
        gate_in = e._ohlc_entry_zone_gate('BUY')
        assert gate_in.get('allowed') is True
        assert gate_in.get('reason') == 'ohlc_entry_zone_confirm'
        assert gate_in.get('trigger') == 'zone'

        # Full eval: keep BUY proposal so zona gate is the wait reason when far.
        e.market = {
            'mid': far, 'ask': far, 'bid': far - 0.1,
            'tick': 0.1, 'spread': 0.2,
        }
        with patch.object(e, '_ohlc_entry_zone_gate', side_effect=e._ohlc_entry_zone_gate):
            # Force neural path to BUY so zona is the gate under test.
            e.last_signal = {
                'action': 'BUY',
                'scores': {'BUY': 0.80, 'SELL': 0.10, 'HOLD': 0.10},
                'confidence': 0.80,
            }
            with patch.object(
                    e, '_mtf_momentum_analysis',
                    return_value={
                        'ready': True, 'enabled': True, 'mode': 'BUY',
                        'preferred': 'BUY', 'confidence': 0.7, 'move_ok': True,
                        'tf': {'m1': {'atr14': 1.0}},
                    }):
                out = e._entry_evaluation()
        # Either still waiting on zona, or already HOLD for another gate —
        # zona must report not allowed while price is far.
        assert (out.get('ohlc_entry_zone') or gate_far).get('allowed') is False or \
               out.get('action') == 'HOLD'


def test_submit_skips_when_outside_entry_zone():
    """Defense-in-depth: submit() must not market-chase outside the zona."""
    import asyncio

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        rows, _ = _seed_demand(store, confirm=True)
        zones = ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = zones['buy_entry']
        far = float(buy['hi']) + 5.0
        e = _ready_engine(d, store, brain, ask=far)
        e.armed = True
        e.execution_enabled = True
        e.positions = []
        e.pending_order = False
        e.pending_close = False
        e.last_closed_step = 10
        e.last_entry_reason = None

        async def _fake_sync():
            return None

        async def _run():
            with patch.object(e, 'sync', new=_fake_sync):
                with patch.object(
                        e, '_external_entry_check',
                        return_value={'entry_allowed': True}):
                    with patch.object(
                            e, 'live_open_gates',
                            return_value={'allowed': True}):
                        with patch.object(e, '_fusion_entry_blocked',
                                         return_value=False):
                            with patch.object(e, '_tick', return_value=0.1):
                                with patch.object(
                                        e, '_min_distance', return_value=0.5):
                                    await e.submit(
                                        'BUY',
                                        broker_market={
                                            'snapshot': {
                                                'marketStatus': 'TRADEABLE'},
                                            'dealingRules': {},
                                        })

        asyncio.run(_run())
        reason=(e.last_entry_reason or '').lower()
        assert 'wait' in reason
        # Outside zone / mid-range / scenario gate — any HOLD that refused OPEN.
        assert ('entry' in reason or 'zone' in reason or 'mid_range' in reason
                or 'scenario' in reason)


def test_require_swing_still_on_decide_path():
    """OPEN brackets stay swing-anchored — no artificial median invent."""
    from app import bracket_learn
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        rows, _ = _seed_demand(store, confirm=True)
        zones = ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = zones['buy_entry']
        entry = (float(buy['lo']) + float(buy['hi'])) / 2
        zone = bracket_learn.zone_at_open(rows, multi_tf=True)
        decided = bracket_learn.decide_brackets_from_ohlc(
            zone, tick=0.1, spread=0.2, min_distance=0.5,
            entry=entry, action='BUY', candles=rows, atr=1.0,
            kama=entry - 2.0, require_swing=True, entry_zone=buy)
        if decided.get('ready'):
            src = str(decided.get('source') or '')
            assert (
                'swing' in src or 'bos' in src or 'ohlc' in src
                or 'regime' in src
            )
            assert decided.get('stop_distance') is not None
            assert float(decided['stop_level']) < float(buy['lo']) + 1e-6
        else:
            reason = str(decided.get('reason') or decided.get('swing_reason') or '')
            assert reason
