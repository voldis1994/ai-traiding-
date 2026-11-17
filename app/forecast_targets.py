"""Multi-horizon forecast targets from completed M1 mid paths.

Horizons are bar counts (1, 3, 5, 15) — identical to minutes on an M1 feed.
Targets are pure labels for forecasting quality; they do not open/close trades.
"""
from __future__ import annotations

import math

HORIZONS = (1, 3, 5, 15)
# Match AdaptiveBrain._label: BUY=0, SELL=1, HOLD=2
LABEL_BUY, LABEL_SELL, LABEL_HOLD = 0, 1, 2


def mid_change(entry_mid, exit_mid):
    """Signed price change in instrument units (GOLD points, not account £)."""
    try:
        a=float(entry_mid);b=float(exit_mid)
    except (TypeError,ValueError):
        return None
    if not (math.isfinite(a) and math.isfinite(b)) or a<=0:
        return None
    return b-a


def direction_label(entry_mid, exit_mid, *, cost=0.):
    """Cost-aware direction label (same rule as AdaptiveBrain._label)."""
    delta=mid_change(entry_mid,exit_mid)
    if delta is None:
        return None
    try:
        c=max(0.,float(cost or 0.))
    except (TypeError,ValueError):
        c=0.
    if delta>c:return LABEL_BUY
    if delta<-c:return LABEL_SELL
    return LABEL_HOLD


def targets_at_horizons(entry_mid, future_mids, *, spread=0., future_spreads=None,
                        horizons=HORIZONS):
    """Build {h: {r, label, sigma_proxy, exit_mid}} for each reachable horizon.

    ``future_mids[i]`` is the mid ``i+1`` bars after entry (index 0 = h=1).
    ``sigma_proxy`` = |r| — a crude realized-move scale used until the model
    learns its own σ̂.
    """
    out={}
    if entry_mid is None:
        return out
    try:
        entry=float(entry_mid)
    except (TypeError,ValueError):
        return out
    if not math.isfinite(entry) or entry<=0:
        return out
    mids=list(future_mids or [])
    spreads=list(future_spreads or [])
    try:
        base_spread=max(0.,float(spread or 0.))
    except (TypeError,ValueError):
        base_spread=0.
    for h in horizons:
        h=int(h)
        if h<1 or h>len(mids):
            continue
        exit_mid=mids[h-1]
        try:
            exit_sp=float(spreads[h-1]) if h-1<len(spreads) else base_spread
        except (TypeError,ValueError,IndexError):
            exit_sp=base_spread
        cost=max(base_spread,exit_sp if math.isfinite(exit_sp) else base_spread)
        r=mid_change(entry,exit_mid)
        lab=direction_label(entry,exit_mid,cost=cost)
        if r is None or lab is None:
            continue
        out[h]={
            'r':float(r),
            'label':int(lab),
            'sigma_proxy':float(abs(r)),
            'exit_mid':float(exit_mid) if math.isfinite(float(exit_mid)) else None,
            'cost':float(cost),
        }
    return out


def path_from_candles(candles, *, after_time=None, after_index=None, max_h=15):
    """Extract future mids/spreads after a bar identified by time or index.

    ``candles``: chronological list of dicts with mid|close and optional spread.
    """
    if not candles:
        return [],[]
    start=None
    if after_index is not None:
        start=int(after_index)+1
    elif after_time is not None:
        for i,c in enumerate(candles):
            t=c.get('time') if isinstance(c,dict) else None
            if t==after_time:
                start=i+1
                break
    if start is None:
        return [],[]
    mids=[];spreads=[]
    for c in candles[start:start+int(max_h)]:
        if not isinstance(c,dict):
            continue
        mid=c.get('mid',c.get('close'))
        try:
            mid=float(mid)
        except (TypeError,ValueError):
            continue
        if not math.isfinite(mid) or mid<=0:
            continue
        mids.append(mid)
        try:
            sp=float(c.get('spread') or 0.)
        except (TypeError,ValueError):
            sp=0.
        spreads.append(sp if math.isfinite(sp) else 0.)
    return mids,spreads
