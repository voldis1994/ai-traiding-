"""Stale liquidity minute must not freeze OPEN — carry last good volume/DOM vector."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from app.liquidity_feed import LIQUIDITY_NAMES
from app.market_context import FEATURE_DIM
from app.market_fusion import (
    FUSION_DIM, fusion_features, fusion_gap_reason, fusion_liquidity_tail,
)
from tests.test_liquidity_observation import _snapshot
from tests.test_neural_external_fusion import market_state, calendar


BASE = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def _calendar(tmp_path, when=None):
    cal = tmp_path / 'calendar.json'
    calendar(cal, when=when or (BASE + timedelta(hours=1)))
    raw = json.loads(cal.read_text())
    raw['updated_at'] = BASE.isoformat()
    cal.write_text(json.dumps(raw), encoding='utf-8')
    return cal


def test_fusion_uses_liquidity_fallback_when_live_minute_missing(tmp_path):
    cal = _calendar(tmp_path)
    liq = tmp_path / 'liquidity_snapshot.json'
    # Live tape ends 5 minutes before needed bar — would block without fallback.
    stale_end = BASE - timedelta(minutes=5)
    liq.write_text(json.dumps(_snapshot(
        bar=stale_end, epic='GOLD', with_book=False,
        series_end=stale_end, updated_at=BASE)), encoding='utf-8')
    base = {'ready': True, 'bar_time': BASE.isoformat(),
            'features': [0.05] * FEATURE_DIM}
    peers = market_state(bar=BASE)
    assert fusion_features(base, peers, 'GOLD', cal, ['USD'],
                           now=BASE, liquidity_path=liq) is None
    assert fusion_gap_reason(base, peers, 'GOLD', cal, ['USD'],
                             now=BASE, liquidity_path=liq).startswith('liquidity:')

    fallback = [0.1, 0.0, 0.05, 0.2, 0.01, 0.5]
    assert len(fallback) == len(LIQUIDITY_NAMES)
    vector = fusion_features(base, peers, 'GOLD', cal, ['USD'],
                             now=BASE, liquidity_path=liq,
                             liquidity_fallback=fallback)
    assert vector is not None
    assert len(vector) == FUSION_DIM
    assert vector[-len(LIQUIDITY_NAMES):] == fallback
    assert fusion_gap_reason(base, peers, 'GOLD', cal, ['USD'],
                             now=BASE, liquidity_path=liq,
                             liquidity_fallback=fallback) is None


def test_fusion_liquidity_tail_roundtrip(tmp_path):
    cal = _calendar(tmp_path)
    liq = tmp_path / 'liquidity_snapshot.json'
    liq.write_text(json.dumps(_snapshot(bar=BASE, epic='GOLD')), encoding='utf-8')
    base = {'ready': True, 'bar_time': BASE.isoformat(),
            'features': [0.05] * FEATURE_DIM}
    fused = fusion_features(base, market_state(bar=BASE), 'GOLD', cal, ['USD'],
                            now=BASE, liquidity_path=liq)
    assert fused and len(fused) == FUSION_DIM
    tail = fusion_liquidity_tail(fused, ohlc_dim=FEATURE_DIM)
    assert tail is not None and len(tail) == len(LIQUIDITY_NAMES)


def test_engine_carries_liquidity_across_gap_bar(tmp_path):
    from app.engine import Store, AdaptiveBrain, SingleTradeEngine

    st = Store(tmp_path / 'db.sqlite3')
    brain = AdaptiveBrain(st, data_dir=tmp_path)
    eng = SingleTradeEngine(st, brain)
    eng._last_good_liquidity = [0.11, -0.02, 0.03, 0.1, 0.0, 0.67]
    eng.mandate = {'epic': 'GOLD', 'size': '0.02'}
    eng.market_context = {
        'ready': True, 'bar_time': BASE.isoformat(),
        'features': [0.01] * FEATURE_DIM, 'close': 4000.0,
    }
    cal = _calendar(tmp_path)
    eng.news_calendar_path = cal
    # Empty / missing live liquidity file.
    eng.liquidity_path = tmp_path / 'missing_liquidity.json'
    from tests.test_neural_external_fusion import market_state as ms
    eng.intermarket.state = ms(bar=BASE)
    fused = fusion_features(
        eng.market_context, eng.intermarket.state, 'GOLD', cal, ['USD'],
        now=BASE, expected_ohlc_dim=eng.brain.ohlc_dim,
        liquidity_path=eng.liquidity_path,
        liquidity_fallback=eng._last_good_liquidity)
    assert fused is not None
    # Simulate poll success with carried liquidity — OPEN must not stay blocked.
    eng.latest_fusion_ready = True
    eng.fusion_block_reason = None
    brain.feature_schema = brain.fusion_schema
    assert eng._fusion_entry_blocked() is False
    reason = eng._compose_entry_block_reason(
        readiness={'ready': True}, evaluation=None, decision='BUY')
    assert 'liquidity' not in (reason or '').lower()
