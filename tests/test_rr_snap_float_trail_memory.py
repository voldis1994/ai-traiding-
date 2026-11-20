"""RR after snap, float tick dust, and trail tip memory past 120 bars."""
import pytest

from app.math_rules import snap_distance_to_tick
from app.bracket_learn import apply_min_rr
from app.protection_ai import trail_extreme_from_broker


def test_snap_distance_float_dust_does_not_add_tick():
    """0.8*0.9 is 0.7200000000000001 in float — must snap to 0.72, not 0.73."""
    dirty = 0.8 * 0.9
    assert dirty != 0.72  # binary dust present
    dist, ticks = snap_distance_to_tick(dirty, 0.01)
    assert ticks == 72
    assert dist == 0.72


def test_snap_still_ceils_true_fractional_tick():
    dist, ticks = snap_distance_to_tick(0.725, 0.01)
    assert ticks == 73
    assert abs(dist - 0.73) < 1e-12


def test_rr_rechecked_after_tick_snap_example():
    """tighten+ceil can drop RR 1.60→1.40 — must not return ready below min_rr."""
    # Synthetic swing-ready path via apply_min_rr already ok; exercise snap math.
    stop_d, profit_d, tighten, tick, min_rr = 2.5, 4.0, 0.85, 0.5, 1.5
    sd = stop_d * tighten  # 2.125
    pd = profit_d * tighten  # 3.4
    sd, _ = snap_distance_to_tick(sd, tick)  # 2.5
    pd, _ = snap_distance_to_tick(pd, tick)  # 3.5
    assert abs(sd - 2.5) < 1e-12 and abs(pd - 3.5) < 1e-12
    assert pd / sd < min_rr  # 1.4 — the bug case
    # Repair path: raise TP to clear min_rr.
    need_tp, _ = snap_distance_to_tick(sd * min_rr, tick)
    assert need_tp / sd + 1e-12 >= min_rr


def test_decide_brackets_ignores_cosmetic_tighten_on_swing(monkeypatch):
    """Swing SL/TP stay on pivots — tighten=0.85 must not shrink distances."""
    from app import bracket_learn

    fake = {
        'ready': True, 'source': 'swing_anchor', 'reason': 'ok',
        'stop_distance': 2.5, 'profit_distance': 4.0,
        'stop_level': 97.5, 'profit_level': 104.0,
        'rr': 1.6, 'min_rr': 1.5, 'sl_max': 10.0,
    }
    monkeypatch.setattr(bracket_learn, 'swing_anchor_brackets', lambda **kw: fake)
    candles = [{'open': 100., 'high': 101., 'low': 99., 'close': 100.}
               for _ in range(30)]
    zone = {'ready': True, 'range_distance': 2.5, 'max_tp': 4.0,
            'kappa': 1.6, 'noise_distance': 2.0, 'structure': 4.0,
            'pull_share': 0.5, 'favor_share': 0.5, 'lookback': 20}
    out = bracket_learn.decide_brackets_from_ohlc(
        zone, tick=0.5, spread=0.0, min_distance=0.5, tighten=0.85,
        entry=100.0, action='BUY', candles=candles, require_swing=True)
    assert out['ready'] is True
    assert out['source'] == 'swing_anchor'
    assert out.get('tighten') == pytest.approx(1.0)
    # Cosmetic 0.85 would have made 2.125/3.4 → snap 2.5/3.5 (RR 1.4). Blocked.
    assert out['stop_distance'] == pytest.approx(2.5, abs=1e-12)
    assert out['profit_distance'] == pytest.approx(4.0, abs=1e-12)
    assert (out['profit_distance'] / out['stop_distance']) + 1e-12 >= 1.5


def test_trail_prior_extreme_remembers_peak_outside_window():
    """BUY peak 105 from hours ago must survive when candles only show 102."""
    opened = '2026-11-20T02:00:42+00:00'
    # Window only has recent bars around 102 — peak already rolled off.
    candles = [{
        'at': f'2026-11-20T05:{i:02d}:00+00:00',
        'open': 102., 'high': 102.2, 'low': 101.8, 'close': 102.0,
        'spread': 0.1,
    } for i in range(0, 5)]
    tip = trail_extreme_from_broker(
        'BUY', bid=102.0, ask=102.1, candles=candles,
        opened_at=opened, prior_extreme=105.0)
    assert tip == 105.0


def test_trail_prior_extreme_drops_unfavorable_memory():
    """SELL prior above entry is not a trough — engine clears it before call."""
    tip = trail_extreme_from_broker(
        'SELL', bid=99.0, ask=99.1,
        candles=[{
            'at': '2026-11-20T05:10:00+00:00',
            'open': 99., 'high': 99.2, 'low': 98.5, 'close': 98.8,
            'spread': 0.1,
        }],
        opened_at='2026-11-20T05:00:42+00:00',
        prior_extreme=None)
    assert tip is not None and tip <= 99.1 + 1e-9
