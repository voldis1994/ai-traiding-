"""Structure path must be entry→exit inclusive — not post-close, not head-truncated."""
from __future__ import annotations

from app import structure_shadow as ss


def _bars(n, *, start_px=100.0, step=-0.3, t0='2026-03-01T10:'):
    rows, px = [], start_px
    for i in range(n):
        o, c = px, px + step
        rows.append({
            'at': f'{t0}{i:02d}:00Z',
            'open': o, 'high': max(o, c) + 0.05,
            'low': min(o, c) - 0.05, 'close': c,
        })
        px = c
    return rows


def test_candle_budget_constants():
    assert ss.MAX_BARS_PER_SNAP == 60          # in-trade path cap
    assert ss.MAX_PRE_BARS == 40               # pre-entry breakout context
    assert ss.MIN_BARS == 3
    assert ss.ADVISE_PATH_BARS == 30           # live evidence lookback
    assert ss.ADVISE_PRE_BARS == 40


def test_trim_includes_entry_and_cuts_at_exit_touch():
    """BUY SL: path starts at entry bar, ends on first bar whose low <= exit."""
    rows = _bars(20, start_px=100.0, step=-0.4)
    entry_at = rows[5]['at']
    # Exit fill sits inside bar 12's range.
    exit_px = rows[12]['low'] + 0.01
    # Extra post-close bars that must NOT enter the frame.
    post = _bars(8, start_px=rows[-1]['close'], step=-0.2, t0='2026-03-01T11:')
    all_rows = rows + post
    path = ss.trim_path_entry_to_exit(
        all_rows, direction='BUY', exit_level=exit_px,
        entry_bar_time=entry_at)
    assert path[0]['at'] == entry_at
    assert path[-1]['at'] == rows[12]['at']
    assert all(str(b['at']) <= rows[12]['at'] for b in path)
    assert all(str(b['at']) >= entry_at for b in path)
    # No 11:xx post-close bars.
    assert not any(str(b['at']).startswith('2026-03-01T11:') for b in path)


def test_trim_long_path_keeps_entry_and_exit_not_head_only():
    """>60 bars: old code path[:60] dropped the SL candle — must keep exit end."""
    rows = _bars(80, start_px=200.0, step=-0.2)
    entry_at = rows[0]['at']
    exit_px = rows[-1]['low']
    path = ss.trim_path_entry_to_exit(
        rows, direction='BUY', exit_level=exit_px,
        entry_bar_time=entry_at, limit=60)
    assert len(path) == 60
    assert path[0]['at'] == entry_at
    assert path[-1]['at'] == rows[-1]['at']  # SL / exit candle kept


def test_entry_and_exit_candles_are_correct_for_structure():
    """Yes: frame the open trade with entry M1 through exit M1 inclusive.

    Pre-bars are separate (breakout only) — they must not be mixed into ``bars``.
    """
    pre = _bars(15, start_px=100.0, step=0.05, t0='2026-03-01T08:')
    path_raw = _bars(12, start_px=100.5, step=-0.45, t0='2026-03-01T10:')
    entry_at = path_raw[0]['at']
    exit_px = path_raw[-1]['close']
    path = ss.trim_path_entry_to_exit(
        pre + path_raw, direction='BUY', exit_level=exit_px,
        entry_bar_time=entry_at)
    assert path[0]['at'] == entry_at
    assert path[-1]['at'] == path_raw[-1]['at']
    frame = ss.frame_from_ohlc(path, entry=path[0]['open'], exit_level=exit_px,
                               pre_bars=pre)
    assert frame['ready']
    assert frame['bars'] == len(path)
    # Structure comes from in-trade path; pre only for breakout flag.
    assert frame['structure'] in (
        'TREND_DOWN', 'BREAKOUT_DOWN', 'CHOP', 'RANGE')
