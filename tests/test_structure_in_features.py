"""Brain must *see* structure at OPEN — otherwise it cannot learn to understand."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from app.market_context import FEATURE_DIM, FEATURE_NAMES, FEATURE_SCHEMA, market_features
from app.structure_shadow import STRUCTURE_FEATURE_NAMES, feature_vector_from_frame, frame_from_ohlc


def _rows(n=70, *, start=3950.0, step=-0.25):
    t0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    rows, px = [], start
    for i in range(n):
        o, c = px, px + step
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': max(o, c) + 0.05,
            'low': min(o, c) - 0.05, 'close': c,
        })
        px = c
    return rows


def test_feature_schema_includes_structure_channels():
    assert FEATURE_SCHEMA.endswith('shadow_v5')
    for name in STRUCTURE_FEATURE_NAMES:
        assert name in FEATURE_NAMES
    assert FEATURE_DIM == 18  # 13 classic + 5 structure


def test_market_features_embed_live_structure_at_open():
    """OPEN-time z carries structure — not only a post-SL grave label."""
    now = datetime(2026, 3, 1, 13, 10, tzinfo=timezone.utc)
    snap = market_features(_rows(), now_utc=now, spread=0.35, epic='GOLD')
    assert snap['ready']
    assert len(snap['features']) == FEATURE_DIM
    assert snap.get('structure') in (
        'TREND_UP', 'TREND_DOWN', 'RANGE', 'BREAKOUT_UP', 'BREAKOUT_DOWN', 'CHOP')
    # Down-sloping seed → negative trend sign channel.
    names = snap['feature_names']
    idx = names.index('struct_trend_sign')
    assert snap['features'][idx] <= 0.0
    assert names.index('struct_net_over_range') >= 13


def test_structure_vector_differs_trend_up_vs_down():
    down = [{'open': 100 - i * 0.4, 'high': 100 - i * 0.4 + 0.05,
             'low': 100 - (i + 1) * 0.4 - 0.05, 'close': 100 - (i + 1) * 0.4,
             'at': f'd{i}'} for i in range(12)]
    up = [{'open': 100 + i * 0.4, 'high': 100 + (i + 1) * 0.4 + 0.05,
           'low': 100 + i * 0.4 - 0.05, 'close': 100 + (i + 1) * 0.4,
           'at': f'u{i}'} for i in range(12)]
    vd = feature_vector_from_frame(frame_from_ohlc(down))
    vu = feature_vector_from_frame(frame_from_ohlc(up))
    assert vd[-1] < 0 < vu[-1]  # trend_sign
    assert not np.allclose(vd, vu)
