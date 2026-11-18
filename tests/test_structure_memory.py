"""Swing BOS/CHoCH + post-trade structure memory gate."""
from datetime import datetime, timedelta, timezone

from app import structure_memory as sm
from app.bracket_learn import resample_m1_ohlc
from app import reentry_confirm as rc


def _up_trend(n=40, start=2650.0):
    rows = []
    px = start
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    for i in range(n):
        o = px
        px = px + 0.4
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': px + 0.15, 'low': o - 0.05, 'close': px,
        })
    return rows


def test_swing_pivots_find_turns():
    # Up then down — clear swing high in the middle.
    rows = []
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    px = 100.0
    for i in range(20):
        o = px
        if i < 10:
            px += 1.0
        else:
            px -= 1.0
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': max(o, px) + 0.2, 'low': min(o, px) - 0.2,
            'close': px,
        })
    piv = sm.swing_pivots(rows, left=2, right=2)
    assert piv['ready'] is True
    assert piv['last_sh'] is not None
    assert piv['last_sh']['i'] >= 8


def test_bos_up_after_swing_break():
    """Deterministic swing path: two SH/SL then close above last SH → BOS_UP."""
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    seq = [
        (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 102, 99.8, 100.2),
        (100.2, 105, 100, 104), (104, 108, 103.5, 107), (107, 110, 106.5, 109),
        (109, 109.5, 106, 106.5), (106.5, 107, 104.5, 105), (105, 105.5, 103, 103.5),
        (103.5, 108, 103.2, 107), (107, 111, 106.5, 110), (110, 112, 109.5, 111.5),
        (111.5, 111.8, 110.5, 111), (111, 111.2, 110.2, 110.5),
        (110.5, 113, 110.3, 112.8), (112.8, 114, 112.5, 113.5),
    ]
    rows = [{
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': o, 'high': h, 'low': l, 'close': c,
    } for i, (o, h, l, c) in enumerate(seq)]
    bos = sm.bos_choch(rows, left=2, right=2)
    assert bos.get('event') == 'BOS_UP'
    assert bos.get('range_hi') == 112.0
    assert bos.get('last_close') == 113.5


def test_missing_prior_range_holds_same_dir():
    prior = {'structure': 'RANGE', 'mid': 100.0}  # no range_hi/lo
    live = {'structure': 'RANGE', 'mid': 100.1, 'live_event': None}
    out = sm.developed_since(prior, live, action='BUY', atr=1.0)
    assert out['ok'] is False
    assert out['reason'] == 'need_structure_range'


def test_same_range_blocks_reentry():
    prior = {
        'structure': 'RANGE', 'range_hi': 2652.0, 'range_lo': 2648.0,
        'mid': 2650.0, 'event': None,
    }
    live = {
        'structure': 'RANGE', 'mid': 2650.2, 'live_event': None,
        'event': None, 'live_retest': None, 'false_break': False,
        'range_hi': 2651.5, 'range_lo': 2648.5,
    }
    out = sm.developed_since(prior, live, action='BUY', atr=2.0)
    assert out['needed'] is True
    assert out['ok'] is False
    assert out['reason'] == 'same_range_no_new_structure'


def test_bos_allows_reentry():
    prior = {
        'structure': 'RANGE', 'range_hi': 2652.0, 'range_lo': 2648.0,
        'mid': 2650.0,
    }
    live = {
        'structure': 'BREAKOUT_UP', 'mid': 2654.0, 'live_event': 'BOS_UP',
        'event': 'BOS_UP', 'live_retest': None, 'false_break': False,
        'range_hi': 2652.0, 'range_lo': 2648.0,
    }
    out = sm.developed_since(prior, live, action='BUY')
    assert out['ok'] is True
    assert 'bos' in out['reason'] or out['reason'] == 'new_bos_up'


def test_gate_reentry_same_range_via_last_outcome():
    last = {
        'direction': 'BUY',
        'structure_at_close': {
            'structure': 'RANGE', 'range_hi': 100.5, 'range_lo': 99.5,
            'mid': 100.0, 'event': None,
        },
    }
    # Live candles still inside that range.
    t0 = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(20):
        candles.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': 100.0, 'high': 100.3, 'low': 99.7, 'close': 100.05,
        })
    gate = sm.gate_reentry(
        action='BUY', last_outcome=last, candles=candles,
        market_context={'mid': 100.1, 'close': 100.1}, atr=1.0)
    assert gate['needed'] is True
    assert gate['ok'] is False


def test_reentry_evaluate_holds_same_range():
    scores_raw = {'BUY': 0.55, 'SELL': 0.20, 'HOLD': 0.25}
    last = {
        'direction': 'BUY', 'exit_mid': 100.0,
        'structure_at_close': {
            'structure': 'RANGE', 'range_hi': 100.5, 'range_lo': 99.5,
            'mid': 100.0,
        },
    }
    t0 = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    candles = [{
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': 100.0, 'high': 100.2, 'low': 99.8, 'close': 100.05,
    } for i in range(25)]
    # structure_break lets same_dir ATR gate pass; structure_memory still HOLDs.
    out = rc.evaluate(
        action='BUY', scores_raw=scores_raw, last_outcome=last,
        market_context={'trend': 'UP', 'close': 100.1, 'mid': 100.1},
        mtf_analysis={'ready': True, 'stop_hint_atr14': 1.0,
                      'structure_break': 'UP', 'preferred': 'BUY', 'mode': 'BUY'},
        candles=candles)
    assert out['allowed'] is False
    assert out['reason'] == 'same_range_no_new_structure'
    assert (out.get('structure_memory') or {}).get('ok') is False


def test_calendar_resample_skips_gapped_bucket():
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        if i == 2:  # hole inside first M5 bucket 12:00–12:04
            continue
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': 100 + i, 'high': 100.5 + i, 'low': 99.5 + i,
            'close': 100.2 + i, 'spread': 0.05,
        })
    m5 = resample_m1_ohlc(rows, 5)
    # First calendar bucket incomplete → skipped; later complete buckets may exist.
    starts = [r.get('bucket_start') for r in m5]
    # 12:00 UTC epoch bucket must be absent.
    first_key = int(t0.timestamp()) // 60
    first_bucket = (first_key // 5) * 5
    assert first_bucket not in starts


def test_calendar_resample_aligned_series_counts():
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    rows = []
    px = 100.0
    for i in range(60):
        o = px
        px += 0.1
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': px + 0.05, 'low': o - 0.05, 'close': px,
            'spread': 0.05,
        })
    assert len(resample_m1_ohlc(rows, 5)) == 12
    assert len(resample_m1_ohlc(rows, 15)) == 4
    assert len(resample_m1_ohlc(rows, 60)) == 1
