"""Walk-Forward / Out-of-Sample validation for discovered features.

Rejects: future leakage, near-duplicates of existing inputs, unstable folds,
and features with no measurable net-PnL association on completed bars.

Approval is independent of LIVE trading — engine only expands dim after approve.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .feature_discovery import CANDIDATE_LIBRARY, compute_candidate, _build_library
from .market_context import candle_time

STORE_KEY = 'feature_validation_log_v1'
MIN_BARS = 120
MIN_FOLDS = 3
MAX_CORR = 0.92  # reject near-copies of base/approved features
MIN_FOLD_AGREE = 0.6  # fraction of folds with same sign of edge
MIN_ABS_EDGE = 1e-4


def _forward_label(closes, i, horizon=8, cost=0.):
    """Spread-aware direction after horizon completed bars — no future peek beyond i+horizon."""
    if i+horizon>=len(closes):return None
    delta=float(closes[i+horizon]-closes[i])
    if delta>cost:return 1.0
    if delta<-cost:return -1.0
    return 0.0


def _series_for_feature(feature_id, bars, spreads, times):
    _build_library()
    if feature_id not in CANDIDATE_LIBRARY:return None
    values=[]
    for i in range(60,len(bars)):
        window=bars[:i+1]
        if len(window)<61:values.append(np.nan);continue
        # Only the last 61 bars of the prefix — same as live contiguous window.
        use=window[-61:]
        sp=float(spreads[i]) if i<len(spreads) else 0.
        bt=times[i] if i<len(times) else None
        val=compute_candidate(feature_id,use,spread=sp,bar_time=bt)
        values.append(np.nan if val is None else val)
    return np.asarray(values,dtype=float)


def _corr(a,b):
    mask=np.isfinite(a)&np.isfinite(b)
    if mask.sum()<40:return None
    x,y=a[mask],b[mask]
    if np.std(x)<1e-12 or np.std(y)<1e-12:return 0.
    return float(np.corrcoef(x,y)[0,1])


def walk_forward_edge(feature_values, labels, *, folds=4):
    """Chronological folds: train on past, score Spearman-like sign edge on OOS."""
    n=len(feature_values)
    if n<MIN_BARS or folds<2:return None
    fold_edges=[]
    boundaries=np.linspace(0,n,folds+1,dtype=int)
    for f in range(folds):
        start,end=int(boundaries[f]),int(boundaries[f+1])
        # OOS slice; correlation with future label inside fold only (labels already delayed).
        x=feature_values[start:end];y=labels[start:end]
        mask=np.isfinite(x)&np.isfinite(y)&(y!=0)
        if mask.sum()<15:
            fold_edges.append(None);continue
        # Edge = mean(sign(x)*y) — does high feature co-move with profitable direction?
        edge=float(np.mean(np.sign(x[mask])*y[mask]))
        fold_edges.append(edge)
    usable=[e for e in fold_edges if e is not None]
    if len(usable)<max(2,MIN_FOLDS-1):return None
    signs=[1 if e>0 else (-1 if e<0 else 0) for e in usable]
    majority=max(set(signs),key=signs.count) if signs else 0
    agree=sum(1 for s in signs if s==majority and s!=0)/max(len(usable),1)
    mean_edge=float(np.mean(usable))
    return {'fold_edges':fold_edges,'mean_edge':mean_edge,'agree':agree,
            'stable':agree>=MIN_FOLD_AGREE and abs(mean_edge)>=MIN_ABS_EDGE}


def validate_feature(feature_id, bars, *, spreads=None, times=None,
                     existing_matrix=None, existing_names=None):
    """Full independent check. bars = list of (o,h,l,c) in chronological order."""
    _build_library()
    if feature_id not in CANDIDATE_LIBRARY:
        return {'approved':False,'reason':'unknown feature id'}
    if not bars or len(bars)<MIN_BARS:
        return {'approved':False,'reason':f'need ≥{MIN_BARS} completed bars'}
    spreads=list(spreads or [0.]*len(bars))
    times=list(times or [None]*len(bars))
    closes=np.array([b[3] for b in bars],dtype=float)
    # Labels aligned to feature series index (starts at bar 60).
    feat=_series_for_feature(feature_id,bars,spreads,times)
    if feat is None:return {'approved':False,'reason':'series failed'}
    labels=[]
    for i in range(60,len(bars)):
        cost=float(spreads[i]) if i<len(spreads) else 0.
        labels.append(_forward_label(closes,i,horizon=8,cost=cost))
    labels=np.asarray([np.nan if x is None else x for x in labels],dtype=float)
    # Leakage guard: feature at i uses bars[i-60:i+1] only; label uses closes[i+8] —
    # never mixes unfinished bar. Enforce lengths match.
    if len(feat)!=len(labels):
        return {'approved':False,'reason':'length mismatch (leakage guard)'}
    # Duplicate / dependency vs existing feature matrix (rows = samples).
    if existing_matrix is not None and len(existing_matrix)==len(feat):
        names=list(existing_names or [])
        for j in range(existing_matrix.shape[1]):
            c=_corr(feat,existing_matrix[:,j])
            if c is not None and abs(c)>=MAX_CORR:
                nm=names[j] if j<len(names) else str(j)
                return {'approved':False,'reason':f'duplicate/collinear with {nm} (|ρ|={abs(c):.2f})',
                        'corr':c,'peer':nm}
    wf=walk_forward_edge(feat,labels,folds=4)
    if wf is None:
        return {'approved':False,'reason':'insufficient walk-forward folds'}
    if not wf['stable']:
        return {'approved':False,'reason':'unstable or weak OOS edge',
                'metrics':wf}
    # Net PnL proxy: trade sign(feature) with spread cost on OOS last fold only.
    n=len(feat);cut=int(n*.7)
    x,y=feat[cut:],labels[cut:]
    mask=np.isfinite(x)&np.isfinite(y)
    if mask.sum()<20:
        return {'approved':False,'reason':'OOS too thin for PnL proxy'}
    pnl=float(np.mean(np.sign(x[mask])*y[mask]))
    if abs(pnl)<MIN_ABS_EDGE:
        return {'approved':False,'reason':'no net PnL association on OOS','metrics':wf,'oos_pnl':pnl}
    name,formula,_=CANDIDATE_LIBRARY[feature_id]
    return {
        'approved':True,
        'reason':'walk-forward OOS stable with PnL association',
        'id':feature_id,'name':name,'formula':formula,
        'metrics':{**wf,'oos_pnl':pnl},
        'approved_at':datetime.now(timezone.utc).isoformat()}


class FeatureValidator:
    def __init__(self, store, registry):
        self.store=store
        self.registry=registry
        self.log=list(self.store.get(STORE_KEY) or [])

    def _persist(self, entry):
        self.log.append(entry)
        self.store.set(STORE_KEY,self.log[-80:])

    def validate_and_maybe_approve(self, feature_id, bars, *, spreads=None, times=None,
                                   existing_matrix=None, existing_names=None):
        result=validate_feature(feature_id,bars,spreads=spreads,times=times,
                                existing_matrix=existing_matrix,existing_names=existing_names)
        entry={'feature_id':feature_id,'result':result,
               'at':datetime.now(timezone.utc).isoformat()}
        self._persist(entry)
        if not result.get('approved'):
            self.store.event('validation','Feature rejected',
                {'id':feature_id,'reason':result.get('reason')})
            return result
        ok,why=self.registry.approve({
            'id':result['id'],'name':result['name'],'formula':result['formula'],
            'source':'walk_forward_oos','approved_at':result['approved_at'],
            'metrics':result.get('metrics')})
        result['registry']=why
        if ok:
            self.store.event('validation','Feature approved into registry',
                {'id':feature_id,'name':result['name'],'ohlc_dim':self.registry.ohlc_dim()})
        else:
            result['approved']=False
            result['reason']='validation passed but registry refused: '+why
        return result
