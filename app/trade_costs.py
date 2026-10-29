"""Unified trade cost accounting for learners and paper simulation.

Round-trip rules (price units unless noted)
==========================================
- ``spread`` is the full bid–ask width in price units (ask − bid). Never treat
  half-spread as a full-width input.
- Mid is the mark; fills are adverse to the trader:
    BUY opens/covers at ask = mid + half_spread (+ adverse slip)
    SELL opens/covers at bid = mid − half_spread (− adverse slip)
- One round-trip therefore costs **one** full spread (half in + half out), not
  |spread_entry| + |spread_exit| as two full widths. When entry and exit
  spreads differ, use their average: ``0.5 * (|sp_i| + |sp_j|)``.
- ``slip_bps`` is optional adverse slippage in basis points of mid, applied per
  fill leg when that leg is modelled. Default 0 — do not invent slip.
- ``commission`` / ``funding`` are optional additive costs in the same unit as
  the PnL being computed (price units for price proxies, account currency for
  cash). Default 0 — never invent financing or broker fees.
- Autonomy history rows store ``spread_bps`` (full width in bps of mid). The
  same RT rule applies in bps: ``0.5 * (spread_bps_i + spread_bps_j)``.

Fill-embedded vs subtractive forms are equivalent when used consistently:
  fill_to_fill_pnl already embeds half+half; do **not** also subtract a full
  spread. Mid-to-mid gross move must subtract ``round_trip_spread_cost`` once.
"""
from __future__ import annotations

import math


def _finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None


def half_spread(spread):
    """Half of full bid–ask width (price units)."""
    return abs(float(spread or 0))/2.


def adverse_slip(mid, slip_bps=0.):
    """Adverse slip in price units from bps of mid."""
    m=_finite(mid)
    if m is None:return 0.
    return abs(float(slip_bps or 0))/10000.*abs(m)


def fill_price(direction, mid, spread, *, slip_bps=0.):
    """Conservative fill: BUY pays ask, SELL hits bid (+ adverse slip_bps)."""
    mid=float(mid)
    half=half_spread(spread)
    slip=adverse_slip(mid,slip_bps)
    if direction=='BUY':
        return mid+half+slip
    if direction=='SELL':
        return mid-half-slip
    raise ValueError('direction must be BUY or SELL')


def round_trip_spread_cost(spread_entry, spread_exit=None):
    """One RT full-spread cost — average when exit spread known; never sum two fulls."""
    se=abs(float(spread_entry or 0))
    if spread_exit is None:
        return se
    sx=abs(float(spread_exit or 0))
    return 0.5*(se+sx)


def round_trip_slip_cost(mid_entry, mid_exit=None, *, slip_bps=0.):
    """Adverse slip on both legs when slip_bps > 0."""
    if not slip_bps:
        return 0.
    entry_leg=adverse_slip(mid_entry,slip_bps)
    if mid_exit is None:
        return 2.*entry_leg
    return entry_leg+adverse_slip(mid_exit,slip_bps)


def round_trip_spread_bps(spread_bps_entry, spread_bps_exit=None):
    """Same RT rule in bps of mid (full-width inputs)."""
    se=abs(float(spread_bps_entry or 0))
    if spread_bps_exit is None:
        return se
    sx=abs(float(spread_bps_exit or 0))
    return 0.5*(se+sx)


def net_mid_move(direction, entry_mid, exit_mid, *, spread_entry=0., spread_exit=None,
                 slip_bps=0., commission=0., funding=0., size=1.):
    """Gross mid move minus one RT spread (+ optional slip/fees). Price units.

    Use when marks are mids. Prefer fill_to_fill_pnl when modelling bid/ask fills.
    """
    entry=_finite(entry_mid);exit_px=_finite(exit_mid)
    if entry is None or exit_px is None or direction not in ('BUY','SELL'):
        return None
    sign=1 if direction=='BUY' else -1
    move=sign*(exit_px-entry)
    cost=(round_trip_spread_cost(spread_entry,spread_exit)
          +round_trip_slip_cost(entry,exit_px,slip_bps=slip_bps)
          +abs(float(commission or 0))+abs(float(funding or 0)))
    return float((move-cost)*float(size or 1.))


def fill_to_fill_pnl(direction, entry_mid, exit_mid, *, spread_entry=0., spread_exit=None,
                     slip_bps=0., commission=0., funding=0., size=1.):
    """PnL from adverse entry fill to adverse exit fill (spread embedded once)."""
    entry_m=_finite(entry_mid);exit_m=_finite(exit_mid)
    if entry_m is None or exit_m is None or direction not in ('BUY','SELL'):
        return None
    sp_x=spread_entry if spread_exit is None else spread_exit
    open_px=fill_price(direction,entry_m,spread_entry,slip_bps=slip_bps)
    close_dir='SELL' if direction=='BUY' else 'BUY'
    close_px=fill_price(close_dir,exit_m,sp_x,slip_bps=slip_bps)
    sign=1 if direction=='BUY' else -1
    gross=sign*(close_px-open_px)
    fees=abs(float(commission or 0))+abs(float(funding or 0))
    return float((gross-fees)*float(size or 1.))


def net_forward_bps(direction, entry_mid, exit_mid, *, spread_bps_entry=0.,
                    spread_bps_exit=None, slip_bps=0., commission_bps=0.,
                    funding_bps=0.):
    """Spread-adjusted forward move in bps of entry mid (autonomy proxy).

    Default matches historical autonomy: RT spread only (no invented slip/fees).
    Optional slip_bps / commission_bps / funding_bps subtract when callers pass them.
    """
    entry=_finite(entry_mid);exit_px=_finite(exit_mid)
    if entry is None or exit_px is None or entry<=0 or direction not in ('BUY','SELL'):
        return None
    sign=1. if direction=='BUY' else -1.
    signed=sign*(exit_px-entry)*10000./entry
    cost=(round_trip_spread_bps(spread_bps_entry,spread_bps_exit)
          +abs(float(slip_bps or 0))
          +abs(float(commission_bps or 0))+abs(float(funding_bps or 0)))
    return float(signed-cost)
