import pytest
"""SL half of TP (1:2); false flip-loss teach stays gated."""
from app.bracket_learn import MULT_KAPPA_MIN, decide_brackets_from_ohlc
from app.profit_learning import label_from_profit


def test_ohlc_locks_two_to_one_even_when_max_tp_tiny():
    """Even with tiny max_tp, LIVE keeps TP=2×SL (SL half of TP)."""
    zone = {
        'kappa': 1.5,
        'range_distance': 2.0,
        'max_tp': 0.8,
        'noise_distance': 0.4,
        'ready': True,
    }
    d = decide_brackets_from_ohlc(
        zone, tick=0.01, spread=0.05, min_distance=0.1, tighten=1.0)
    assert d['source'] == 'ohlc_zone'
    assert d['stop_distance'] > 0 and d['profit_distance'] > 0
    assert d['profit_distance'] / d['stop_distance'] == pytest.approx(2.0, abs=0.05)
    assert d['kappa'] == pytest.approx(2.0)
    assert d.get('rr_locked') is True
    assert not d.get('blocked')


def test_loss_with_missing_spread_teaches_hold_not_flip():
    d = label_from_profit(
        'SELL', -0.12, flip_after_loss=True,
        entry=3950.0, exit_level=3950.2, spread=0.0)
    assert d['label'] == 2
    assert 'hold' in d['mode']


def test_loss_opposite_must_clear_spread_floor():
    d = label_from_profit(
        'BUY', -0.15, flip_after_loss=True,
        entry=100.0, exit_level=99.98, spread=0.40)
    assert d['label'] == 2
    assert d['mode'].endswith('hold')


def test_mult_kappa_soft_rail_unchanged():
    assert MULT_KAPPA_MIN == 0.5
