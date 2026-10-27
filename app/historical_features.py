"""Build a real chronological feature matrix for collinearity checks.

Never tile a single snapshot — each row is features from that completed bar's
past-only 61-minute window (same leakage rules as LIVE).
"""
from __future__ import annotations

import math

import numpy as np

from .market_context import FEATURE_NAMES, FEATURE_DIM
from .feature_discovery import compute_approved, compute_candidate


def _window_ok(bars_slice):
    return bars_slice is not None and len(bars_slice)>=61


def sample_bar_indices(n_bars, *, step=4, max_rows=80):
    """Evenly spaced completed-bar indices (≥60) for historical sampling."""
    if n_bars<61+20:return []
    start=60
    indices=list(range(start,n_bars,max(1,int(step))))
    if len(indices)>max_rows:
        pick=np.linspace(0,len(indices)-1,max_rows,dtype=int)
        indices=[indices[i] for i in pick]
    return indices


def base_feature_row(bars_window, *, spread=0.):
    """Compute the fixed BASE_OHLC vector for one completed window (61 bars)."""
    if not _window_ok(bars_window):return None
    from datetime import datetime, timezone, timedelta
    from .market_context import market_features
    origin=datetime(2020,1,1,tzinfo=timezone.utc)
    rows=[]
    for i,b in enumerate(bars_window):
        rows.append({'at':(origin+timedelta(minutes=i)).isoformat(),
                     'open':b[0],'high':b[1],'low':b[2],'close':b[3]})
    # now_utc = one minute after last bar so all 61 are completed.
    snap=market_features(rows,now_utc=origin+timedelta(minutes=len(bars_window)),
                         spread=float(spread or 0),approved=None)
    if not snap.get('ready') or not snap.get('features'):return None
    feats=list(snap['features'][:FEATURE_DIM])
    return feats if len(feats)==FEATURE_DIM and all(math.isfinite(x) for x in feats) else None


def build_feature_matrix(bars, spreads, *, approved=None, step=4, max_rows=80):
    """Rows = historical samples; cols = BASE (+ approved dynamic).

    Returns (matrix, names, indices). `step` subsamples for CPU; still many
    distinct bars — not one tiled vector.
    """
    names=list(FEATURE_NAMES)
    if approved:
        names+=[(a.get('name') if isinstance(a,dict) else str(a)) for a in approved]
    if not bars or len(bars)<61+20:
        return None,names,[]
    rows=[];used=[]
    for i in sample_bar_indices(len(bars),step=step,max_rows=max_rows):
        window=bars[i-60:i+1]
        sp=spreads[i] if i<len(spreads) else None
        if sp is None or not math.isfinite(float(sp)) or float(sp)<0:
            continue
        base=base_feature_row(window,spread=float(sp))
        if base is None:continue
        if approved:
            extra=compute_approved(window,approved,spread=float(sp),
                                   bar_time=None)
            if extra is None:continue
            rows.append(base+list(extra))
        else:
            rows.append(base)
        used.append(i)
    if len(rows)<20:return None,names,[]
    matrix=np.asarray(rows,dtype=float)
    if matrix.ndim!=2 or matrix.shape[1]!=len(names):return None,names,[]
    return matrix,names,used


def candidate_at_indices(feature_id, bars, spreads, times, indices):
    """Candidate values on the same bar indices as the historical matrix."""
    out=[]
    for i in indices:
        if i<60 or i>=len(bars):
            out.append(float('nan'));continue
        window=bars[i-60:i+1]
        sp=spreads[i] if i<len(spreads) else None
        if sp is None or not math.isfinite(float(sp)) or float(sp)<0:
            out.append(float('nan'));continue
        bt=times[i] if times is not None and i<len(times) else None
        val=compute_candidate(feature_id,window,spread=float(sp),bar_time=bt)
        out.append(float('nan') if val is None else float(val))
    return np.asarray(out,dtype=float)
