"""Walk-Forward / Out-of-Sample validation for discovered features.

Uses per-bar spreads and cumulative net price PnL (after round-trip cost) —
not a single live spread pasted onto history, and not mere up/down coincidence.

Approval is independent of LIVE trading — engine only expands dim after approve.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .feature_discovery import CANDIDATE_LIBRARY, compute_candidate, _build_library

STORE_KEY = 'feature_validation_log_v1'
MIN_BARS = 120
MIN_FOLDS = 3
MAX_CORR = 0.92
MIN_FOLD_AGREE = 0.6
MIN_NET_PNL = 1e-6  # must beat flat after costs on OOS
HORIZON = 8
MIN_SPREAD_COVERAGE = 0.7  # ≥70% of bars need a real per-bar spread


def _series_for_feature(feature_id, bars, spreads, times):
    _build_library()
    if feature_id not in CANDIDATE_LIBRARY:return None
    values=[]
    for i in range(60,len(bars)):
        use=bars[max(0,i+1-61):i+1]
        if len(use)<61:values.append(np.nan);continue
        sp=spreads[i]
        if sp is None or not math.isfinite(float(sp)):
            values.append(np.nan);continue
        bt=times[i] if i<len(times) else None
        val=compute_candidate(feature_id,use,spread=float(sp),bar_time=bt)
        values.append(np.nan if val is None else val)
    return np.asarray(values,dtype=float)


def _corr(a,b):
    mask=np.isfinite(a)&np.isfinite(b)
    if mask.sum()<40:return None
    x,y=a[mask],b[mask]
    if np.std(x)<1e-12 or np.std(y)<1e-12:return 0.
    return float(np.corrcoef(x,y)[0,1])


def _trade_net(closes, spreads, i, direction, horizon=HORIZON):
    """Net price PnL for one round-trip after entry+exit spread costs."""
    j=i+horizon
    if j>=len(closes):return None
    sp_i=spreads[i];sp_j=spreads[j]
    if sp_i is None or sp_j is None:return None
    try:
        sp_i=float(sp_i);sp_j=float(sp_j)
    except (TypeError,ValueError):
        return None
    if not (math.isfinite(sp_i) and math.isfinite(sp_j)) or sp_i<0 or sp_j<0:
        return None
    sign=1 if direction>0 else -1
    move=sign*(float(closes[j])-float(closes[i]))
    cost=abs(sp_i)+abs(sp_j)
    return move-cost


def net_pnl_curve(feature_values, closes, spreads, *, start, end, horizon=HORIZON):
    """Cumulative net PnL trading sign(feature): +1 long, -1 short, 0 flat."""
    pnls=[]
    # feature_values index 0 ↔ bar index 60
    for local in range(start,end):
        bar_i=60+local
        if bar_i+horizon>=len(closes):break
        x=feature_values[local]
        if not math.isfinite(float(x)) or float(x)==0:continue
        direction=1 if x>0 else -1
        net=_trade_net(closes,spreads,bar_i,direction,horizon=horizon)
        if net is not None:pnls.append(net)
    if not pnls:return None
    return {'n':len(pnls),'net':float(sum(pnls)),'mean':float(np.mean(pnls)),
            'max_dd':float(np.min(np.cumsum(pnls)-np.maximum.accumulate(np.cumsum(pnls))))}


def walk_forward_net_pnl(feature_values, closes, spreads, *, folds=4):
    """Chronological folds scored by cumulative net PnL after per-bar costs."""
    n=len(feature_values)
    if n<MIN_BARS-60 or folds<2:return None
    fold_nets=[];boundaries=np.linspace(0,n,folds+1,dtype=int)
    for f in range(folds):
        start,end=int(boundaries[f]),int(boundaries[f+1])
        curve=net_pnl_curve(feature_values,closes,spreads,start=start,end=end)
        fold_nets.append(None if curve is None or curve['n']<10 else curve['net'])
    usable=[e for e in fold_nets if e is not None]
    if len(usable)<max(2,MIN_FOLDS-1):return None
    signs=[1 if e>0 else (-1 if e<0 else 0) for e in usable]
    majority=max(set(signs),key=signs.count) if signs else 0
    agree=sum(1 for s in signs if s==majority and s!=0)/max(len(usable),1)
    mean_net=float(np.mean(usable))
    return {'fold_nets':fold_nets,'mean_net':mean_net,'agree':agree,
            'stable':agree>=MIN_FOLD_AGREE and mean_net>MIN_NET_PNL}


def _normalize_spreads(spreads, n):
    """Accept list aligned to bars; reject flat-paste of a single live value."""
    if spreads is None:return None,'no per-bar spreads'
    if len(spreads)!=n:return None,'spread length mismatch'
    vals=[]
    for s in spreads:
        try:
            v=float(s) if s is not None else float('nan')
        except (TypeError,ValueError):
            v=float('nan')
        vals.append(v if math.isfinite(v) and v>=0 else float('nan'))
    finite=[v for v in vals if math.isfinite(v)]
    if len(finite)<MIN_SPREAD_COVERAGE*n:
        return None,f'spread coverage {len(finite)}/{n} < {MIN_SPREAD_COVERAGE:.0%}'
    # Detect "one current spread copied onto history": ≥95% identical finite values.
    if len(finite)>=20:
        uniq=len({round(v,10) for v in finite})
        if uniq==1:
            return None,'refusing single spread value pasted across history'
    return vals,None


def validate_feature(feature_id, bars, *, spreads=None, times=None,
                     existing_matrix=None, existing_names=None):
    """Full independent check. bars = list of (o,h,l,c) chronological."""
    _build_library()
    if feature_id not in CANDIDATE_LIBRARY:
        return {'approved':False,'reason':'unknown feature id'}
    if not bars or len(bars)<MIN_BARS:
        return {'approved':False,'reason':f'need ≥{MIN_BARS} completed bars'}
    spreads_n,err=_normalize_spreads(spreads,len(bars))
    if spreads_n is None:
        return {'approved':False,'reason':err}
    times=list(times or [None]*len(bars))
    closes=np.array([b[3] for b in bars],dtype=float)
    feat=_series_for_feature(feature_id,bars,spreads_n,times)
    if feat is None:return {'approved':False,'reason':'series failed'}
    if existing_matrix is not None and len(existing_matrix)==len(feat):
        names=list(existing_names or [])
        for j in range(existing_matrix.shape[1]):
            c=_corr(feat,existing_matrix[:,j])
            if c is not None and abs(c)>=MAX_CORR:
                nm=names[j] if j<len(names) else str(j)
                return {'approved':False,'reason':f'duplicate/collinear with {nm} (|ρ|={abs(c):.2f})',
                        'corr':c,'peer':nm}
    wf=walk_forward_net_pnl(feat,closes,spreads_n,folds=4)
    if wf is None:
        return {'approved':False,'reason':'insufficient walk-forward net-PnL folds'}
    if not wf['stable']:
        return {'approved':False,'reason':'unstable or non-positive OOS net PnL',
                'metrics':wf}
    # Final OOS block cumulative net must stay positive after costs.
    n=len(feat);cut=int(n*.7)
    oos=net_pnl_curve(feat,closes,spreads_n,start=cut,end=n)
    if oos is None or oos['n']<20:
        return {'approved':False,'reason':'OOS too thin for net PnL'}
    if oos['net']<=MIN_NET_PNL:
        return {'approved':False,'reason':'OOS cumulative net PnL not positive after costs',
                'metrics':wf,'oos':oos}
    name,formula,_=CANDIDATE_LIBRARY[feature_id]
    return {
        'approved':True,
        'reason':'walk-forward OOS net PnL positive after per-bar costs',
        'id':feature_id,'name':name,'formula':formula,
        'metrics':{**wf,'oos_net':oos['net'],'oos_trades':oos['n'],'oos_max_dd':oos['max_dd']},
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
            'source':'walk_forward_net_pnl','approved_at':result['approved_at'],
            'metrics':result.get('metrics')})
        result['registry']=why
        if ok:
            self.store.event('validation','Feature approved into registry',
                {'id':feature_id,'name':result['name'],'ohlc_dim':self.registry.ohlc_dim()})
        else:
            result['approved']=False
            result['reason']='validation passed but registry refused: '+why
        return result
