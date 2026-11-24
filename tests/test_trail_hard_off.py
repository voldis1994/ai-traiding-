"""Trail hard OFF — fixed OHLC SL/TP only across LIVE + params + shadow."""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app import self_params as sp
from app import evo_boot
from app.shadow_paper import ShadowPaperJournal


def test_engine_trail_hard_off_ignores_class_flip_and_store():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        assert e.TRAIL_ENABLED is False
        assert e._trail_is_enabled() is False
        store.set('self_params_v1', {**sp.DEFAULTS, 'trail_live_enabled': 1})
        e.TRAIL_ENABLED = True
        assert e._trail_is_enabled() is False
        assert sp.load(store)['trail_live_enabled'] == 0
        assert sp.get(store, 'trail_live_enabled') == 0


def test_evo_boot_forces_trail_off_on_stale_store():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        # Mark boot done so we hit the light path (no sample ingest hang).
        store.set(evo_boot.STORE_FLAG, {'done': True})
        store.set('self_params_v1', {**sp.DEFAULTS, 'trail_live_enabled': 1})
        out = evo_boot.ensure_physical_evo(store, force=False)
        assert out.get('ok')
        assert out.get('trail') == 'forced_off'
        assert sp.load(store)['trail_live_enabled'] == 0


def test_adapt_protection_never_puts_trail():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            e = SingleTradeEngine(store, brain)
            e.policy = AutoEvolution(store, Path(d) / 'pol.py')
            store.set('robot_open:d1', True)
            store.set('adaptive_protection:d1', {
                'owner': 'AUTO', 'original_risk': 1.0, 'original_profit': 2.0,
                'opened': '2026-11-22T10:00:00+00:00'})
            e.positions = [{
                'dealId': 'd1', 'direction': 'BUY', 'level': 100.0,
                'stopLevel': 99.0, 'profitLevel': 102.0, 'epic': 'GOLD',
                'size': '0.1',
            }]
            e.market = {
                'epic': 'GOLD', 'mid': 100.5, 'bid': 100.4, 'ask': 100.6,
                'spread': 0.2, 'tick': 0.01, 'min_distance': 0.5,
                'status': 'TRADEABLE',
            }
            puts = []

            async def _update(deal_id, stop_level=None, profit_level=None):
                puts.append((deal_id, stop_level, profit_level))
                return {'dealReference': 'x'}

            e.broker = type('B', (), {
                'update_position': _update,
            })()
            await e.adapt_protection(e.positions[0])
            assert puts == []
            brief = str((e.protection_brief or {}).get('status') or '')
            assert 'disabled' in brief.lower()
            assert 'OHLC' in brief

    asyncio.run(scenario())


def test_shadow_paper_does_not_trail_stops():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        j = ShadowPaperJournal(store)
        j.position = {
            'direction': 'BUY', 'entry': 100.0, 'stop': 99.0, 'profit': 103.0,
            'original_risk': 1.0, 'original_profit': 3.0,
            'tick': 0.01, 'min_distance': 0.5, 'size': 1.0,
        }
        # Favorable bar that would arm trail if enabled.
        closed = j.on_bar(
            'HOLD', mid=101.5, spread=0.1, bar_time='t1',
            high=101.8, low=101.0, tick=0.01, min_distance=0.5,
            stop_distance=1.0, profit_distance=3.0)
        assert closed is None
        assert j.position['stop'] == 99.0
        assert not j.position.get('trailed')
