"""Mid-bar retry: zone/confirm wait must not freeze until next closed M1."""
import tempfile
from pathlib import Path

from app.engine import AdaptiveBrain, SingleTradeEngine, Store


def test_entry_wait_reason_retryable():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        assert engine._entry_wait_reason_retryable(
            'Neural HOLD · wait_buy_entry_zone (price 3720 > zona 3715)') is True
        assert engine._entry_wait_reason_retryable(
            'medium_need_m1_confirm') is True
        assert engine._entry_wait_reason_retryable(
            'raw_disagrees_nudged') is True
        assert engine._entry_wait_reason_retryable(
            'day_book capacity full') is False


def test_mtf_soft_hold_allows_zone_check():
    from app import ohlc_entry_zones
    ok, reason = ohlc_entry_zones._mtf_allows_side(
        'BUY', {'ready': True, 'mode': 'HOLD', 'preferred': 'HOLD',
                'confidence': 0.3, 'reason': 'noise'})
    assert ok is True
    assert reason == 'mtf_hold_soft'
    blocked, _ = ohlc_entry_zones._mtf_allows_side(
        'BUY', {'ready': True, 'mode': 'SELL', 'preferred': 'SELL',
                'confidence': 0.80, 'reason': 'trend'})
    assert blocked is False
