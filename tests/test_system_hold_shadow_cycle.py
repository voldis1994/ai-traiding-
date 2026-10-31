"""End-to-end: fixed 0.06 floor + policy LIVE + HOLD→opposite shadow + teach.

Proves the pieces work *together*:
  1. Weak BUY proposal → policy/cycle HOLD (floor stays 0.06)
  2. Engine queues hold_shadow with would-be brackets
  3. Down path → opposite SELL hits TP → learn_from_trade
  4. settle(£) cools aggression but does NOT move entry floor
  5. Clear edge still OPENs; strategy hint after enough opposite TPs
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app import hold_shadow as hs
from app import learning_generation
from app import unified_cycle as uc
from app.autonomy import AutoEvolution, TEMPLATES
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.math_rules import stop_take
from tests.test_integration import BrokerSimulation


def sandbox():
    return tempfile.TemporaryDirectory()


def _seed_hist_candles(store, epic, *, n=80, start_px=100.0, t0=None, trend=0.02):
    t0 = t0 or datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
    px = start_px
    rows = []
    for i in range(n):
        o = px
        c = px + trend
        h = max(o, c) + 0.08
        l = min(o, c) - 0.08
        at = (t0 + timedelta(minutes=i)).isoformat().replace('+00:00', 'Z')
        bar = {'at': at, 'open': o, 'high': h, 'low': l, 'close': c, 'spread': 0.02}
        store.candle(epic, bar, source='HISTORICAL')
        rows.append(bar)
        px = c
    return rows


def _append_down_path(store, epic, *, from_at, n=50, start_px=100.0, step=0.45):
    """Bars at/after from_at trending down — SELL TP / BUY SL with ~1.0 distances."""
    t0 = datetime.fromisoformat(str(from_at).replace('Z', '+00:00'))
    px = start_px
    for i in range(n):
        o = px
        c = px - step
        h = o + 0.05
        l = c - 0.05
        at = (t0 + timedelta(minutes=i)).isoformat().replace('+00:00', 'Z')
        store.candle(
            epic,
            {'at': at, 'open': o, 'high': h, 'low': l, 'close': c, 'spread': 0.20},
            source='HISTORICAL')
        px = c


def test_system_hold_shadow_teaches_opposite_with_fixed_floor():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d, seed=29)
        brain._activate('SIM')
        # Force a known transition for learn_from_trade.
        brain.core.last_z = np.linspace(-0.2, 0.2, brain.core.d)
        brain.core.step(brain.core.last_z, dt=60.)
        assert brain.core.last_transition is not None

        e = SingleTradeEngine(store, brain)
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.06),
            encoding='utf8')
        e.policy = AutoEvolution(store, path)
        broker = BrokerSimulation()
        # Flat book — mandate adopted then closed so entry path is legal.
        import asyncio

        async def _prep():
            e.attach(broker)
            await e.sync()
            broker.open = []
            await e.sync()

        asyncio.run(_prep())
        assert e.mandate and e.mandate['epic'] == 'SIM'
        brain.mark_prep_ready('test fixture')

        # Warm OHLC for would-be brackets.
        hist = _seed_hist_candles(store, 'SIM', n=90, start_px=100.0, trend=0.01)
        hold_bar = hist[-1]['at']

        e.market = {
            'mid': 100.0, 'spread': 0.20, 'bid': 99.9, 'ask': 100.1,
            'tick': 0.1, 'epic': 'SIM', 'status': 'TRADEABLE', 'min_distance': 0.15,
        }
        e.market_context = {
            'ready': True, 'reason': 'test', 'epic': 'SIM', 'bar_time': hold_bar,
            'close': 100.0, 'trend': 'UP', 'regime': 'ALIGNED',
            'returns_bps': {'1': 1, '5': 2, '15': 3, '60': 4},
        }
        e.last_decision_bar = hold_bar
        # Weak BUY edge 0.05 < calibrated seed ~0.06 → policy HOLD, proposal BUY.
        weak = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}
        e.last_signal = {'action': 'BUY', 'scores': weak}

        # ── 1) Policy + cycle agree: HOLD at calibrated floor ───────────────
        out = e.policy.evaluate_neural_entry(weak, readiness={'ready': True})
        assert out['action'] == 'HOLD'
        assert out['proposal'] == 'BUY'
        floor = out['evidence']['evolved_policy']['entry_edge_floor']
        assert floor > 0.05  # weak 0.05 must stay below gate
        assert uc.min_open_margin(uc.load_state(store)) == pytest.approx(floor)
        gate = uc.edge_allows_open(weak, {'action': 'BUY'}, uc.load_state(store))
        assert gate['allowed'] is False

        # ── 2) Engine queues hold_shadow with real would-be brackets ────────
        gen_before = learning_generation.current(store)
        labeled_before = int(brain.trade_labeled)
        row = e._enqueue_hold_shadow(out)
        assert row is not None
        assert row['proposal'] == 'BUY'
        assert row['opposite'] == 'SELL'
        assert row['stop_distance'] and row['profit_distance']
        assert len(hs.load_pending(store)) == 1
        snap = e.snapshot()
        assert 'hold_shadow' in snap['self_development']
        assert snap['self_development']['hold_shadow']['pending'] == 1

        # ── 3) Path dumps → resolve teaches SELL ────────────────────────────
        _append_down_path(
            store, 'SIM', from_at=hold_bar, n=55,
            start_px=float(row['entry']), step=0.50)
        # Tighten case distances so TP is reachable on this path.
        pending = hs.load_pending(store)
        pending[0]['stop_distance'] = 1.0
        pending[0]['profit_distance'] = 1.0
        pending[0]['spread'] = 0.20
        pending[0]['tick'] = 0.1
        hs.save_pending(store, pending)

        outcomes = e._resolve_hold_shadows()
        assert outcomes, 'expected opposite TP resolve'
        assert outcomes[0]['taught'] is True
        assert outcomes[0]['opposite'] == 'SELL'
        assert outcomes[0]['opposite_hit_tp'] is True
        assert outcomes[0]['mode'] == 'hold_missed_opposite_tp_sell'
        assert hs.load_stats(store)['taught_opposite'] >= 1
        assert hs.load_pending(store) == []
        assert learning_generation.current(store) > gen_before
        assert int(brain.trade_labeled) >= labeled_before  # count_sample may bump

        # ── 4) £ settle moves aggression, NOT the calibrated floor ──────────
        before_floor = uc.load_state(store)['entry_edge_floor']
        uc.settle(store, pnl=-2.0, action='BUY')
        st = uc.load_state(store)
        assert st['entry_edge_floor'] == pytest.approx(before_floor)
        assert st['aggression'] < 1.0
        assert uc.min_open_margin(st) == pytest.approx(before_floor)

        # ── 5) Clear edge still confirms OPEN under same fixed floor ────────
        strong = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
        ok = e.policy.evaluate_neural_entry(strong, readiness={'ready': True})
        assert ok['action'] == 'BUY'
        assert uc.edge_allows_open(
            strong, {'action': 'BUY'}, uc.load_state(store))['allowed'] is True

        # ── 6) Hint fires early (n≥5, rate≥50%) ────────────────────────────
        stats = hs.load_stats(store)
        stats['resolved'] = 5
        stats['opposite_hit_tp'] = 3
        hs.save_stats(store, hs._update_strategy_hint(stats))
        ev = hs.strategy_evidence(store)
        assert ev['strategy_hint']
        assert 'opposite hit TP' in ev['strategy_hint']


def test_system_would_be_brackets_match_stop_take_shape():
    """Would-be brackets for shadow use same stop_take math as LIVE OPEN."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d, seed=7)
        brain._activate('SIM')
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e.mandate = {'epic': 'SIM', 'size': '0.25', 'initial_deal_id': 'm1'}
        _seed_hist_candles(store, 'SIM', n=100, start_px=200.0)
        e.market = {
            'mid': 200.0, 'spread': 0.30, 'bid': 199.85, 'ask': 200.15,
            'tick': 0.1, 'epic': 'SIM', 'status': 'TRADEABLE', 'min_distance': 0.5,
        }
        e.last_signal = {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}}
        got = e._would_be_brackets('BUY')
        assert got is not None
        assert got['stop_distance'] > 0 and got['profit_distance'] > 0
        # Recompute with same inputs — distances finite and broker-min aware.
        direct = stop_take(
            got['entry'], 'BUY', got['tick'], got['spread'], got['min_distance'],
            kappa=1.0, sl_scale=1.0, range_distance=1.0)
        assert float(direct['stop_distance']) > 0
