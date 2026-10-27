"""Learnable SL/TP shape from realized cash P&L.

Risk frame is the adopted lot + market only. Broker min stop is the hard floor.
κ (TP/SL) and sl_scale are learned from account-currency outcomes — cold start
is 1:1, NOT a frozen 1:2 invent.
"""
from __future__ import annotations
import math
import random
from statistics import mean, median


# Cold start = 1:1. Old DEFAULT_KAPPA=2 locked every open to TP=2×SL.
DEFAULT_KAPPA=1.0
DEFAULT_SL_SCALE=1.0

KAPPA_GRID=(0.75,1.0,1.25,1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0)
SL_SCALE_GRID=(1.0,1.25,1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0)
EXPLORE_UNTIL=12  # force explore until enough cash outcomes exist


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


def kappa_from_ohlc(candles, n: int=20) -> float|None:
    """Broker M1 upside/downside median ratio → nearest κ grid (not a magic 2)."""
    ups=[];downs=[]
    for row in list(candles or [])[-max(1,int(n)):]:
        try:
            o=float(row['open']);h=float(row['high']);l=float(row['low']);c=float(row['close'])
        except (KeyError,TypeError,ValueError):
            continue
        if min(o,h,l,c)<=0 or h<l:continue
        ups.append(max(0.,h-min(o,c)))
        downs.append(max(0.,max(o,c)-l))
    if len(ups)<5 or len(downs)<5:return None
    du=median(downs)
    if du<=0:return None
    raw=median(ups)/du
    if not math.isfinite(raw) or raw<=0:return None
    return float(min(KAPPA_GRID,key=lambda x:abs(x-raw)))


def propose_for_open(store, *, explore_every: int=2, rng=None, ohlc_kappa=None) -> dict:
    """Next open params: explore hard until cash history exists; then exploit.

    If SQLite still has a stale κ=2 champion with no outcomes, prefer OHLC κ hint.
    """
    cur=load_params(store)
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list):hist=[]
    n=len(hist)
    # Stale frozen 1:2 with no evidence — drop to OHLC hint or 1:1.
    if n==0 and abs(cur['kappa']-2.0)<1e-9 and store.get('bracket_learn_reason') in (None,'',):
        if ohlc_kappa and ohlc_kappa>0:
            cur={'kappa':float(ohlc_kappa),'sl_scale':cur['sl_scale']}
            save_params(store,cur['kappa'],cur['sl_scale'],reason='ohlc_kappa_hint')
        elif abs(cur['kappa']-DEFAULT_KAPPA)>1e-9:
            cur={'kappa':DEFAULT_KAPPA,'sl_scale':cur['sl_scale']}
            save_params(store,cur['kappa'],cur['sl_scale'],reason='cold_start_1_to_1')
    # Force exploration until enough broker cash samples exist.
    force=n<EXPLORE_UNTIL
    if not force and (n % max(1,explore_every))!=0:
        return {**cur,'explored':False}
    counts=_counts(hist)
    cells=[(k,s) for k in KAPPA_GRID for s in SL_SCALE_GRID]
    cells.sort(key=lambda ks:(counts.get(ks,0),ks[0],ks[1]))
    least=counts.get(cells[0],0) if cells else 0
    candidates=[ks for ks in cells if counts.get(ks,0)==least]
    # Bias first explores away from pure κ=2 so we do not re-lock 1:2.
    if force and n<4:
        non_two=[ks for ks in candidates if abs(ks[0]-2.0)>1e-9]
        if non_two:candidates=non_two
    pick=(rng or random).choice(candidates) if candidates else (cur['kappa'],cur['sl_scale'])
    return {'kappa':float(pick[0]),'sl_scale':float(pick[1]),'explored':True}


def evolve_from_history(store, *, min_samples: int=6) -> dict|None:
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
