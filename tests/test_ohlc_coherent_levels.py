"""ATR-coherent SL + shared epic side — no hairline stops / BUY vs SELL chaos."""
import tempfile
from pathlib import Path

import pytest

from app import shared_market as sm
from app.bracket_learn import ATR_STOP_FLOOR_FRAC, swing_anchor_brackets
from tests.test_bracket_learn import _swing_rows


def test_swing_sl_respects_atr_noise_floor():
    """Nearest swing 2pts under entry must NOT become SL when ATR is 4.65."""
    # Flat-ish tape then a tiny dip — nearest low is only ~2 below entry.
    seq = [
        (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 102, 99.8, 100.2),
        (100.2, 105, 100, 104), (104, 108, 103.5, 107), (107, 110, 106.5, 109),
        (109, 109.5, 106, 106.5), (106.5, 107, 104.5, 105), (105, 105.5, 103, 103.5),
        (103.5, 108, 103.2, 107), (107, 111, 106.5, 110), (110, 112, 109.5, 111.5),
        (111.5, 111.8, 110.5, 111), (111, 111.2, 110.2, 110.5),
        # Micro pullback — swing low ~108.5 while entry sits at 110.5
        (110.5, 111.0, 108.5, 110.5), (110.5, 110.8, 110.0, 110.4),
    ]
    rows = _swing_rows(seq)
    entry = 110.5
    atr = 4.65
    zone = {'kappa': 1.0, 'range_distance': 4.0, 'max_tp': 6.0, 'noise_distance': 3.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.01, spread=0.05,
        min_distance=0.2, atr=atr, zone=zone)
    floor = ATR_STOP_FLOOR_FRAC * atr
    if swing.get('ready'):
        assert swing['stop_distance'] + 1e-9 >= floor
        assert swing['stop_level'] <= entry - floor + 1e-9
        assert not swing.get('rr_capped')
    else:
        # Refusing micro-structure / soft-cap is OK — never invent hairline SL.
        assert 'floor' in str(swing.get('reason') or '').lower() or swing.get(
            'reason') in ('no_structural_swings_for_entry',
                          'swing_distances_below_floor',
                          'rr_keep_structural_sl',
                          'rr_unachievable_vs_floor',
                          'rr_fail')


def test_shared_side_keeps_fleet_on_one_side():
    sm.clear()
    a = sm.publish_side('GOLD', 'BUY', source='mtf_preferred', confidence=0.7)
    assert a['side'] == 'BUY'
    # Weak opposite must not flip.
    b = sm.publish_side('GOLD', 'SELL', source='neural', confidence=0.3)
    assert b['side'] == 'BUY'
    got = sm.adopt_side('GOLD')
    assert got['side'] == 'BUY'
    # Strong opposite may flip.
    c = sm.publish_side('GOLD', 'SELL', source='mtf_preferred', confidence=0.8)
    assert c['side'] == 'SELL'
    sm.clear('GOLD')


def test_admin_chart_js_hides_prospective_when_in_trade():
    js = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert 'inTrade' in js
    assert 'Prospective swing brackets only when flat' in js or 'only when flat' in js
    assert 'scaledInTrade' in js
