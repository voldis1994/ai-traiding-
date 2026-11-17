"""Multi-horizon forecast targets from completed M1 mid paths.

Horizons are bar counts (1, 3, 5, 15) — identical to minutes on an M1 feed.
Targets are pure labels for forecasting quality; they do not open/close trades.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

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


def bar_stamp(value):
    """Normalize candle/forecast timestamps for equality (UTC aware)."""
    if value is None:
        return None
    if isinstance(value,datetime):
        dt=value
    else:
        try:
            from .market_context import candle_time
            dt=candle_time(value)
        except Exception:
            try:
                s=str(value).replace('Z','+00:00')
                dt=datetime.fromisoformat(s)
            except Exception:
                return str(value)
    if dt.tzinfo is None:
        dt=dt.replace(tzinfo=timezone.utc)
    else:
        dt=dt.astimezone(timezone.utc)
    # Floor to the minute — M1 bar identity.
    return dt.replace(second=0,microsecond=0)


def candle_bar_time(c):
    """Store candles use ``at``; forecast rows / tests may use ``time``."""
    if not isinstance(c,dict):
        return None
    return c.get('at',c.get('time'))


def candle_mid(c):
    if not isinstance(c,dict):
        return None
    for k in ('mid','close','c'):
        if c.get(k) is None:
            continue
        try:
            v=float(c[k])
        except (TypeError,ValueError):
            continue
        if math.isfinite(v) and v>0:
            return v
    return None


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

    Matches Store candle ``at`` or test ``time`` via normalized minute stamps.
    Returns (mids, spreads, matched_index) — matched_index is None on miss.
    """
    if not candles:
        return [],[],None
    # Chronological order by stamp (Store.candles is already ASC, but be safe).
    indexed=[]
    for i,c in enumerate(candles):
        if not isinstance(c,dict):
            continue
        st=bar_stamp(candle_bar_time(c))
        mid=candle_mid(c)
        if st is None or mid is None:
            continue
        try:
            sp=float(c.get('spread') or 0.)
        except (TypeError,ValueError):
            sp=0.
        indexed.append((i,st,mid,sp if math.isfinite(sp) else 0.))
    if not indexed:
        return [],[],None
    indexed.sort(key=lambda x:x[1])
    start_pos=None
    if after_index is not None:
        # after_index refers to original list index; find first sorted entry
        # whose original index is > after_index.
        ai=int(after_index)
        for j,(_,_,_,_) in enumerate(indexed):
            if indexed[j][0]>ai:
                start_pos=j
                break
    elif after_time is not None:
        want=bar_stamp(after_time)
        if want is None:
            return [],[],None
        for j,(_,st,_,_) in enumerate(indexed):
            if st==want:
                start_pos=j+1  # first bar AFTER the forecast bar
                break
        if start_pos is None:
            # Fallback: first bar strictly after the forecast minute.
            for j,(_,st,_,_) in enumerate(indexed):
                if st>want:
                    start_pos=j
                    break
    if start_pos is None:
        return [],[],None
    window=indexed[start_pos:start_pos+int(max_h)]
    mids=[m for _,_,m,_ in window]
    spreads=[s for _,_,_,s in window]
    match_idx=indexed[start_pos-1][0] if start_pos>0 else None
    return mids,spreads,match_idx
