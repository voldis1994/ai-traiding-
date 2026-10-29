"""Global public volume/DOM routes — per Capital instrument, never invent."""
from datetime import datetime, timedelta, timezone
import json
import math

from app.liquidity_feed import (
    LIQUIDITY_NAMES, build_liquidity_features, resolve_liquidity_routes,
    _parse_coinbase_book, _parse_coinbase_candles, _parse_yahoo_chart, _zscore,
)
from app.instrument_profile import instrument_profile
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


def _snapshot(*, bar=BASE, epic='GOLD', extra_vol=0.0, with_book=True):
    start = bar - timedelta(minutes=39)
    bars_a = _vol_bars(start=start)
    bars_b = _vol_bars(start=start, base_vol=50.0)
    target = bar.replace(second=0, microsecond=0).isoformat()
    for series in (bars_a, bars_b):
        for b in series:
            if b['t'] == target:
                b['volume'] += extra_vol
    snap = {
        'ok': True,
        'epic': epic,
        'updated_at': bar.isoformat(),
        'routes': {'label': 'TEST', 'yahoo': ['GC=F'], 'mapped': True},
        'series': [
            {'symbol': 'GC=F', 'source': 'yahoo_finance', 'bars': bars_a},
            {'symbol': 'PAXG-USD', 'source': 'coinbase', 'bars': bars_b},
        ],
        'book': None,
        'sources': ['yahoo:GC=F', 'coinbase:PAXG-USD'],
        'errors': [],
    }
    if with_book:
        snap['book'] = {
            'source': 'coinbase', 'symbol': 'PAXG-USD',
            'bid': 4000.0, 'ask': 4001.0,
            'imbalance': 0.25, 'micro_bps': 0.1, 'spread_bps': 2.5, 'levels': 10,
        }
        snap['sources'].append('coinbase:book')
    return snap


def test_liquidity_names_extend_external_tail():
    assert LIQUIDITY_NAMES == EXTERNAL_NAMES[-len(LIQUIDITY_NAMES):]
    assert len(EXTERNAL_NAMES) == 14
    assert FUSION_DIM == FEATURE_DIM + 14


def test_routes_cover_major_capital_kinds():
    gold = resolve_liquidity_routes('GOLD', instrument_profile('GOLD', {'name': 'Gold'}))
    assert gold['mapped'] and 'GC=F' in gold['yahoo'] and gold['coinbase'] == 'PAXG-USD'

    eurusd = resolve_liquidity_routes(
        'CS.D.EURUSD.CFD.IP', instrument_profile('CS.D.EURUSD.CFD.IP', {'name': 'EUR/USD'}))
    assert eurusd['mapped'] and '6E=F' in eurusd['yahoo']

    btc = resolve_liquidity_routes('BTCUSD', instrument_profile('BTCUSD', {'name': 'Bitcoin'}))
    assert btc['mapped'] and btc['coinbase'] == 'BTC-USD' and btc['okx'] == 'BTC-USDT'

    us100 = resolve_liquidity_routes('US100', instrument_profile('US100', {'name': 'US Tech 100'}))
    assert us100['mapped'] and 'NQ=F' in us100['yahoo']

    wti = resolve_liquidity_routes('OIL_CRUDE', instrument_profile('OIL_CRUDE', {'name': 'US Crude'}))
    assert wti['mapped'] and 'CL=F' in wti['yahoo']

    silver = resolve_liquidity_routes('SILVER', instrument_profile('SILVER', {'name': 'Silver'}))
    assert silver['mapped'] and 'SI=F' in silver['yahoo']


def test_build_features_multi_source_and_optional_book():
    snap = _snapshot(extra_vol=80.0, with_book=True)
    feats = build_liquidity_features(snap, BASE.isoformat(), now=BASE, epic='GOLD')
    assert feats is not None and len(feats) == 6
    assert feats[3] == 0.25 and feats[5] == 1.0

    # Book optional — volume-only still works with lower coverage.
    snap2 = _snapshot(with_book=False)
    feats2 = build_liquidity_features(snap2, BASE.isoformat(), now=BASE, epic='GOLD')
    assert feats2 is not None and feats2[3] == 0.0 and feats2[5] < 1.0


def test_wrong_epic_or_missing_volume_returns_none():
    snap = _snapshot(epic='GOLD')
    assert build_liquidity_features(snap, BASE.isoformat(), now=BASE, epic='EURUSD') is None
    snap['series'] = []
    assert build_liquidity_features(snap, BASE.isoformat(), now=BASE, epic='GOLD') is None


def test_stale_snapshot_rejected():
    snap = _snapshot()
    later = BASE + timedelta(minutes=10)
    assert build_liquidity_features(snap, BASE.isoformat(), now=later, epic='GOLD') is None


def test_fusion_requires_liquidity_path(tmp_path):
    from tests.test_neural_external_fusion import market_state, calendar
    cal = tmp_path / 'calendar.json'
    calendar(cal, when=BASE + timedelta(hours=1))
    raw = json.loads(cal.read_text())
    raw['updated_at'] = BASE.isoformat()
    cal.write_text(json.dumps(raw), encoding='utf-8')
    liq = tmp_path / 'liquidity_snapshot.json'
    liq.write_text(json.dumps(_snapshot(bar=BASE, epic='GOLD')), encoding='utf-8')
    base = {'ready': True, 'bar_time': BASE.isoformat(), 'features': [0.05] * FEATURE_DIM}
    assert fusion_features(base, market_state(bar=BASE), 'GOLD', cal, ['USD'],
                           now=BASE) is None
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
    cb = _parse_coinbase_candles(candles, product='BTC-USD')
    assert len(cb['bars']) >= 20
    book = _parse_coinbase_book({
        'bids': [['4000', '1.0', 1], ['3999', '2.0', 1]],
        'asks': [['4001', '1.5', 1], ['4002', '0.5', 1]],
    }, product='BTC-USD')
    assert -1 <= book['imbalance'] <= 1
    assert _zscore(list(range(30)), 40) is not None
