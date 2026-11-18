"""KAMA + ADX/DI + ATR + Pressure + Semivariance — GOLD start stack."""
from datetime import datetime, timedelta, timezone

import pytest

from app import mtf_momentum
from app.feature_validation import recommended_live_history_bars, BROKER_PRICE_HISTORY_MAX


def _bars(n=520, *, trend=1.0, start=2000.0, break_down=False, pressure_sell=False):
    """Synthetic M1 OHLC. trend>0 uptrend, <0 downtrend."""
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    out = []
    px = start
    for i in range(n):
        drift = trend * 0.08
        o = px
        c = px + drift
        h = max(o, c) + 0.15
        l = min(o, c) - 0.15
        if pressure_sell and i >= n - 3:
            # Force recent red bodies (pressure flip toward SELL).
            c = o - abs(drift) - 0.4
            l = c - 0.1
            h = o + 0.05
        if break_down and i == n - 1:
            c = l - 2.0
            l = c - 0.05
            h = o + 0.05
        out.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c, 'spread': 0.2,
        })
        px = c
    return out


def test_insufficient_history_not_ready():
    a = mtf_momentum.analyze(_bars(80), spread=0.2)
    assert a['ready'] is False
    assert a['need_m1'] >= 480
    assert a['mode'] == 'HOLD'


def test_uptrend_kama_buy_path():
    a = mtf_momentum.analyze(_bars(520, trend=1.0), spread=0.15)
    assert a['ready'] is True
    assert a['stack'] == 'kama_adx_atr_pressure'
    assert a['tf']['m5']['kama'] is not None
    assert a['preferred'] != 'SELL' or a['mode'] == 'SHIFT'
    assert a['stop_hint_atr14'] is None or a['stop_hint_atr14'] > 0
    assert 'period_weights' in a
    assert abs(sum(a['period_weights'].values()) - 1.0) < 1e-6


def test_downtrend_can_sell_when_confirmed():
    a = mtf_momentum.analyze(_bars(520, trend=-1.0), spread=0.15)
    assert a['ready'] is True
    assert a['preferred'] != 'BUY' or a['mode'] == 'SHIFT'
    assert a['tf']['m5'].get('kama_down') or a['tf']['m5'].get('ema_down')


def test_structure_or_pressure_triggers_shift():
    bars = _bars(520, trend=1.0, break_down=True, pressure_sell=True)
    a = mtf_momentum.analyze(bars, spread=0.15)
    assert a['ready'] is True
    assert a['mode'] == 'SHIFT'
    assert a['preferred'] == 'SELL'


def test_kama_adapts_vs_ema():
    closes = [100.0 + i * 0.5 for i in range(80)]
    # Add noise in the middle.
    for i in range(30, 50):
        closes[i] = closes[i - 1] + ((-1) ** i) * 0.8
    k = mtf_momentum.kama_series(closes, 10, 2, 30)
    e = mtf_momentum.ema_series(closes, 10)
    assert len(k) == len(closes)
    assert abs(k[-1] - closes[-1]) < abs(e[-1] - closes[-1]) + 5.0  # both track


def test_market_pressure_and_semivariance():
    bars = _bars(40, trend=1.0)
    p = mtf_momentum.pressure_bundle(bars)
    assert p['p3'] is not None and -1.0 <= p['p3'] <= 1.0
    closes = [float(b['close']) for b in bars]
    sv = mtf_momentum.realized_semivariance(closes, 20)
    assert sv['rs_up'] is not None and sv['rs_down'] is not None
    assert sv['rs_share_up'] is None or 0.0 <= sv['rs_share_up'] <= 1.0
    # Uptrend → upside share should dominate.
    assert sv['rs_share_up'] > 0.5


def test_soft_nudge_tilts_buy():
    scores = {'BUY': 0.33, 'HOLD': 0.34, 'SELL': 0.33}
    analysis = {
        'ready': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.8, 'reason': 'test',
        'period_weights': {'m1': 0.2, 'm5': 0.3, 'm15': 0.3, 'm30': 0.1, 'm60': 0.1},
    }
    out, meta = mtf_momentum.soft_nudge_scores(scores, analysis)
    assert meta['nudged'] is True
    assert out['BUY'] > scores['BUY']
    assert abs(sum(out.values()) - 1.0) < 1e-9


def test_shift_lowers_exit_streak():
    assert mtf_momentum.exit_need_streak(
        {'ready': True, 'mode': 'SHIFT'}, default=2) == 1
    assert mtf_momentum.exit_need_streak(
        {'ready': True, 'mode': 'BUY'}, default=2) == 2


def test_recommended_history_floors_for_mtf():
    n = recommended_live_history_bars(None)
    assert n >= mtf_momentum.DEFAULT_MIN_M1_BARS
    assert n <= BROKER_PRICE_HISTORY_MAX


def test_atr_bundle_and_adx():
    bars = [{'open': c, 'high': c + 1, 'low': c - 1, 'close': c} for c in range(40, 80)]
    atrs = mtf_momentum.atr_bundle(bars)
    assert atrs['atr7'] and atrs['atr14'] and atrs['atr21']
    adx = mtf_momentum.adx_di(
        [{'open': c, 'high': c + 2, 'low': c - 0.5, 'close': c + 1}
         for c in range(40, 80)], 14)
    assert adx is not None and adx['adx'] >= 0


def test_mode_presets_clamp_knobs():
    cfg = mtf_momentum._cfg(None)
    assert cfg['mode_preset'] == 'trend_follow'
    assert 'nudge' in cfg and cfg['adx_min'] >= 10
