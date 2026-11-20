"""Trail must not use pre-open prices from the opening minute candle."""
from datetime import datetime, timezone, timedelta

from app.protection_ai import trail_extreme_from_broker, _open_minute_cutoff
from app.market_context import event_time, candle_time


def test_event_time_keeps_seconds():
    raw = '2026-11-20T05:05:42+00:00'
    assert event_time(raw).second == 42
    assert candle_time(raw).second == 0


def test_open_minute_cutoff_floors_to_open_bar():
    opened = datetime(2026, 11, 20, 5, 5, 42, tzinfo=timezone.utc)
    cut = _open_minute_cutoff(opened)
    assert cut == datetime(2026, 11, 20, 5, 5, 0, tzinfo=timezone.utc)


def test_trail_skips_opening_minute_preopen_spike():
    """BUY: opening-minute high before fill must not inflate trail tip."""
    opened = datetime(2026, 11, 20, 5, 5, 42, tzinfo=timezone.utc)
    # Same minute as open — spike high 110 before fill at ~100.
    open_bar = {
        'at': '2026-11-20T05:05:00+00:00',
        'open': 100., 'high': 110., 'low': 99.5, 'close': 100.2,
        'spread': 0.1,
    }
    later = {
        'at': '2026-11-20T05:06:00+00:00',
        'open': 100.2, 'high': 101.0, 'low': 100.0, 'close': 100.8,
        'spread': 0.1,
    }
    tip = trail_extreme_from_broker(
        'BUY', bid=100.5, ask=100.6, candles=[open_bar, later],
        opened_at=opened, prior_extreme=None)
    # Live bid 100.5 and later bar ~100.95 (101 - 0.05) — never 110.
    assert tip is not None
    assert tip < 102.0
    assert tip >= 100.5 - 1e-9


def test_trail_sell_skips_opening_minute_preopen_trough():
    opened = datetime(2026, 11, 20, 5, 5, 42, tzinfo=timezone.utc)
    open_bar = {
        'at': '2026-11-20T05:05:00+00:00',
        'open': 100., 'high': 100.5, 'low': 90.0, 'close': 99.8,
        'spread': 0.1,
    }
    later = {
        'at': '2026-11-20T05:06:00+00:00',
        'open': 99.8, 'high': 100.0, 'low': 99.0, 'close': 99.2,
        'spread': 0.1,
    }
    tip = trail_extreme_from_broker(
        'SELL', bid=99.4, ask=99.5, candles=[open_bar, later],
        opened_at=opened, prior_extreme=None)
    assert tip is not None
    assert tip > 98.0  # not the pre-open 90 trough
    assert tip <= 99.5 + 1e-9


def test_prior_extreme_not_required_when_recomputing():
    """Engine no longer feeds stored trail_extreme — recompute stays clean."""
    opened = '2026-11-20T05:05:42+00:00'
    bars = [{
        'at': (datetime(2026, 11, 20, 5, 5, tzinfo=timezone.utc)
               + timedelta(minutes=i)).isoformat(),
        'open': 100., 'high': 100.2 + i * 0.01, 'low': 99.8, 'close': 100.1,
        'spread': 0.05,
    } for i in range(0, 5)]
    polluted = 150.0  # would stick if prior were trusted
    tip = trail_extreme_from_broker(
        'BUY', bid=100.3, ask=100.35, candles=bars,
        opened_at=opened, prior_extreme=None)
    assert tip is not None and tip < 101.0
    # Even if a caller passes polluted prior, opening-minute bars stay out;
    # prior alone can still lift tip — engine must pass prior_extreme=None.
    tip2 = trail_extreme_from_broker(
        'BUY', bid=100.3, ask=100.35, candles=bars,
        opened_at=opened, prior_extreme=polluted)
    assert tip2 == polluted
