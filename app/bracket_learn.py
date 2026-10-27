"""SL/TP from OHLC at open + real cash learning when to raise / cut.

Open moment: distances come from recent M1 adverse/favorable moves (same candles
the neural net sees). After each broker £ result the robot *learns*:
  - losses / scratches → raise SL room (or cut unreachable TP)
  - wins → keep / gently reinforce the shape that paid

No hardcoded κ lottery. Learning is the adaptation layer, not a 5% cosmetic nudge.
"""
from __future__ import annotations
import math
from statistics import mean, median


DEFAULT_KAPPA=1.0
DEFAULT_SL_SCALE=1.0
# Learned multipliers on top of the live OHLC zone (raised/cut from cash).
MULT_KAPPA_MIN,MULT_KAPPA_MAX=0.5,2.5
MULT_SL_MIN,MULT_SL_MAX=1.0,2.5
MIN_BATCH_SAMPLES=6


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def _clip(v,lo,hi):
    return float(max(lo,min(hi,float(v))))


def load_params(store) -> dict:
    """Effective κ / sl_scale last used (for tagging closed trades)."""
    kappa=_finite(store.get('bracket_kappa',DEFAULT_KAPPA)) or DEFAULT_KAPPA
    sl_scale=_finite(store.get('bracket_sl_scale',DEFAULT_SL_SCALE)) or DEFAULT_SL_SCALE
    if kappa<=0:kappa=DEFAULT_KAPPA
    if sl_scale<1:sl_scale=1.0
    return {'kappa':kappa,'sl_scale':sl_scale}


def load_multipliers(store) -> dict:
    """What the robot has learned: raise (>1) or cut (<1) relative to the zone."""
    k=_finite(store.get('bracket_mult_kappa',1.0)) or 1.0
    s=_finite(store.get('bracket_mult_sl',1.0)) or 1.0
    return {
        'kappa':_clip(k,MULT_KAPPA_MIN,MULT_KAPPA_MAX),
        'sl_scale':_clip(max(1.0,s),MULT_SL_MIN,MULT_SL_MAX),
    }


def save_params(store,kappa: float,sl_scale: float,reason: str=''):
    store.set('bracket_kappa',float(kappa))
    store.set('bracket_sl_scale',float(sl_scale))
    store.set('bracket_learn_reason',reason or 'updated')


def save_multipliers(store,kappa_mult: float,sl_mult: float,reason: str=''):
    store.set('bracket_mult_kappa',_clip(kappa_mult,MULT_KAPPA_MIN,MULT_KAPPA_MAX))
    store.set('bracket_mult_sl',_clip(max(1.0,sl_mult),MULT_SL_MIN,MULT_SL_MAX))
    if reason:
        store.set('bracket_learn_reason',reason)


def record_outcome(store, *, kappa, sl_scale, pnl, currency=None, deal_id=None):
    """Append cash outcome and immediately learn raise/cut from that result."""
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
    adapt_after_outcome(store,kappa=float(k),sl_scale=float(s),pnl=float(pnl_n))


def adapt_after_outcome(store, *, kappa: float, sl_scale: float, pnl: float) -> dict:
    """Learn from one closed trade: when to raise SL/TP room, when to cut TP.

    Signals (broker cash in account currency):
      scratch |pnl|≤0.05 → SL too tight → raise sl_mult
      loss < -0.05 with tight shape → raise sl_mult (need more room)
      loss with already-wide shape → cut kappa_mult (TP was fantasy; SL hit first)
      win > 0.05 → reinforce current multipliers slightly
    """
    mult=load_multipliers(store)
    k_m,s_m=mult['kappa'],mult['sl_scale']
    shape=float(kappa)*float(sl_scale)
    reason='hold'
    if abs(pnl)<=0.05:
        s_m=min(MULT_SL_MAX,s_m*1.10)
        reason='scratch_raise_sl'
    elif pnl<-0.05:
        if shape<1.6:
            s_m=min(MULT_SL_MAX,s_m*1.12)
            reason='loss_tight_raise_sl'
        else:
            k_m=max(MULT_KAPPA_MIN,k_m*0.90)
            reason='loss_wide_cut_tp'
    elif pnl>0.05:
        k_m=min(MULT_KAPPA_MAX,k_m*1.05)
        s_m=min(MULT_SL_MAX,s_m*1.03)
        reason='win_reinforce'
    save_multipliers(store,k_m,s_m,reason=f'learned_{reason} pnl={pnl:.4f}')
    return {'kappa_mult':k_m,'sl_mult':s_m,'reason':reason,'pnl':pnl}


def _bar_moves(candles, n: int=40):
    ups=[];downs=[];spans=[]
    for row in list(candles or [])[-max(1,int(n)):]:
        try:
            o=float(row['open']);h=float(row['high']);l=float(row['low']);c=float(row['close'])
        except (KeyError,TypeError,ValueError):
            continue
        if min(o,h,l,c)<=0 or h<l:continue
        ups.append(max(0.,h-min(o,c)))
        downs.append(max(0.,max(o,c)-l))
        spans.append(h-l)
    return ups,downs,spans


