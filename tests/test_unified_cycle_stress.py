"""Adversarial stress — try to break the unified LIVE cycle.

Not a profitability test. Goals: crash, NaN leak, unbounded state, permanent
OPEN lockout, or SL/TP that violate broker mins after compose_open.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app import bracket_learn
from app import unified_cycle as uc
from app.engine import AdaptiveBrain, Store
from app.math_rules import distances_meet_broker_min, stop_take


def sandbox():
    return tempfile.TemporaryDirectory()


# ── poison inputs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('scores', [
    None,
    {},
    {'BUY': 'x', 'SELL': 0.1, 'HOLD': 0.1},
    {'BUY': float('nan'), 'SELL': 0.2, 'HOLD': 0.1},
    {'BUY': float('inf'), 'SELL': float('-inf'), 'HOLD': 0.0},
    {'BUY': 1e308, 'SELL': -1e308, 'HOLD': 0.0},
    {'BUY': None, 'SELL': None, 'HOLD': None},
])
def test_perceive_survives_poison_scores(scores):
    d = uc.perceive_decision(scores)
    assert d['action'] in ('BUY', 'SELL', 'HOLD')
    # Unknown / poison scores → margin None (not invented weak 0.0).
    assert d['margin'] is None or math.isfinite(d['margin'])
    assert math.isfinite(d['confidence'])


@pytest.mark.parametrize('pnl', [
    None, 'bad', float('nan'), float('inf'), float('-inf'), 1e20, -1e20, 0, 0.0,
])
def test_settle_survives_poison_pnl(pnl):
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        state = uc.settle(store, pnl=pnl, action='BUY', exit_reason='sl')
        assert uc.AGGRESSION_MIN <= state['aggression'] <= uc.AGGRESSION_MAX
        assert state['loss_streak'] >= 0 and state['win_streak'] >= 0
        assert math.isfinite(state['aggression'])


def test_load_state_survives_corrupted_store():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.set(uc.STORE_KEY, {
            'aggression': 'nope',
            'loss_streak': -99,
            'win_streak': float('nan'),
            'opens': 'x',
            'version': None,
            'extra_garbage': {'nested': True},
        })
        state = uc.load_state(store)
        assert uc.AGGRESSION_MIN <= state['aggression'] <= uc.AGGRESSION_MAX
        assert state['loss_streak'] >= 0
        assert math.isfinite(state['aggression'])


def test_compose_open_poison_zone_and_learned():
    scores = {'BUY': 0.9, 'SELL': 0.05, 'HOLD': 0.05}
    pkg = uc.compose_open(
        action='BUY', scores=scores, evaluation={'action': 'BUY'},
        zone={'kappa': float('nan'), 'range_distance': -5, 'max_tp': float('inf')},
        learned={'kappa': float('-inf'), 'sl_scale': 0},
        cycle_state=uc.default_state(), base_sl=float('nan'), max_tp='x')
    assert pkg['action'] in ('BUY', 'SELL', 'HOLD')
    if not pkg.get('blocked'):
        assert pkg['kappa'] is None or math.isfinite(pkg['kappa'])
        assert pkg['sl_scale'] is None or math.isfinite(pkg['sl_scale'])
        assert pkg['sl_scale'] is None or pkg['sl_scale'] >= 1.0


# ── streak lockout / aggression hammer ───────────────────────────────────────

def test_thousand_losses_aggression_stays_bounded_not_dead():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for _ in range(1000):
            uc.settle(store, pnl=-50.0, action='SELL')
        state = uc.load_state(store)
        assert state['aggression'] == pytest.approx(uc.AGGRESSION_MIN)
        assert state['loss_streak'] == 1000
        # Edge gate becomes strict but finite — strong edge must still be allowed.
        need = uc.min_open_margin(state)
        assert need == pytest.approx(0.20)
        strong = {'BUY': 0.95, 'SELL': 0.10, 'HOLD': 0.10}  # margin 0.85
        gate = uc.edge_allows_open(strong, {'action': 'BUY'}, state)
        assert gate['allowed'] is True
        weak = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}  # margin 0.05
        assert uc.edge_allows_open(weak, {'action': 'BUY'}, state)['allowed'] is False


def test_thousand_wins_aggression_capped():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for _ in range(1000):
            uc.settle(store, pnl=50.0, action='BUY')
        state = uc.load_state(store)
        assert state['aggression'] == pytest.approx(uc.AGGRESSION_MAX)
        assert state['win_streak'] == 1000
        assert state['loss_streak'] == 0


def test_alternating_whipsaw_500_does_not_nan():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for i in range(500):
            uc.settle(store, pnl=(12.0 if i % 2 == 0 else -12.0), action='BUY')
        state = uc.load_state(store)
        assert math.isfinite(state['aggression'])
        assert uc.AGGRESSION_MIN <= state['aggression'] <= uc.AGGRESSION_MAX


# ── bracket + compose under extreme multipliers ──────────────────────────────

def test_compose_at_mult_rails_still_broker_legal():
    zone = {'kappa': 2.5, 'range_distance': 2.0, 'max_tp': 8.0}
    learned = {'kappa': 2.5, 'sl_scale': 2.5}
    state = uc.default_state()
    state['aggression'] = uc.AGGRESSION_MAX
    scores = {'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1}
    pkg = uc.compose_open(
        action='BUY', scores=scores, evaluation={'action': 'BUY', 'score_edge': 0.7},
        zone=zone, learned=learned, cycle_state=state, base_sl=2.0, max_tp=8.0)
    assert not pkg['blocked']
    st = stop_take(2000, 'BUY', 0.1, 0.3, 0.5,
                   kappa=pkg['kappa'], sl_scale=pkg['sl_scale'], range_distance=2.0)
    assert distances_meet_broker_min(st['stop_distance'], st['profit_distance'], 0.5)


def test_compose_at_min_aggression_after_losses_broker_legal():
    zone = {'kappa': 0.5, 'range_distance': 0.6, 'max_tp': 1.2}
    learned = {'kappa': 0.5, 'sl_scale': 1.0}
    state = uc.default_state()
    state['aggression'] = uc.AGGRESSION_MIN
    state['loss_streak'] = 10
    scores = {'BUY': 0.9, 'SELL': 0.05, 'HOLD': 0.05}  # clears 0.20 need
    pkg = uc.compose_open(
        action='BUY', scores=scores, evaluation={'action': 'BUY', 'score_edge': 0.85},
        zone=zone, learned=learned, cycle_state=state, base_sl=0.6, max_tp=1.2)
    assert not pkg['blocked']
    st = stop_take(2000, 'BUY', 0.1, 0.3, 0.5,
                   kappa=pkg['kappa'], sl_scale=pkg['sl_scale'], range_distance=0.6)
    assert distances_meet_broker_min(st['stop_distance'], st['profit_distance'], 0.5)


def test_bracket_adapt_spam_stays_inside_rails():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for i in range(300):
            pnl = 5.0 if i % 3 else -3.0
            reason = 'tp' if pnl > 0 else 'sl'
            bracket_learn.adapt_after_outcome(
                store, kappa=1.5, sl_scale=1.8, pnl=pnl, exit_reason=reason,
                stop_distance=1.0, volatility=0.5, adverse_path=0.8 if pnl < 0 else 0.1)
            if i % 10 == 9:
                bracket_learn.evolve_from_history(store, min_samples=6)
        mult = bracket_learn.load_multipliers(store)
        assert 0.5 <= mult['kappa'] <= 2.5
        assert 1.0 <= mult['sl_scale'] <= 2.5


# ── permanent lockout attempt ────────────────────────────────────────────────

def test_cannot_permanently_lock_out_strong_edge():
    """After arbitrary losses, a clear neural edge must still be OPEN-able."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for _ in range(500):
            uc.settle(store, pnl=-1.0)
        state = uc.load_state(store)
        scores = {'BUY': 1.0, 'SELL': 0.0, 'HOLD': 0.0}
        pkg = uc.compose_open(
            action='BUY', scores=scores, evaluation={'action': 'BUY'},
            zone={'kappa': 1.0, 'range_distance': 1.0, 'max_tp': 2.0},
            learned=bracket_learn.load_multipliers(store),
            cycle_state=state, base_sl=1.0)
        assert pkg['blocked'] is False
        assert pkg['action'] == 'BUY'


