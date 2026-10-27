"""Learnable SL/TP shape from realized cash P&L — grounded in broker OHLC zones.

Risk frame is the adopted lot + market only. Broker min stop is the hard floor.
κ (TP/SL) and sl_scale learn from account-currency outcomes, but explore must
stay inside *recently observed* M1 moves. Random κ=6/8 that last printed weeks
ago is not learning — it is hoping.
"""
from __future__ import annotations
import math
import random
from statistics import mean, median


# Cold start = 1:1. Old DEFAULT_KAPPA=2 locked every open to TP=2×SL.
DEFAULT_KAPPA=1.0
DEFAULT_SL_SCALE=1.0

# Full research grid (evolve may still promote from history).
KAPPA_GRID=(0.75,1.0,1.25,1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0)
SL_SCALE_GRID=(1.0,1.25,1.5,2.0,2.5,3.0,4.0,5.0,6.0,8.0)
# Live explore stays near the OHLC zone — never jump to fantasy TP on day one.
EXPLORE_KAPPA=(0.75,1.0,1.25,1.5,2.0,2.5,3.0)
EXPLORE_SL_SCALE=(1.0,1.25,1.5,2.0,2.5,3.0)
EXPLORE_UNTIL=12  # force mild explore until enough cash outcomes exist


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


def recent_reachable_move(candles, n: int=60) -> float|None:
    """Largest single-bar high−low in the recent M1 window (broker zone, not fantasy).

    TP farther than this has not printed even once in the lookback — do not open
    hoping for a move last seen weeks ago.
    """
    spans=[]
    for row in list(candles or [])[-max(1,int(n)):]:
        try:
            h=float(row['high']);l=float(row['low'])
        except (KeyError,TypeError,ValueError):
            continue
        if h>0 and l>0 and h>=l:
            spans.append(h-l)
    if len(spans)<5:return None
    # Soft cap = max recent bar; also keep a robust 90th-ish via sorted pick.
    spans.sort()
    robust=spans[max(0,int(len(spans)*0.9)-1)]
    return float(max(robust,spans[-1]*0.85))


def _feasible(kappa,sl_scale,*,base_sl: float,max_tp: float|None) -> bool:
    """Reject (κ, scale) whose TP would exceed the recent reachable zone."""
    if max_tp is None or max_tp<=0 or base_sl<=0:return True
    sl=base_sl*float(sl_scale)
    tp=float(kappa)*sl
    return tp<=max_tp*1.05+1e-9  # tiny float cushion


def propose_for_open(store, *, explore_every: int=2, rng=None, ohlc_kappa=None,
                     base_sl: float|None=None, max_tp: float|None=None) -> dict:
    """Next open params — zone-first, mild explore inside reachable TP.

    Uses the same recent OHLC zone the neural path already sees. Does NOT pick
    κ=6/8 just because the static grid lists them.
    """
    cur=load_params(store)
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list):hist=[]
    n=len(hist)
    # Seed champion from OHLC zone whenever we have no cash evidence yet.
    if n==0 and ohlc_kappa and ohlc_kappa>0:
        seeded=float(min(EXPLORE_KAPPA,key=lambda x:abs(x-float(ohlc_kappa))))
        if abs(cur['kappa']-seeded)>1e-9 or store.get('bracket_learn_reason') in (None,''):
            cur={'kappa':seeded,'sl_scale':min(cur['sl_scale'],3.0)}
            save_params(store,cur['kappa'],cur['sl_scale'],reason='ohlc_zone_seed')
    elif n==0 and abs(cur['kappa']-2.0)<1e-9 and store.get('bracket_learn_reason') in (None,''):
        cur={'kappa':DEFAULT_KAPPA,'sl_scale':DEFAULT_SL_SCALE}
        save_params(store,cur['kappa'],cur['sl_scale'],reason='cold_start_1_to_1')

    # Clamp stored champion into the reachable zone (stops unreal TP after Update).
    if base_sl and base_sl>0 and max_tp and max_tp>0:
        if not _feasible(cur['kappa'],cur['sl_scale'],base_sl=base_sl,max_tp=max_tp):
            # Shrink κ then sl_scale until TP fits the recent zone.
            k=float(cur['kappa']);s=float(cur['sl_scale'])
            for kk in sorted(EXPLORE_KAPPA,reverse=True):
                if _feasible(kk,s,base_sl=base_sl,max_tp=max_tp):
                    k=kk;break
            else:
                k=min(EXPLORE_KAPPA)
            for ss in sorted(EXPLORE_SL_SCALE,reverse=True):
                if _feasible(k,ss,base_sl=base_sl,max_tp=max_tp):
                    s=ss;break
            else:
                s=1.0
            cur={'kappa':k,'sl_scale':s}
            save_params(store,k,s,reason='clamped_to_recent_ohlc_zone')

    force=n<EXPLORE_UNTIL
    if not force and (n % max(1,explore_every))!=0:
        return {**cur,'explored':False,'max_tp':max_tp,'base_sl':base_sl}

    counts=_counts(hist)
    cells=[(k,s) for k in EXPLORE_KAPPA for s in EXPLORE_SL_SCALE]
    if base_sl and max_tp:
        feasible=[ks for ks in cells if _feasible(ks[0],ks[1],base_sl=base_sl,max_tp=max_tp)]
        if feasible:cells=feasible
    # Prefer cells near the OHLC κ hint (adapt to the zone already given).
    if ohlc_kappa and ohlc_kappa>0:
        cells.sort(key=lambda ks:(counts.get(ks,0),abs(ks[0]-float(ohlc_kappa)),ks[0],ks[1]))
    else:
        cells.sort(key=lambda ks:(counts.get(ks,0),abs(ks[0]-float(cur['kappa'])),ks[0],ks[1]))
    if not cells:
        return {**cur,'explored':False,'max_tp':max_tp,'base_sl':base_sl}
    least=counts.get(cells[0],0)
    # Among least-tried, keep only those close to the zone κ (top band).
    band=[ks for ks in cells if counts.get(ks,0)==least]
    if ohlc_kappa and ohlc_kappa>0 and len(band)>1:
        near=[ks for ks in band if abs(ks[0]-float(ohlc_kappa))<=1.0+1e-9]
        if near:band=near
    pick=(rng or random).choice(band)
    return {'kappa':float(pick[0]),'sl_scale':float(pick[1]),'explored':True,
            'max_tp':max_tp,'base_sl':base_sl,'ohlc_kappa':ohlc_kappa}


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
        # Do not re-promote fantasy κ that live explore no longer uses unless
        # they truly dominate — still allow history winners inside full grid.
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
