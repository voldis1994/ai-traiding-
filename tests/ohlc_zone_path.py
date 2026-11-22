"""Synthetic 1m paths with clear base→impulse Demand/Supply zones."""
from datetime import datetime, timedelta, timezone


def _row(t0, i, o, h, l, c):
    return {
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
    }


def demand_zone_path(*, start=2000.0, confirm=True):
    """Down → tight base → strong UP impulse → optional bullish rejection in base.

    Returns (rows, zone_approx_mid) where zone is the base before the impulse.
    """
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    rows = []
    px = float(start)
    i = 0
    # Lead-in drift down.
    for _ in range(18):
        o = px
        c = px - 0.35
        rows.append(_row(t0, i, o, o + 0.15, c - 0.15, c))
        px = c
        i += 1
    # Base (consolidation) — Demand box.
    base_lo, base_hi = px - 0.25, px + 0.35
    base_mid = 0.5 * (base_lo + base_hi)
    for _ in range(4):
        o = base_mid - 0.05
        c = base_mid + 0.05
        rows.append(_row(t0, i, o, base_hi - 0.02, base_lo + 0.02, c))
        px = c
        i += 1
    # Strong UP impulse leaving the base.
    for k in range(6):
        o = px
        c = px + 1.1
        rows.append(_row(t0, i, o, c + 0.15, o - 0.05, c))
        px = c
        i += 1
    # Drift above, then return into zone.
    for _ in range(8):
        o = px
        c = px - 0.55
        rows.append(_row(t0, i, o, o + 0.1, c - 0.1, c))
        px = c
        i += 1
    if confirm:
        # Bullish pin rejecting Demand base.
        o = base_mid + 0.05
        c = base_mid + 0.12
        l = base_lo - 0.05
        h = c + 0.08
        rows.append(_row(t0, i, o, h, l, c))
        px = c
        i += 1
    return rows, base_mid


def supply_zone_path(*, start=2000.0, confirm=True):
    """Up → tight base → strong DOWN impulse → optional bearish rejection in base."""
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    rows = []
    px = float(start)
    i = 0
    for _ in range(18):
        o = px
        c = px + 0.35
        rows.append(_row(t0, i, o, c + 0.15, o - 0.15, c))
        px = c
        i += 1
    base_lo, base_hi = px - 0.35, px + 0.25
    base_mid = 0.5 * (base_lo + base_hi)
    for _ in range(4):
        o = base_mid + 0.05
        c = base_mid - 0.05
        rows.append(_row(t0, i, o, base_hi - 0.02, base_lo + 0.02, c))
        px = c
        i += 1
    for _ in range(6):
        o = px
        c = px - 1.1
        rows.append(_row(t0, i, o, o + 0.05, c - 0.15, c))
        px = c
        i += 1
    for _ in range(8):
        o = px
        c = px + 0.55
        rows.append(_row(t0, i, o, c + 0.1, o - 0.1, c))
        px = c
        i += 1
    if confirm:
        o = base_mid - 0.05
        c = base_mid - 0.12
        h = base_hi + 0.05
        l = c - 0.08
        rows.append(_row(t0, i, o, h, l, c))
        px = c
        i += 1
    return rows, base_mid