# ── toxic market sim: flash crash + flatline + gap ───────────────────────────

def _toxic_ohlc(n=4000, seed=99):
    rng = np.random.default_rng(seed)
    from datetime import datetime, timedelta, timezone
    px = 2000.0
    t0 = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        # Regimes: flat → flash crash → rip → gap jump
        if i % 500 < 80:
            ret = float(rng.normal(0, 0.02))  # dead market
        elif i % 500 < 100:
            ret = -8.0 - abs(float(rng.normal(0, 2)))  # crash
        elif i % 500 < 150:
            ret = 6.0 + abs(float(rng.normal(0, 1.5)))  # rip
        elif i % 500 == 200:
            ret = 50.0  # gap
        else:
            ret = float(rng.normal(0, 1.2))
        o = px
        c = max(10.0, o + ret)
        h = max(o, c) + abs(float(rng.normal(0, 0.5)))
        l = min(o, c) - abs(float(rng.normal(0, 0.5)))
        at = (t0 + timedelta(minutes=i)).isoformat().replace('+00:00', 'Z')
        rows.append({'at': at, 'open': float(o), 'high': float(h),
                     'low': float(l), 'close': float(c), 'spread': 0.3})
        px = c
    return rows


def test_toxic_market_200_trades_no_nan_state():
    """Drive brain+cycle through crash/gap OHLC; state must stay finite."""
    from app.autonomy import AutoEvolution
    from app.math_rules import ohlc_range_distance

    with sandbox() as d:
        data = Path(d)
        store = Store(data / 'robot.sqlite3')
        brain = AdaptiveBrain(store, data_dir=data, seed=99)
        policy = AutoEvolution(store, data / 'policy.py')
        bars = _toxic_ohlc(5000, seed=99)
        candle_buf = []
        open_pos = None
        n_closed = 0
        target = 200
        TICK, SPREAD, MIN_DIST, EPIC = 0.1, 0.3, 0.5, 'GOLD'

        for i, bar in enumerate(bars):
            if n_closed >= target:
                break
            mid = float(bar['close'])
            candle_buf.append(bar)
            if len(candle_buf) > 200:
                candle_buf = candle_buf[-200:]
            sig = brain.tick(mid, SPREAD, TICK, EPIC, dt=60., bar_time=bar['at'])
            scores = (sig or {}).get('scores')
            if open_pos is not None:
                dirc = open_pos['direction']
                entry, stop_d, profit_d = open_pos['entry'], open_pos['stop_d'], open_pos['profit_d']
                hi, lo = float(bar['high']), float(bar['low'])
                exit_px = exit_reason = None
                if dirc == 'BUY':
                    if lo <= entry - stop_d:
                        exit_px, exit_reason = entry - stop_d, 'sl'
                    elif hi >= entry + profit_d:
                        exit_px, exit_reason = entry + profit_d, 'tp'
                else:
                    if hi >= entry + stop_d:
                        exit_px, exit_reason = entry + stop_d, 'sl'
                    elif lo <= entry - profit_d:
                        exit_px, exit_reason = entry - profit_d, 'tp'
                if exit_px is None and (i - open_pos['bar_i']) >= 45:
                    exit_px, exit_reason = mid, 'manual'
                if exit_px is not None:
                    sign = 1 if dirc == 'BUY' else -1
                    move = sign * (float(exit_px) - entry)
                    if exit_reason == 'sl':
                        pnl = -abs(0.07 * (stop_d / TICK) * 1.0)
                    elif exit_reason == 'tp':
                        pnl = abs(0.07 * (profit_d / TICK) * 1.0)
                    else:
                        pnl = 0.07 * (move / TICK) * 1.0
                    uc.settle(store, pnl=pnl, action=dirc, exit_reason=exit_reason)
                    bracket_learn.record_outcome(
                        store, kappa=open_pos['kappa'], sl_scale=open_pos['sl_scale'],
                        pnl=pnl, currency='GBP', deal_id=f'toxic-{n_closed}',
                        exit_reason=exit_reason, direction=dirc,
                        entry=entry, exit_level=float(exit_px), stop_distance=stop_d)
                    if open_pos.get('transition') is not None and abs(pnl) > 0.05:
                        label = (0 if dirc == 'BUY' else 1) if pnl > 0 else 2
                        brain.learn_from_trade(open_pos['transition'], label, won=pnl > 0)
                    n_closed += 1
                    open_pos = None
                continue
            if not scores or len(candle_buf) < 40:
                continue
            action = max(('BUY', 'SELL', 'HOLD'),
                         key=lambda a: float(scores.get(a, 0) or 0))
            if action not in ('BUY', 'SELL'):
                continue
            zone = bracket_learn.zone_at_open(candle_buf)
            rng = zone['range_distance'] or ohlc_range_distance(candle_buf)
            base_sl = max(MIN_DIST + max(2 * TICK, 2 * SPREAD), float(rng or 0))
            pkg = uc.compose_open(
                action=action, scores=scores,
                evaluation={'action': action, 'score_edge': policy.score_edge(scores)},
                zone=zone, learned=bracket_learn.load_multipliers(store),
                cycle_state=uc.load_state(store), base_sl=base_sl,
                max_tp=zone.get('max_tp'))
            if pkg.get('blocked') or pkg.get('action') not in ('BUY', 'SELL'):
                continue
            assert math.isfinite(pkg['kappa']) and math.isfinite(pkg['sl_scale'])
            st = stop_take(mid, action, TICK, SPREAD, MIN_DIST,
                           kappa=pkg['kappa'], sl_scale=pkg['sl_scale'],
                           range_distance=float(rng) if rng else 0)
            assert distances_meet_broker_min(
                st['stop_distance'], st['profit_distance'], MIN_DIST)
            tr = (brain._json_transition(brain.core.last_transition)
                  if brain.core.last_transition is not None else None)
            open_pos = {
                'direction': action, 'entry': float(mid),
                'stop_d': float(st['stop_distance']),
                'profit_d': float(st['profit_distance']),
                'kappa': pkg['kappa'], 'sl_scale': pkg['sl_scale'],
                'transition': tr, 'bar_i': i,
            }

        state = uc.load_state(store)
        mult = bracket_learn.load_multipliers(store)
        assert n_closed >= 50  # toxic market may slow entries; still must trade
        assert math.isfinite(state['aggression'])
        assert uc.AGGRESSION_MIN <= state['aggression'] <= uc.AGGRESSION_MAX
        assert 0.5 <= mult['kappa'] <= 2.5
        assert 1.0 <= mult['sl_scale'] <= 2.5
        assert brain.core.n >= 8


def test_note_open_spam_counter_sane():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        for _ in range(10_000):
            uc.note_open(store, action='BUY')
        state = uc.load_state(store)
        assert state['opens'] == 10_000
