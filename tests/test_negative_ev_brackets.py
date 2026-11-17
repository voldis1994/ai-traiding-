"""No TP/SL hardcode — OHLC decides; only false flip-loss teach is gated."""
from app.bracket_learn import MULT_KAPPA_MIN, decide_brackets_from_ohlc
from app.profit_learning import label_from_profit


def test_ohlc_decides_without_forced_one_to_one():
    """Tiny max_tp may yield TP<SL — market/OHLC call, not our hardcoded ratio."""
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
    assert not d.get('reshaped')
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