def kappa_from_ohlc(candles, n: int=40) -> float|None:
    ups,downs,_=_bar_moves(candles,n)
    if len(ups)<5 or len(downs)<5:return None
    du=median(downs)
    if du<=0:return None
    raw=median(ups)/du
    if not math.isfinite(raw) or raw<=0:return None
    return float(raw)


def recent_reachable_move(candles, n: int=60) -> float|None:
    _,_,spans=_bar_moves(candles,n)
    if len(spans)<5:return None
    spans=sorted(spans)
    robust=spans[max(0,int(len(spans)*0.9)-1)]
    return float(max(robust,spans[-1]*0.85))


def zone_at_open(candles, *, n: int=40, reach_n: int=60) -> dict:
    """SL/TP shape from recent OHLC at the open — the information already on hand."""
    ups,downs,spans=_bar_moves(candles,n)
    reach=recent_reachable_move(candles,reach_n)
    if len(spans)<5:
        return {'kappa':DEFAULT_KAPPA,'range_distance':0.0,'max_tp':reach,
                'median_up':None,'median_down':None,'ready':False}
    med_span=float(median(spans))
    med_up=float(median(ups)) if ups else med_span
    med_down=float(median(downs)) if downs else med_span
    if med_down<=1e-12:
        kappa=DEFAULT_KAPPA
    else:
        kappa=med_up/med_down
    if not math.isfinite(kappa) or kappa<=0:
        kappa=DEFAULT_KAPPA
    kappa=max(float(kappa),0.5)
    return {
        'kappa':float(kappa),
        'range_distance':med_span,
        'max_tp':float(reach) if reach is not None else None,
        'median_up':med_up,'median_down':med_down,
        'ready':True,
    }


def propose_for_open(store, *, explore_every: int=2, rng=None, ohlc_kappa=None,
                     base_sl: float|None=None, max_tp: float|None=None,
                     candles=None) -> dict:
    """Zone distances × learned raise/cut multipliers; TP still capped by reach."""
    zone=zone_at_open(candles or [],n=40,reach_n=60) if candles is not None else {
        'kappa':float(ohlc_kappa) if ohlc_kappa and ohlc_kappa>0 else DEFAULT_KAPPA,
        'range_distance':float(base_sl or 0),'max_tp':max_tp,'ready':bool(ohlc_kappa),
    }
    if max_tp is None:
        max_tp=zone.get('max_tp')
    kappa=float(zone.get('kappa') or DEFAULT_KAPPA)
    if ohlc_kappa and ohlc_kappa>0:
        kappa=0.5*kappa+0.5*float(ohlc_kappa)
    learned=load_multipliers(store)
    kappa*=learned['kappa']
    sl_scale=max(1.0,float(learned['sl_scale']))
    base=float(base_sl or zone.get('range_distance') or 0)
    if base>0 and max_tp and max_tp>0:
        room=max_tp/(base*sl_scale)
        if kappa>room:
            kappa=max(0.5,room)
    save_params(store,kappa,sl_scale,
                reason=(f"ohlc×learn k_mult={learned['kappa']:.3f} "
                        f"sl_mult={learned['sl_scale']:.3f}"))
    return {
        'kappa':float(kappa),'sl_scale':float(sl_scale),'explored':False,
        'max_tp':max_tp,'base_sl':base_sl or zone.get('range_distance'),
        'ohlc_kappa':ohlc_kappa,'zone_ready':bool(zone.get('ready')),
        'median_up':zone.get('median_up'),'median_down':zone.get('median_down'),
        'kappa_mult':learned['kappa'],'sl_mult':learned['sl_scale'],
        'nudge_samples':len(store.get('bracket_outcome_history',[]) or []),
    }


def evolve_from_history(store, *, min_samples: int=MIN_BATCH_SAMPLES) -> dict|None:
    """Batch confirm: if wide brackets earn more £ than tight, raise; else cut TP/SL."""
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list) or len(hist)<min_samples:
        return None
    wide=[];tight=[]
    for row in hist:
        pnl=_finite(row.get('pnl'));k=_finite(row.get('kappa'));s=_finite(row.get('sl_scale'))
        if pnl is None or k is None or s is None:continue
        (wide if k*s>=1.5 else tight).append(pnl)
    if len(wide)<2 or len(tight)<2:
        return None
    mw,mt=mean(wide),mean(tight)
    mult=load_multipliers(store)
    k_m,s_m=mult['kappa'],mult['sl_scale']
    if mw>mt+1e-9:
        k_m=min(MULT_KAPPA_MAX,k_m*1.08)
        s_m=min(MULT_SL_MAX,s_m*1.08)
        reason=f'batch_raise wide_mean={mw:.4f} tight_mean={mt:.4f}'
    elif mt>mw+1e-9:
        k_m=max(MULT_KAPPA_MIN,k_m*0.92)
        # Keep SL floor ≥1; cut TP ambition when tight wins.
        reason=f'batch_cut_tp wide_mean={mw:.4f} tight_mean={mt:.4f}'
    else:
        return None
    before=dict(mult)
    save_multipliers(store,k_m,s_m,reason=reason)
    if abs(k_m-before['kappa'])<1e-6 and abs(s_m-before['sl_scale'])<1e-6:
        return None
    save_params(store,k_m,s_m,reason=reason)
    return {'kappa':k_m,'sl_scale':s_m,'mean_pnl':mw,'samples':len(wide)+len(tight)}
