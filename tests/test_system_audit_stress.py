"""End-to-end stress: OPEN → trail → close → learn as one machine.

Proves the B1–B18 fixes stay glued: nudged scores reach submit, scratch does
not cool aggression, early-arm never goes loss-side, teach respects floor,
once-only learn is not burned on pennies.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.engine import Store, SingleTradeEngine
from app import structure_edge as se
from app import unified_cycle as uc
from app.protection_ai import propose_broker_trail
from app.profit_learning import label_from_profit
from app.math_rules import levels_meet_broker_min
from app.feature_validation import recommended_live_history_bars
from tests.test_adaptive_live_protection import (
    UpdatingBroker, Brain, _market, _position, _meta, engine_fixture,
)


def test_nudge_does_not_compound_across_reeval():
    """Engine must always nudge from scores_raw — never stack on prior nudge."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        from app import structure_shadow as ss
        lib = ss.load_library(store)
        for side, pnl in (('BUY', 5.0), ('SELL', -4.0)):
            key = ss.frame_key(epic='GOLD', structure='TREND_UP', direction=side)
            lib.setdefault('buckets', {})[key] = {
                'samples': 10, 'total_pnl': pnl, 'win_count': 7, 'loss_count': 3}
        store.set(ss.STORE_KEY, lib)
        advice = se.compare_frame(store, epic='GOLD', structure='TREND_UP')
        assert advice['advice'] == 'BUY'
        raw = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}
        # Simulate engine: first nudge from raw, second also from scores_raw.
        n1, m1 = se.soft_nudge_scores(raw, advice, store=store)
        assert m1.get('nudged')
        signal = {'scores': dict(n1), 'scores_raw': dict(raw)}
        base = dict(signal['scores_raw'])
        n2, m2 = se.soft_nudge_scores(base, advice, store=store)
        assert n1 == n2
        # Wrong path (re-nudge from already nudged) would drift further.
        wrong, _ = se.soft_nudge_scores(signal['scores'], advice, store=store)
        assert wrong != n1


def test_scratch_settle_preserves_aggression():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        before = uc.load_state(store)['aggression']
        after = uc.settle(store, pnl=-0.02, action='BUY', scratch_band=0.05)
        assert after['loss_streak'] == 0
        assert after['last_scratch'] is True
        assert after['aggression'] >= before * 0.99


def test_early_arm_never_loss_side_of_entry():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        pos = _position(stop=99.2, profit=102.0)
        market = _market(100.5, 100.6)
        meta = _meta(0.8)
        arm = {'arm': True, 'reason': 'mfe', 'mfe': 0.5}
        out = propose_broker_trail(
            pos, market, meta, extreme=100.5, store=store, arm_decision=arm)
        if out is not None:
            assert out['stop_level'] >= 100.0 - 1e-9


def test_teach_never_bypasses_cost_floor():
    taught, mode = se.teach_after_loss(
        direction='BUY', cf_best='HOLD',
        frame_advice={'advice': 'SELL', 'buy': {'mean_pnl': -1},
                      'sell': {'mean_pnl': 1}},
        opp_val=0.01, opp_floor=0.1)
    assert taught == 'HOLD'
    assert 'below_floor' in mode


def test_trail_stop_only_allows_tp_near_market():
    assert levels_meet_broker_min(
        'BUY', 100.55, None, bid=101.35, ask=101.45, min_distance=0.3)


def test_history_need_covers_multi_tf():
    need = recommended_live_history_bars(None)
    assert need >= 260


def test_adapt_protection_disabled_ignores_stale_protect_journal():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e, b, s = engine_fixture(d)
            # Stale PENDING PROTECT must not unlock a trail PUT — trail is off.
            oid = s.order('PROTECT', 'auto1', {'stop': 99.5, 'profit': 101.6})
            s.order_update(oid, 'PENDING', 'stale-ref')
            assert s.unresolved()
            await e.sync()
            live_sl = e.positions[0]['stopLevel']
            await e.adapt_protection(e.positions[0])
            assert not [r for r in b.requests if r[0] == 'PROTECT']
            assert e.positions[0]['stopLevel'] == live_sl
            brief = str((e.protection_brief or {}).get('status') or '')
            assert 'disabled' in brief.lower()
    asyncio.run(scenario())
