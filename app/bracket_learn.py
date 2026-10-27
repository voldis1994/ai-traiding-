"""Learnable SL/TP shape from realized cash P&L.

Risk frame is the adopted lot + market only. We do NOT invent extra 'safety'
entry filters. Broker min stop distance is the only hard floor (dealing rules).

Bracket parameters (sl_scale, kappa) are chosen by what historically produced
better account-currency P&L — not by fixed κ=2 forever.
"""
from __future__ import annotations
import math
import random
from statistics import mean


# Cold start only — replaced as soon as enough cash outcomes exist.
DEFAULT_KAPPA=2.0
DEFAULT_SL_SCALE=1.0

# Search grid (discrete learners, not frozen live constants).
KAPPA_GRID=(0.75,1.0,1.25,1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0)
SL_SCALE_GRID=(1.0,1.25,1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0)


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def load_params(store) -> dict:
    kappa=_finite(store.get('bracket_kappa',DEFAULT_KAPPA)) or DEFAULT_KAPPA
    sl_scale=_finite(store.get('bracket_sl_scale',DEFAULT_SL_SCALE)) or DEFAULT_SL_SCALE
    if kappa<=0:kappa=DEFAULT_KAPPA
    if sl_scale<1:sl_scale=1.0
    return {'kappa':kappa,'sl_scale':sl_scale}


def save_params(store,kappa: float,sl_scale: float,reason: str=''):
    store.set('bracket_kappa',float(kappa))
    store.set('bracket_sl_scale',float(sl_scale))
    store.set('bracket_learn_reason',reason or 'updated')


def record_outcome(store, *, kappa, sl_scale, pnl, currency=None, deal_id=None):
    """Append one cash outcome tagged with the brackets that opened the trade."""
    pnl_n=_finite(pnl)
    if pnl_n is None:return
    k=_finite(kappa);s=_finite(sl_scale)
    if k is None or s is None or k<=0 or s<1:return
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list):hist=[]
    hist.append({
        'kappa':float(k),'sl_scale':float(s),'pnl':float(pnl_n),
        'currency':currency,'deal_id':deal_id,
    })
    store.set('bracket_outcome_history',hist[-200:])


def _bucket(kappa,sl_scale):
    # Nearest grid node so sparse history still aggregates.
    k=min(KAPPA_GRID,key=lambda x:abs(x-float(kappa)))
    s=min(SL_SCALE_GRID,key=lambda x:abs(x-float(sl_scale)))
    return k,s


def _counts(hist) -> dict:
    buckets={}
    for row in hist or []:
        k=_finite(row.get('kappa'));s=_finite(row.get('sl_scale'))
        if k is None or s is None:continue
        key=_bucket(k,s)
        buckets[key]=buckets.get(key,0)+1
    return buckets


def propose_for_open(store, *, explore_every: int=3, rng=None) -> dict:
    """Params for the next open: mostly current champion, periodically explore.

    Without exploration history stays stuck on the cold-start (κ=2, scale=1).
    Lot + market remain the risk frame; this only shapes SL/TP distance.
    """
    cur=load_params(store)
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list):hist=[]
    n=len(hist)
    # Explore early and every Nth closed outcome thereafter.
    if n>=4 and (n % max(1,explore_every))!=0:
        return {**cur,'explored':False}
    counts=_counts(hist)
    # Prefer under-sampled cells; never go below broker-min floor (sl_scale>=1).
    cells=[(k,s) for k in KAPPA_GRID for s in SL_SCALE_GRID]
    cells.sort(key=lambda ks:(counts.get(ks,0),ks[0],ks[1]))
    least=counts.get(cells[0],0) if cells else 0
    candidates=[ks for ks in cells if counts.get(ks,0)==least]
    pick=(rng or random).choice(candidates) if candidates else (cur['kappa'],cur['sl_scale'])
    return {'kappa':float(pick[0]),'sl_scale':float(pick[1]),'explored':True}


def evolve_from_history(store, *, min_samples: int=8) -> dict|None:
    """Pick (kappa, sl_scale) with best mean cash P&L; promote if better than current."""
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list) or len(hist)<min_samples:
        return None
    buckets={}
    for row in hist:
        pnl=_finite(row.get('pnl'))
        k=_finite(row.get('kappa'));s=_finite(row.get('sl_scale'))
        if pnl is None or k is None or s is None:continue
        key=_bucket(k,s)
        buckets.setdefault(key,[]).append(pnl)
    scored=[]
    for (k,s),pnls in buckets.items():
        if len(pnls)<2:continue
        scored.append((mean(pnls),len(pnls),k,s))
    if not scored:
        return None
    # Prefer higher mean P&L; tie-break: more samples, then wider exploration.
    scored.sort(key=lambda t:(t[0],t[1],t[2],t[3]),reverse=True)
    best_mean,n,k,s=scored[0]
    cur=load_params(store)
    cur_key=_bucket(cur['kappa'],cur['sl_scale'])
    cur_pnls=buckets.get(cur_key,[])
    cur_mean=mean(cur_pnls) if cur_pnls else None
    if cur_mean is not None and best_mean<=cur_mean+1e-9 and (k,s)==cur_key:
        return None
    if cur_mean is not None and best_mean<=cur_mean:
        return None
    save_params(store,k,s,reason=f'promoted mean_pnl={best_mean:.4f} n={n}')
    return {'kappa':k,'sl_scale':s,'mean_pnl':best_mean,'samples':n}
