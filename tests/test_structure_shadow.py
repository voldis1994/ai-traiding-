"""Structure shadow: SL path OHLC → market frame → LIVE advice."""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.engine import Store
from app import structure_shadow as ss


def _trend_down_bars(n=12, start=100.0, step=0.4):
    """Lower highs + lower lows — classic TREND_DOWN path."""
    rows = []
    px = start
    for i in range(n):
        o = px
        c = px - step
        rows.append({
            'at': f'2024-01-01T00:{i:02d}:00+00:00',
            'open': o, 'high': o + 0.05, 'low': c - 0.05, 'close': c,
        })
        px = c
    return rows


def _range_bars(n=12, mid=100.0, half=0.3):
    rows = []
    for i in range(n):
        # Oscillate inside a band; end near start so net/range stays small.
        sign = 1 if i % 2 == 0 else -1
        o = mid + sign * half * 0.1
        c = mid - sign * half * 0.1
        if i == n - 1:
            c = mid  # close the loop → RANGE, not a fake trend
        rows.append({
            'at': f'2024-01-01T01:{i:02d}:00+00:00',
            'open': o, 'high': mid + half, 'low': mid - half, 'close': c,
        })
    return rows


def _breakout_down(pre_n=10, path_n=8, base=100.0):
    pre = []
    for i in range(pre_n):
        pre.append({
            'at': f'2024-01-01T02:{i:02d}:00+00:00',
            'open': base, 'high': base + 0.5, 'low': base - 0.5, 'close': base + 0.1,
        })
    path = []
    px = base - 0.4
    for i in range(path_n):
        o = px
        c = px - 0.6
        path.append({
            'at': f'2024-01-01T03:{i:02d}:00+00:00',
            'open': o, 'high': o + 0.05, 'low': c - 0.1, 'close': c,
        })
        px = c
    return pre, path


def test_frame_trend_down_from_ohlc():
    frame = ss.frame_from_ohlc(_trend_down_bars())
    assert frame['ready']
    assert frame['structure'] == 'TREND_DOWN'
    assert frame['net'] < 0


def test_frame_range_from_ohlc():
    frame = ss.frame_from_ohlc(_range_bars())
    assert frame['ready']
    assert frame['structure'] in ('RANGE', 'CHOP')


def test_frame_breakout_down():
    pre, path = _breakout_down()
    frame = ss.frame_from_ohlc(path, exit_level=path[-1]['close'], pre_bars=pre)
    assert frame['structure'] == 'BREAKOUT_DOWN'
    assert frame['breakout'] is True


def test_record_loss_builds_library_and_advice_holds():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        bars = _trend_down_bars()
        # Three BUY SL deaths on TREND_DOWN — same structure memory.
        for i in range(3):
            out = ss.record_loss(
                store, epic='GOLD', direction='BUY',
                entry=bars[0]['open'], exit_level=bars[-1]['close'],
                exit_reason='sl', broker_pnl=-0.12,
                bars=bars, deal_id=f'd{i}', spread=0.35)
            assert out is not None
            assert out['key'].startswith('GOLD|TREND_DOWN|BUY')
        advice = ss.advise(
            store, epic='GOLD', proposal='BUY', bars=bars)
        assert advice['action'] == 'HOLD'
        assert advice['structure'] == 'TREND_DOWN'
        assert advice['confidence'] >= ss.ADVICE_SL_RATE
        # Opposite side is not blocked by BUY graves.
        other = ss.advise(
            store, epic='GOLD', proposal='SELL', bars=bars)
        assert other['action'] is None


def test_advice_needs_min_samples():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        bars = _trend_down_bars()
        ss.record_loss(
            store, epic='GOLD', direction='BUY',
            entry=100., exit_level=95., exit_reason='sl',
            broker_pnl=-0.1, bars=bars, deal_id='only1')
        advice = ss.advise(store, epic='GOLD', proposal='BUY', bars=bars)
        assert advice['action'] is None
        assert 'need_' in advice['reason']


def test_snapshot_keeps_ohlc_trail():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        bars = _trend_down_bars(n=20)
        ss.record_loss(
            store, epic='GOLD', direction='BUY',
            entry=bars[0]['open'], exit_level=bars[-1]['close'],
            exit_reason='sl', broker_pnl=-0.15, bars=bars, deal_id='snap1')
        snaps = store.get(ss.SNAPSHOT_KEY)
        assert isinstance(snaps, list) and snaps
        assert snaps[-1]['bar_count'] == 20
        assert len(snaps[-1]['ohlc']) == 20
        assert snaps[-1]['ohlc'][0]['open'] == bars[0]['open']
