"""Multi-TF Momentum + Structure + ADX + ATR — start stack for GOLD tests."""
from datetime import datetime, timedelta, timezone

import pytest

from app import mtf_momentum
from app.feature_validation import recommended_live_history_bars, BROKER_PRICE_HISTORY_MAX


def _bars(n=520, *, trend=1.0, start=2000.0, break_down=False):
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
        if break_down and i == n - 1:
            # Hard break below prior 3-candle low.
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


def test_uptrend_prefers_buy_mode():
    a = mtf_momentum.analyze(_bars(520, trend=1.0), spread=0.15)
    assert a['ready'] is True
    assert a['mode'] in ('BUY', 'HOLD', 'SHIFT')  # warm ADX may still settle
    # Strong synthetic uptrend should not land on SELL.
    assert a['preferred'] != 'SELL' or a['mode'] == 'SHIFT'
    assert 'm5' in a['tf'] and 'm15' in a['tf'] and 'm60' in a['tf']
    assert a['tf']['m5']['ema_up'] is True


def test_downtrend_impulse_can_sell():
    a = mtf_momentum.analyze(_bars(520, trend=-1.0), spread=0.15)
    assert a['ready'] is True
    assert a['preferred'] != 'BUY' or a['mode'] == 'SHIFT'
    assert a['tf']['m5']['ema_down'] is True


def test_m1_structure_break_triggers_shift():
    # Build uptrend then break last bar down through structure.
    bars = _bars(520, trend=1.0, break_down=True)
    a = mtf_momentum.analyze(bars, spread=0.15)
    assert a['ready'] is True
    assert a['mode'] == 'SHIFT'
    assert a['preferred'] == 'SELL'
    assert a['tf']['m1']['structure_break'] == 'DOWN'


def test_soft_nudge_tilts_buy():
    scores = {'BUY': 0.33, 'HOLD': 0.34, 'SELL': 0.33}
    analysis = {
        'ready': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.8, 'reason': 'test',
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


def test_ema_and_atr_helpers():
    closes = [float(i) for i in range(1, 30)]
    e = mtf_momentum.ema_series(closes, 5)
    assert len(e) == len(closes)
    assert e[-1] > e[0]
    bars = [{'open': c, 'high': c + 1, 'low': c - 1, 'close': c} for c in closes]
    atr = mtf_momentum.atr_wilder(bars, 14)
    assert atr is not None and atr > 0
    adx = mtf_momentum.adx_di(
        [{'open': c, 'high': c + 2, 'low': c - 0.5, 'close': c + 1}
         for c in range(40, 80)], 14)
    assert adx is not None
    assert adx['adx'] >= 0
