"""1m guide: held retest vs fakeout must not be conflated; FAKEOUT TP uses mid."""
from datetime import datetime, timedelta, timezone

from app import structure_memory as sm
from app import ohlc_scenarios as osc
from app import ohlc_regime_brackets as orb


def _bars(seq):
    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    return [{
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': o, 'high': h, 'low': l, 'close': c,
    } for i, (o, h, l, c) in enumerate(seq)]


def _range_then(extra):
    """Deterministic swing range: last SH≈112, last SL≈103 (≥16 bars for classify)."""
    base = [
        (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5),
        (101.5, 102, 99.8, 100.2), (100.2, 105, 100, 104),
        (104, 108, 103.5, 107), (107, 110, 106.5, 109),
        (109, 109.5, 106, 106.5), (106.5, 107, 104.5, 105),
        (105, 105.5, 103, 103.5), (103.5, 108, 103.2, 107),
        (107, 111, 106.5, 110), (110, 112, 109.5, 111.5),
        (111.5, 111.8, 110.5, 111.0), (111.0, 111.2, 110.2, 110.5),
        (110.5, 110.8, 110.0, 110.3), (110.3, 110.6, 109.8, 110.1),
    ]
    return _bars(base + list(extra))


def test_fakeout_does_not_also_tag_retest():
    """Pierce then close back inside → FAKEOUT only, not RETEST."""
    # last SH will be near 112; wick above then close clearly inside range.
    rows = _range_then([
        (110.5, 114.5, 110.3, 110.8),  # pierce high, close back inside
    ])
    bos = sm.bos_choch(rows, left=2, right=2)
    assert bos.get('false_break') is True
    assert bos.get('retest') is None
    sc = osc.classify(rows, tick=0.1)
    assert sc.get('scenario') == 'FAKEOUT'
    assert 'RETEST_UP' not in (sc.get('scenarios') or [])
    assert 'RETEST_DOWN' not in (sc.get('scenarios') or [])


def test_held_retest_after_bos_sets_retest_not_fakeout():
    """Close beyond level, pullback that holds outside → RETEST, not FAKEOUT."""
    rows = _range_then([
        (110.5, 114.0, 110.3, 113.5),  # BOS close above SH
        (113.5, 113.8, 112.0, 112.6),  # retest hold above broken level
    ])
    bos = sm.bos_choch(rows, left=2, right=2)
    assert bos.get('false_break') is False
    assert bos.get('retest') == 'UP'
    sc = osc.classify(rows, tick=0.1)
    assert sc.get('scenario') == 'RETEST_UP'
    assert sc.get('fakeout') is False


def test_fakeout_tp_includes_range_mid():
    sc = {
        'ready': True, 'scenario': 'FAKEOUT', 'entry_hint': 'BUY',
        'range': {'lo': 100.0, 'hi': 110.0, 'span': 10.0},
    }
    rows = _bars([
        (104, 104.3, 103.7, 104.0),
        (104, 104.2, 99.5, 103.8),  # sweep wick
        (103.8, 104.5, 103.5, 104.2),
    ] * 8)
    br = orb.regime_brackets(
        candles=rows, entry=104.0, action='BUY', scenario=sc,
        trigger='zone', tick=0.1, spread=0.2, min_distance=0.5, atr=1.0)
    assert br.get('ready') is True
    assert float(br['tp1_level']) >= 104.5
    assert abs(float(br['tp1_level']) - 105.0) < 0.6 or float(br['tp2_level']) >= 109.0
