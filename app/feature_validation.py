"""Walk-Forward / Out-of-Sample validation for discovered features.

Uses per-bar spreads and cumulative net price PnL after **one** round-trip
cost via ``trade_costs.fill_to_fill_pnl`` (half-spread in + half-spread out;
never |sp_i|+|sp_j| as two full widths). Not a single live spread pasted onto
history, and not mere up/down coincidence.

Approval is independent of LIVE trading — engine only expands dim after approve.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .feature_discovery import CANDIDATE_LIBRARY, compute_candidate, _build_library

STORE_KEY = 'feature_validation_log_v1'


def _cfg(store=None):
    from .self_params import load
    return load(store)


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


def _trade_net(closes, spreads, i, direction, horizon):
    """Net price PnL for one round-trip after shared RT cost (no double spread)."""
    from .trade_costs import fill_to_fill_pnl
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
    side='BUY' if direction>0 else 'SELL'
    return fill_to_fill_pnl(side,float(closes[i]),float(closes[j]),
                            spread_entry=sp_i,spread_exit=sp_j)


def net_pnl_curve(feature_values, closes, spreads, *, start, end, horizon=8):
    """Cumulative net PnL trading sign(feature): +1 long, -1 short, 0 flat."""
    pnls=[]
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


def walk_forward_net_pnl(feature_values, closes, spreads, *, folds=None, store=None):
    """Chronological folds scored by cumulative net PnL after per-bar costs."""
    cfg=_cfg(store)
    folds=int(cfg['val_min_folds']) if folds is None else int(folds)
    horizon=int(cfg['val_horizon_bars'])
    min_agree=float(cfg['val_min_fold_agree'])
    min_net=float(cfg['val_min_net_pnl'])
    min_bars=int(cfg['val_min_bars'])
    n=len(feature_values)
    if n<min_bars-60 or folds<2:return None
    fold_nets=[];boundaries=np.linspace(0,n,folds+1,dtype=int)
    for f in range(folds):
        start,end=int(boundaries[f]),int(boundaries[f+1])
        curve=net_pnl_curve(feature_values,closes,spreads,start=start,end=end,horizon=horizon)
        fold_nets.append(None if curve is None or curve['n']<10 else curve['net'])
    usable=[e for e in fold_nets if e is not None]
    if len(usable)<max(2,folds-1):return None
    signs=[1 if e>0 else (-1 if e<0 else 0) for e in usable]
    majority=max(set(signs),key=signs.count) if signs else 0
    agree=sum(1 for s in signs if s==majority and s!=0)/max(len(usable),1)
    mean_net=float(np.mean(usable))
    return {'fold_nets':fold_nets,'mean_net':mean_net,'agree':agree,
            'stable':agree>=min_agree and mean_net>min_net,
            'horizon':horizon}


def _normalize_spreads(spreads, n, *, store=None):
    """Accept list aligned to bars; reject flat-paste of a single live value."""
    cfg=_cfg(store)
    coverage=float(cfg['val_min_spread_coverage'])
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
    if len(finite)<coverage*n:
        return None,f'spread coverage {len(finite)}/{n} < {coverage:.0%}'
    if len(finite)>=20:
        uniq=len({round(v,10) for v in finite})
        if uniq==1:
            return None,'refusing single spread value pasted across history'
    return vals,None


def validate_feature(feature_id, bars, *, spreads=None, times=None,
                     existing_matrix=None, existing_names=None,
                     existing_indices=None, store=None):
    """Full independent check. bars = list of (o,h,l,c) chronological."""
    cfg=_cfg(store)
    _build_library(horizons=cfg.get('gen_horizons'))
    if feature_id not in CANDIDATE_LIBRARY:
        return {'approved':False,'reason':'unknown feature id'}
    min_bars=int(cfg['val_min_bars'])
    if not bars or len(bars)<min_bars:
        return {'approved':False,'reason':f'need ≥{min_bars} completed bars'}
    spreads_n,err=_normalize_spreads(spreads,len(bars),store=store)
    if spreads_n is None:
        return {'approved':False,'reason':err}
    times=list(times or [None]*len(bars))
    closes=np.array([b[3] for b in bars],dtype=float)
    feat=_series_for_feature(feature_id,bars,spreads_n,times)
    if feat is None:return {'approved':False,'reason':'series failed'}
    max_corr=float(cfg['val_max_corr'])
    if existing_matrix is not None:
        mat=np.asarray(existing_matrix,dtype=float)
        names=list(existing_names or [])
        if mat.ndim==2 and mat.shape[0]>=20:
            # Prefer values on the same historical windows as the matrix
            # (never compare against a tiled single-bar snapshot).
            if existing_indices and len(existing_indices)==mat.shape[0]:
                from .historical_features import candidate_at_indices
                cand=candidate_at_indices(feature_id,bars,spreads_n,times,existing_indices)
            elif len(mat)==len(feat):
                cand=feat
            else:
                cand=None
            if cand is not None and len(cand)==mat.shape[0]:
                for j in range(mat.shape[1]):
                    c=_corr(cand,mat[:,j])
                    if c is not None and abs(c)>=max_corr:
                        nm=names[j] if j<len(names) else str(j)
                        return {'approved':False,
                                'reason':f'duplicate/collinear with {nm} (|ρ|={abs(c):.2f})',
                                'corr':c,'peer':nm}
    wf=walk_forward_net_pnl(feat,closes,spreads_n,store=store)
    if wf is None:
        return {'approved':False,'reason':'insufficient walk-forward net-PnL folds'}
    if not wf['stable']:
        return {'approved':False,'reason':'unstable or non-positive OOS net PnL',
                'metrics':wf}
    n=len(feat);cut=int(n*float(cfg['val_oos_frac']))
    horizon=int(cfg['val_horizon_bars'])
    oos=net_pnl_curve(feat,closes,spreads_n,start=cut,end=n,horizon=horizon)
    if oos is None or oos['n']<20:
        return {'approved':False,'reason':'OOS too thin for net PnL'}
    if oos['net']<=float(cfg['val_min_net_pnl']):
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
    def __init__(self, store, registry, discovery=None):
        self.store=store
        self.registry=registry
        self._discovery=discovery
        self.log=list(self.store.get(STORE_KEY) or [])

    def _persist(self, entry):
        self.log.append(entry)
        self.store.set(STORE_KEY,self.log[-80:])

    def validate_and_maybe_approve(self, feature_id, bars, *, spreads=None, times=None,
                                   existing_matrix=None, existing_names=None,
                                   existing_indices=None):
        from .self_params import evolve_from_feedback
        result=validate_feature(feature_id,bars,spreads=spreads,times=times,
                                existing_matrix=existing_matrix,existing_names=existing_names,
                                existing_indices=existing_indices,store=self.store)
        entry={'feature_id':feature_id,'result':result,
               'at':datetime.now(timezone.utc).isoformat()}
        self._persist(entry)
        # Discovery rotation: always inform the proposer (skip cool-down / clear).
        discovery=getattr(self,'_discovery',None)
        if not result.get('approved'):
            self.store.event('validation','Feature rejected',
                {'id':feature_id,'reason':result.get('reason')})
            if discovery is not None:
                discovery.mark_rejected(feature_id,result.get('reason'))
            # Param nudges only via long-horizon gate in self_params (queued evidence).
            evolve_from_feedback(self.store,feature_rejected=result,commit=False)
            return result
        ok,why=self.registry.approve({
            'id':result['id'],'name':result['name'],'formula':result['formula'],
            'source':'walk_forward_net_pnl','approved_at':result['approved_at'],
            'metrics':result.get('metrics')})
        result['registry']=why
        if ok:
            self.store.event('validation','Feature approved into registry',
                {'id':feature_id,'name':result['name'],'ohlc_dim':self.registry.ohlc_dim()})
            if discovery is not None:
                discovery.mark_approved(feature_id)
            evolve_from_feedback(self.store,feature_approved=feature_id,commit=False)
        else:
            result['approved']=False
            result['reason']='validation passed but registry refused: '+why
            if discovery is not None:
                discovery.mark_rejected(feature_id,why)
            evolve_from_feedback(self.store,feature_rejected=result,commit=False)
        return result
