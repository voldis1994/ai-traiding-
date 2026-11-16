"""Fast reverse: manage perception → CLOSE; teach opposite not HOLD; no wrong nudge."""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.market_context import FEATURE_DIM, FEATURE_SCHEMA
from app.profit_learning import label_from_profit
from app import structure_edge as se
from tests.test_integration import BrokerSimulation


BASE = datetime(2026, 10, 14, 17, 10, tzinfo=timezone.utc)


def test_soft_nudge_skips_when_live_trend_disagrees():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        advice = {'advice': 'BUY', 'confidence': 0.9, 'reason': 'hist'}
        scores = {'BUY': 0.4, 'SELL': 0.4, 'HOLD': 0.2}
        nudged, meta = se.soft_nudge_scores(
            scores, advice, store=store, live_trend='DOWN')
        assert meta['nudged'] is False
        assert meta['reason'] == 'live_trend_disagrees'
        assert nudged == scores


def test_reverse_loss_teaches_opposite_not_gave_then_hold():
    """Went green then SL on a reverse — teach SELL, not HOLD spam."""
    bars = [
        {'open': 100., 'high': 100.8, 'low': 99.9, 'close': 100.5, 'spread': 0.2},
        {'open': 100.5, 'high': 100.6, 'low': 98.2, 'close': 98.4, 'spread': 0.2},
    ]
    out = label_from_profit(
        'BUY', -1.5, entry=100., exit_level=98.4, spread=0.2,
        bars=bars, stop_distance=1.0, profit_distance=1.5, tick=0.01,
        path_mfe=0.8, path_mae=1.6,
        structure_frame_advice={'advice': 'SELL', 'confidence': 0.7})
    assert out['label'] == 1  # SELL
    assert 'hold' not in str(out.get('mode') or '').lower() or 'structure' in str(
        out.get('structure_teach') or out.get('mode') or '')


def test_confirmed_reverse_market_closes(monkeypatch):
    """Manage scores opposite + streak + aligned returns → market CLOSE."""
    from app import engine as module
    clock = [BASE]

    def context(rows, *, spread=0., epic=None, **_kw):
        return {
            'ready': True, 'reason': 'synthetic', 'epic': epic,
            'bar_time': clock[0].isoformat(), 'close': 100.,
            'features': [0.02] * FEATURE_DIM,
            'trend': 'DOWN', 'regime': 'ALIGNED',
            'returns_bps': {'1': -1, '5': -4, '15': -5, '60': -6},
            'feature_schema': FEATURE_SCHEMA,
        }

    monkeypatch.setattr(module, 'market_features', context)
    monkeypatch.setattr(module, 'now', lambda: clock[0].isoformat())

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 'db.sqlite3')
            brain = AdaptiveBrain(store, data_dir=d)
            e = SingleTradeEngine(store, brain)
            e.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = BrokerSimulation()
            broker.open[0]['position'].update(
                stopLevel=99., profitLevel=101., direction='BUY')
            broker.open[0]['market']['epic'] = 'SIM'
            e.attach(broker)
            e._schedule_calendar_in_background = lambda: None
            e._schedule_liquidity_in_background = lambda: None
            exits = []
            async def fake_exit(deal_id):
                exits.append(deal_id)
                broker.open = []
                e.positions = []
            e.exit_position = fake_exit
            # Seed locked OPEN BUY vote (Admin freeze).
            e.last_signal = {
                'scores': {'BUY': 0.7, 'SELL': 0.2, 'HOLD': 0.1},
                'scores_raw': {'BUY': 0.7, 'SELL': 0.2, 'HOLD': 0.1},
                'action': 'BUY',
            }
            e._locked_entry_signal = dict(e.last_signal, scores_frozen=True)
            e.last_decision_bar = (clock[0] - timedelta(minutes=5)).isoformat()
            # Force manage perception to SELL each new bar.
            def sell_tick(*_a, **kw):
                assert kw.get('in_position') is True
                return {
                    'scores': {'BUY': 0.1, 'SELL': 0.8, 'HOLD': 0.1},
                    'action': 'SELL', 'z': [0.0],
                }
            brain.tick = sell_tick
            await e.sync()
            for _ in range(3):
                clock[0] += timedelta(minutes=1)
                e.last_candle_tick = -20
                await e.poll()
                if exits:
                    break
            assert exits, 'confirmed reverse must market-close'
            events = store.recent(40)
            assert any(
                ev.get('kind') == 'exit'
                and 'confirmed reverse' in str(ev.get('message', '')).lower()
                for ev in events)
            snap = e.snapshot()
            assert snap.get('neural_market_exit') is True

    asyncio.run(scenario())


def test_snapshot_advertises_executable_neural_exit():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        snap = e.snapshot()
        assert snap.get('neural_market_exit') is True
        assert snap.get('neural_close_executable') is True
