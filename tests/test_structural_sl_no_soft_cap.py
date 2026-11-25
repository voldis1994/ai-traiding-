"""LIVE SL must stay on structure — soft-cap was inventing US100 hairline stops."""
from app.bracket_learn import ATR_STOP_FLOOR_FRAC, swing_anchor_brackets
from tests.test_bracket_learn import _swing_rows


def _nasdaq_like_rows():
    # Deep invalidation ~19920, grind to entry ~20025, tiny upside swing ~20035.
    seq = [
        (19950, 19980, 19940, 19970), (19970, 20020, 19960, 20010),
        (20010, 20040, 19990, 20020), (20020, 20050, 20000, 20040),
        (20040, 20045, 19920, 19930), (19930, 19980, 19910, 19970),
        (19970, 20010, 19960, 20000), (20000, 20030, 19990, 20020),
        (20020, 20035, 20005, 20015), (20015, 20025, 20000, 20012),
        (20012, 20018, 19995, 20008), (20008, 20022, 20000, 20016),
        (20016, 20028, 20005, 20020), (20020, 20032, 20010, 20025),
        (20025, 20040, 20015, 20030), (20030, 20042, 20020, 20028),
    ]
    return _swing_rows(seq + [(20028, 20035, 20020, 20030)] * 8)


def test_us100_style_never_soft_caps_deep_sl_to_10pt():
    """Reproduce LIVE scratch: soft-cap pulled ~100pt structure to ~10pt (£0.75)."""
    rows = _nasdaq_like_rows()
    entry = 20025.0
    atr = 8.0
    buy = {'lo': 20020.0, 'hi': 20023.0, 'side': 'BUY', 'sl_anchor': 20020.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.1, spread=1.0,
        min_distance=5.0, atr=atr, entry_zone=buy,
        zone={'kappa': 1.0, 'range_distance': 12.0, 'max_tp': 40.0,
              'noise_distance': 8.0})
    # Soft-cap path used to ship ready + rr_cap_sl with stop_d≈10.
    assert swing.get('ready') is True, swing
    assert 'rr_cap_sl' not in str(swing.get('reason') or '')
    assert not swing.get('rr_capped')
    # Structural stop stays far enough to survive M1 noise (≥1×ATR, ≫10pt glue).
    assert float(swing['stop_distance']) + 1e-9 >= ATR_STOP_FLOOR_FRAC * atr
    assert float(swing['stop_distance']) + 1e-9 >= 20.0
    assert float(swing['stop_level']) < entry - 20.0
    # Must not be the old soft-capped £0.75 hairline (~10pt).
    assert float(swing['stop_distance']) > 15.0


def test_atr_noise_floor_is_full_atr():
    assert ATR_STOP_FLOOR_FRAC >= 1.0 - 1e-12
