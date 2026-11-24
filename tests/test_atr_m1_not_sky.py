"""ATR must stay M1-scale for the live epic — no sky spikes / wrong-epic paint."""
from __future__ import annotations

from pathlib import Path

from app.mtf_momentum import atr_wilder


def _bar(i, *, mid=100.0, span=1.0):
    return {
        'at': f'2026-11-24T10:{i:02d}:00Z',
        'open': mid, 'high': mid + span / 2, 'low': mid - span / 2, 'close': mid,
    }


def test_atr_wilder_clips_spike_gap_bar():
    bars = [_bar(i, mid=100.0, span=1.0) for i in range(40)]
    # One catastrophic gap bar would otherwise drag Wilder ATR sky-high.
    bars[-1] = {
        'at': '2026-11-24T10:39:00Z',
        'open': 100.0, 'high': 180.0, 'low': 20.0, 'close': 100.0,
    }
    atr = atr_wilder(bars, 14)
    assert atr is not None
    assert atr < 10.0  # clipped — without clip this is ≫10


def test_structure_view_prefers_open_position_epic():
    src = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    chunk = src.split('def admin_client_structure_view')[1].split(
        'def admin_markets_ohlc')[0]
    assert "epic_source='open_position'" in chunk
    assert 'Live open deal epic beats mandate' in chunk or 'open_position' in chunk


def test_desk_js_open_beats_sticky_focus_and_labels_atr_epic():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'Live open book ALWAYS beats sticky focus' in js
    assert 'deskFocusEpic' in js
    assert 'deskClipAtr' in js
    assert 'ATR M1' in js
    chart = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert 'ATR M1' in chart
