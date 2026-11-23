"""OHLC swing SL/TP + shared epic side — candles only, no ATR invent."""
from pathlib import Path

from app import shared_market as sm
from app.bracket_learn import swing_anchor_brackets
from tests.test_bracket_learn import _swing_rows


def test_swing_sl_uses_ohlc_candle_not_atr_formula():
    """Nearest swing low is the SL — ATR must not invent entry−0.5×ATR."""
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
    atr = 4.65  # old bug: invent entry − 0.5×ATR
    zone = {'kappa': 1.0, 'range_distance': 4.0, 'max_tp': 6.0, 'noise_distance': 3.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.01, spread=0.05,
        min_distance=0.2, atr=atr, zone=zone)
    assert swing.get('ready') is True
    assert float(swing['stop_level']) <= 108.5 + 1e-6
    assert abs(float(swing['stop_level']) - (entry - 0.5 * atr)) > 0.05


def test_screenshot_style_sl_must_not_be_half_atr():
    """Gold screenshot: SL 3639.69 = entry−6.82 ≈ 0.5×ATR — must not recur."""
    entry = 3646.51
    atr = 13.64
    fantasy = entry - 0.5 * atr
    seq = [
        (3640, 3650, 3638, 3645), (3645, 3655, 3642, 3652),
        (3652, 3660, 3648, 3658), (3658, 3662, 3650, 3655),
        (3655, 3658, 3644, 3648), (3648, 3652, 3643, 3647),
        (3647, 3650, 3641, 3646), (3646, 3649, 3640, 3645),
        (3645, 3648, 3639, 3644), (3644, 3650, 3642, 3648),
        (3648, 3655, 3645, 3652), (3652, 3658, 3648, 3654),
        (3654, 3656, 3649, 3651), (3651, 3653, 3647, 3649),
        (3649, 3651, 3644, 3646.51), (3646.51, 3648, 3645, 3646.8),
    ]
    rows = _swing_rows(seq)
    zone = {'kappa': 2.0, 'range_distance': 8.0, 'max_tp': 20.0, 'noise_distance': 5.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.01, spread=0.1,
        min_distance=0.5, atr=atr, zone=zone)
    if swing.get('ready'):
        assert abs(float(swing['stop_level']) - fantasy) > 0.2


def test_shared_side_keeps_fleet_on_one_side():
    sm.clear()
    a = sm.publish_side('GOLD', 'BUY', source='mtf_preferred', confidence=0.7)
    assert a['side'] == 'BUY'
    b = sm.publish_side('GOLD', 'SELL', source='neural', confidence=0.3)
    assert b['side'] == 'BUY'
    got = sm.adopt_side('GOLD')
    assert got['side'] == 'BUY'
    c = sm.publish_side('GOLD', 'SELL', source='mtf_preferred', confidence=0.8)
    assert c['side'] == 'SELL'
    sm.clear('GOLD')


def test_admin_chart_js_hides_prospective_when_in_trade():
    js = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert 'inTrade' in js
    assert 'Prospective swing brackets only when flat' in js or 'only when flat' in js
    assert 'scaledInTrade' in js
