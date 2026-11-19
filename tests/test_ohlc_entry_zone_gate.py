"""LIVE OPEN waits for OHLC entry zone; SL/TP stay require_swing."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import ohlc_entry_zones
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def _seed(store, epic='GOLD', n=80, start=2000.0):
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    px = start
    rows = []
    for i in range(n):
        o = px
        c = px + (0.15 if i % 7 else -0.35)
        h = max(o, c) + 0.8 + (1.5 if i % 11 == 10 else 0.1)
        l = min(o, c) - 0.8 - (1.5 if i % 11 == 5 else 0.1)
        row = {
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c,
        }
        store.candle(epic, row, source='HISTORICAL')
        rows.append(row)
        px = c
    return rows


def _ready_engine(d, store, brain, rows, *, ask):
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


def test_entry_evaluation_holds_until_price_in_buy_zone():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        rows = _seed(store)
        zones = ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = zones['buy_entry']
        assert buy
        far = float(buy['hi']) + 8.0
        e = _ready_engine(d, store, brain, rows, ask=far)
        out = e._entry_evaluation()
        assert out['action'] == 'HOLD'
        assert 'wait' in (out.get('reason') or '').lower()
        assert 'entry' in (out.get('reason') or '').lower()
        assert out.get('proposal') == 'BUY'
        assert (out.get('ohlc_entry_zone') or {}).get('allowed') is False

        inside = (float(buy['lo']) + float(buy['hi'])) / 2
        e.market = {
            'mid': inside, 'ask': inside, 'bid': inside - 0.1,
            'tick': 0.1, 'spread': 0.2,
        }
        out2 = e._entry_evaluation()
        assert out2['action'] == 'BUY'
        assert (out2.get('ohlc_entry_zone') or {}).get('allowed') is True
        assert (out2.get('ohlc_entry_zone') or {}).get('reason') == 'ohlc_entry_zone_touch'


def test_submit_skips_when_outside_entry_zone():
    """Defense-in-depth: submit() must not market-chase outside the zona."""
    import asyncio

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        rows = _seed(store)
        zones = ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = zones['buy_entry']
        far = float(buy['hi']) + 5.0
        e = _ready_engine(d, store, brain, rows, ask=far)
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
        assert 'wait' in (e.last_entry_reason or '').lower()
        assert 'entry' in (e.last_entry_reason or '').lower()


def test_require_swing_still_on_decide_path():
    """OPEN brackets stay swing-anchored — no artificial median invent."""
    from app import bracket_learn
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        rows = _seed(store)
        zones = ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = zones['buy_entry']
        entry = (float(buy['lo']) + float(buy['hi'])) / 2
        zone = bracket_learn.zone_at_open(rows, multi_tf=True)
        decided = bracket_learn.decide_brackets_from_ohlc(
            zone, tick=0.1, spread=0.2, min_distance=0.5,
            entry=entry, action='BUY', candles=rows, require_swing=True)
        # Either ready with swing source, or blocked waiting swings — never invent.
        if decided.get('ready'):
            src = str(decided.get('source') or '')
            assert 'swing' in src or 'bos' in src or 'ohlc' in src
            assert decided.get('stop_distance') is not None
        else:
            reason = str(decided.get('reason') or decided.get('swing_reason') or '')
            assert reason  # explicit unready, not silent invent
