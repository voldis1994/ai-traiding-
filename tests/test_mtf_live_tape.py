"""MTF must use LIVE QUOTE+HISTORICAL tape — HISTORICAL-only punched holes."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app import mtf_momentum, shared_market
from tests.test_mtf_momentum import _bars


def test_mtf_analysis_uses_closed_quote_minutes():
    """Closed minutes tagged QUOTE must still warm ADX/ATR for signals."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        engine.mandate = {'epic': 'GOLD'}
        engine.market = {'spread': 0.2, 'mid': 2000.0}
        bars = _bars(750, trend=-1.0)
        for i, bar in enumerate(bars):
            # Alternate provenance like LIVE poll before broker rewrite.
            src = 'HISTORICAL' if i % 3 == 0 else 'QUOTE'
            store.candle('GOLD', bar, source=src)
        hist_only = store.candles('GOLD', n=750, historical_only=True) or []
        live = store.candles('GOLD', n=750, historical_only=False) or []
        assert len(hist_only) < len(live)
        # Pure HISTORICAL subset is often too gappy for full MTF ready.
        thin = mtf_momentum.analyze(hist_only, spread=0.2, epic='GOLD')
        analysis = engine._mtf_momentum_analysis()
        assert analysis.get('ready') is True, analysis.get('reason')
        assert analysis.get('stop_hint_atr14') is None or analysis['stop_hint_atr14'] > 0
        adx = ((analysis.get('tf') or {}).get('m15') or {}).get('adx')
        assert adx is None or adx > 0
        # If HIST path was not ready, LIVE path must still have recovered.
        if not thin.get('ready'):
            assert analysis.get('ready') is True


def test_mtf_sticky_keeps_last_ready_on_thin_tick():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        engine.mandate = {'epic': 'GOLD'}
        engine.market = {'spread': 0.2}
        for bar in _bars(750, trend=1.0):
            store.candle('GOLD', bar, source='QUOTE')
        first = engine._mtf_momentum_analysis()
        assert first.get('ready') is True
        # Wipe candles so next analyze is not ready — sticky must hold.
        # Clear shared peer cache so thin-tick path uses engine sticky, not adopt_mtf.
        shared_market.clear('GOLD')
        store.db.execute('DELETE FROM candles')
        store.db.commit()
        sticky = engine._mtf_momentum_analysis()
        assert sticky.get('ready') is True
        assert sticky.get('sticky') is True
        assert sticky.get('mode') == first.get('mode')


def test_chart_overlays_partial_adx_atr_before_full_warm():
    """Admin footer must not wait for 720 M1 before showing ADX/ATR."""
    short = _bars(200, trend=1.0)  # enough M5 for ATR/ADX, below DEFAULT_MIN
    ov = mtf_momentum.chart_overlays(short, epic='GOLD', display_n=60, spread=0.2)
    assert ov.get('ok') is True
    analysis = ov.get('analysis') or {}
    assert analysis.get('ready') is False
    assert ov.get('atr14') is not None and ov['atr14'] > 0
    assert ov.get('adx') is not None and ov['adx'] > 0
    assert ov.get('reason') == 'series_atr_adx_partial'


def test_chart_overlays_atr14_is_m1_not_m5_stop_hint():
    """Desk ATR14 must match M1 Wilder — M5 stop_hint (~larger) must not lie as ATR14."""
    bars = _bars(400, trend=0.8)
    ov = mtf_momentum.chart_overlays(bars, epic='GOLD', display_n=120, spread=0.3)
    assert ov.get('atr14') is not None and ov['atr14'] > 0
    m1 = mtf_momentum.atr_wilder(
        [{'open': b['open'], 'high': b['high'], 'low': b['low'], 'close': b['close']}
         for b in bars], 14)
    assert m1 is not None
    assert ov['atr14'] == pytest.approx(m1, rel=0.02)
    # If M5 hint exists it must be separate — never equal-forced into atr14 when larger.
    hint = ov.get('stop_hint_atr14') or ov.get('atr14_m5')
    if hint is not None and hint > ov['atr14'] * 1.15:
        assert ov['atr14'] < hint
