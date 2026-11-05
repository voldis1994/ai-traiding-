"""Live Gold: κ<1 OPEN + flip_loss with spread≈0 → rapid −£0.10 churn."""
import tempfile
from pathlib import Path

import pytest

from app.bracket_learn import (
    MIN_LIVE_RR, MULT_KAPPA_MIN, decide_brackets_from_ohlc, load_multipliers,
    save_multipliers,
)
from app.engine import Store
from app.profit_learning import label_from_profit


def test_live_kappa_floor_is_at_least_one():
    assert MULT_KAPPA_MIN >= 1.0
    assert MIN_LIVE_RR >= 1.0


def test_max_tp_crush_blocks_negative_ev_open():
    """Tiny reachable TP vs wide SL must refuse OPEN, not send κ=0.5."""
    zone = {
        'kappa': 1.5,
        'range_distance': 2.0,
        'max_tp': 0.05,  # quiet M1 reach — live crush case
        'noise_distance': 0.4,
        'ready': True,
    }
    d = decide_brackets_from_ohlc(
        zone, tick=0.01, spread=0.3, min_distance=0.3, tighten=1.0)
    assert d['blocked'] is True
    assert d.get('block_reason')
    # Must not leave a sendable −EV shape as the only outcome.
    if d['stop_distance'] > 0 and not d['blocked']:
        assert d['profit_distance'] / d['stop_distance'] >= MIN_LIVE_RR - 1e-9


def test_reachable_1_to_1_forces_rr_not_half_kappa():
    zone = {
        'kappa': 2.0,
        'range_distance': 1.0,
        'max_tp': 1.0,  # exactly 1:1 reachable
        'noise_distance': 0.5,
        'ready': True,
    }
    d = decide_brackets_from_ohlc(
        zone, tick=0.01, spread=0.05, min_distance=0.1, tighten=1.0)
    assert d['blocked'] is False
    assert d['profit_distance'] + 1e-9 >= d['stop_distance'] * MIN_LIVE_RR
    assert d['kappa'] + 1e-9 >= MIN_LIVE_RR


def test_loss_with_missing_spread_teaches_hold_not_flip():
    """spread=0 used to invent flip_loss_counterfactual_* every few minutes."""
    d = label_from_profit(
        'SELL', -0.12, flip_after_loss=True,
        entry=3950.0, exit_level=3950.2, spread=0.0)
    assert d['label'] == 2  # HOLD
    assert 'hold' in d['mode']
    assert 'buy' not in d['mode'] or d['mode'].endswith('hold')


def test_loss_opposite_must_clear_spread_floor():
    # Tiny opposite mid move inside RT spread → HOLD, not flip.
    d = label_from_profit(
        'BUY', -0.15, flip_after_loss=True,
        entry=100.0, exit_level=99.98, spread=0.40)
    assert d['label'] == 2
    assert d['mode'].endswith('hold')


def test_multiplier_cuts_cannot_go_below_one():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 's.db')
        save_multipliers(store, 1.0, 1.0, reason='seed')
        # Simulate many cut steps via direct save clip.
        save_multipliers(store, 0.4, 1.0, reason='poison')
        assert load_multipliers(store)['kappa'] >= 1.0
