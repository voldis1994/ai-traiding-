"""Public multi-source gold volume/DOM features — never invent missing books."""
from datetime import datetime, timedelta, timezone
import json
import math

from app.liquidity_feed import (
    LIQUIDITY_NAMES, build_liquidity_features, _parse_coinbase_book,
    _parse_coinbase_candles, _parse_yahoo_chart, _zscore,
)
from app.market_context import FEATURE_DIM
from app.market_fusion import EXTERNAL_NAMES, FUSION_DIM, fusion_features


BASE = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _vol_bars(n=40, start=None, base_vol=100.0):
    start = start or (BASE - timedelta(minutes=40))
    bars = []
    for i in range(n):
        t = start + timedelta(minutes=i)
        bars.append({
            't': t.replace(second=0, microsecond=0).isoformat(),
            'ts': int(t.timestamp()),
            'volume': float(base_vol + (i % 7) * 3),
            'close': 4000.0 + i * 0.1,
        })
    return bars


def _snapshot(*, bar=BASE, extra_vol=0.0):
    start = bar - timedelta(minutes=39)
    bars_a = _vol_bars(start=start)
    bars_b = _vol_bars(start=start, base_vol=50.0)
    # Spike the target bar volume for a readable z.
    target = bar.replace(second=0, microsecond=0).isoformat()
    for series in (bars_a, bars_b):
        for b in series:
            if b['t'] == target:
                b['volume'] += extra_vol
    return {
        'ok': True,
        'updated_at': bar.isoformat(),
        'gc_f': {'symbol': 'GC=F', 'source': 'yahoo_finance', 'bars': bars_a},
        'paxg_coinbase': {'symbol': 'PAXG-USD', 'source': 'coinbase', 'bars': bars_b},
        'paxg_book': {
            'source': 'coinbase', 'symbol': 'PAXG-USD',
            'bid': 4000.0, 'ask': 4001.0,
            'imbalance': 0.25, 'micro_bps': 0.1, 'spread_bps': 2.5, 'levels': 10,
        },
        'sources': ['yahoo:GC=F', 'coinbase:PAXG-USD', 'coinbase:book'],
        'errors': [],
    }


def test_liquidity_names_extend_external_tail():
    assert LIQUIDITY_NAMES == EXTERNAL_NAMES[-len(LIQUIDITY_NAMES):]
    assert len(EXTERNAL_NAMES) == 14
    assert FUSION_DIM == FEATURE_DIM + 14


def test_build_features_multi_source_and_book():
    snap = _snapshot(extra_vol=80.0)
    feats = build_liquidity_features(snap, BASE.isoformat(), now=BASE)
    assert feats is not None and len(feats) == 6
    assert all(math.isfinite(x) for x in feats)
    assert -1 <= feats[0] <= 1 and -1 <= feats[1] <= 1
    assert feats[3] == 0.25
    assert feats[5] == 1.0  # primary + confirm + book


def test_missing_book_or_volume_returns_none():
    snap = _snapshot()
    del snap['paxg_book']
    assert build_liquidity_features(snap, BASE.isoformat(), now=BASE) is None
    snap = _snapshot()
    snap['gc_f'] = None
    snap['paxg_coinbase'] = None
    assert build_liquidity_features(snap, BASE.isoformat(), now=BASE) is None


def test_stale_snapshot_rejected():
    snap = _snapshot()
    later = BASE + timedelta(minutes=10)
    assert build_liquidity_features(snap, BASE.isoformat(), now=later) is None


def test_fusion_requires_liquidity_path(tmp_path):
    from tests.test_neural_external_fusion import market_state, calendar
    cal = tmp_path / 'calendar.json'
    # Calendar event timeline must match fusion `now` (BASE here).
    calendar(cal, when=BASE + timedelta(hours=1))
    # Patch calendar updated_at to BASE so assess_news freshness passes.
    raw = json.loads(cal.read_text())
    raw['updated_at'] = BASE.isoformat()
    cal.write_text(json.dumps(raw), encoding='utf-8')
    liq = tmp_path / 'liquidity_snapshot.json'
    liq.write_text(json.dumps(_snapshot(bar=BASE)), encoding='utf-8')
    base = {'ready': True, 'bar_time': BASE.isoformat(), 'features': [0.05] * FEATURE_DIM}
    assert fusion_features(base, market_state(bar=BASE), 'GOLD', cal, ['USD'],
                           now=BASE) is None  # no liquidity_path
    vector = fusion_features(base, market_state(bar=BASE), 'GOLD', cal, ['USD'],
                             now=BASE, liquidity_path=liq)
    assert vector and len(vector) == FUSION_DIM


def test_parsers_yahoo_coinbase_book():
    yahoo = {
        'chart': {'result': [{
            'meta': {'regularMarketTime': int(BASE.timestamp())},
            'timestamp': [int((BASE - timedelta(minutes=i)).timestamp()) for i in range(25, 0, -1)],
            'indicators': {'quote': [{
                'volume': [10.0 + i for i in range(25)],
                'close': [4000.0] * 25,
            }]},
        }]}
    }
    series = _parse_yahoo_chart(yahoo, symbol='GC=F')
    assert len(series['bars']) >= 20
    candles = [[int((BASE - timedelta(minutes=i)).timestamp()), 1, 2, 1, 2, 0.5 + i * 0.01]
               for i in range(30, 0, -1)]
    cb = _parse_coinbase_candles(candles)
    assert len(cb['bars']) >= 20
    book = _parse_coinbase_book({
        'bids': [['4000', '1.0', 1], ['3999', '2.0', 1]],
        'asks': [['4001', '1.5', 1], ['4002', '0.5', 1]],
    })
    assert -1 <= book['imbalance'] <= 1
    assert _zscore([1, 1, 1], 1) == 0.0 or _zscore(list(range(30)), 40) is not None
