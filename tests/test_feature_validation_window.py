"""Regression: LIVE history window must satisfy train/OOS feature validation math."""
from app.feature_validation import (
    FEATURE_SERIES_WARMUP,
    LIVE_HISTORY_BARS_FLOOR,
    BROKER_PRICE_HISTORY_MAX,
    train_oos_bounds,
    train_end_floor,
    min_bars_for_train_oos_split,
    recommended_live_history_bars,
    validate_feature,
)
from app.self_params import DEFAULTS


def test_defaults_need_more_than_legacy_190_bars():
    need = min_bars_for_train_oos_split(None)
    assert need >= 260
    assert need > 190
    assert DEFAULTS['val_oos_frac'] == 0.7
    assert DEFAULTS['val_min_bars'] == 120


def test_recommended_live_history_covers_train_oos_split():
    need = min_bars_for_train_oos_split(None)
    hist = recommended_live_history_bars(None)
    assert hist >= need
    assert hist >= LIVE_HISTORY_BARS_FLOOR
    assert hist <= BROKER_PRICE_HISTORY_MAX


def test_legacy_190_fails_thin_split_math():
    """Document the bug: 190 completed bars → train_end below floor."""
    n_feat = 190 - FEATURE_SERIES_WARMUP
    train_end, end, frac = train_oos_bounds(n_feat, oos_frac=DEFAULTS['val_oos_frac'])
    floor = train_end_floor(None)
    assert frac == 0.7
    assert train_end < floor
    assert end - train_end >= 15  # OOS side alone is not the blocker


def test_recommended_window_passes_thin_split_math():
    hist = recommended_live_history_bars(None)
    n_feat = hist - FEATURE_SERIES_WARMUP
    train_end, end, frac = train_oos_bounds(n_feat, oos_frac=DEFAULTS['val_oos_frac'])
    floor = train_end_floor(None)
    assert train_end >= floor
    assert end - train_end >= 15


def test_validate_feature_rejects_190_as_thin_split():
    """Legacy LIVE window: past val_min_bars, fails train/OOS thin-split."""
    bars = []
    price = 100.0
    for i in range(190):
        o = price
        price = price + 0.05
        bars.append((o, max(o, price) + 0.01, min(o, price) - 0.01, price))
    spreads = [0.02 * (1.0 + 0.1 * ((i % 7) / 7.0)) for i in range(len(bars))]
    from app.feature_discovery import _build_library, CANDIDATE_LIBRARY
    _build_library()
    fid = next(iter(CANDIDATE_LIBRARY))
    result = validate_feature(fid, bars, spreads=spreads)
    assert result.get('approved') is False
    assert 'train/OOS chronological split too thin' in str(result.get('reason') or '')
    assert int(result.get('need_bars') or 0) >= 260
