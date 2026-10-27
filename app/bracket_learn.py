"""SL/TP from broker OHLC at open — not a hardcoded κ lottery.

At the open moment the robot already has the same M1 candles the neural net
uses. Stop and take distances are *computed* from recent adverse / favorable
moves. Cash P&L only applies a mild continuous nudge to those zone distances
after enough outcomes — it does not pick κ=6 from a static grid and hope.
"""
from __future__ import annotations
import math
from statistics import mean, median


DEFAULT_KAPPA=1.0
DEFAULT_SL_SCALE=1.0
# Continuous nudge bounds after cash evidence (not a discrete explore menu).
NUDGE_KAPPA_MIN,NUDGE_KAPPA_MAX=0.7,1.4
NUDGE_SL_MIN,NUDGE_SL_MAX=0.85,1.6
MIN_NUDGE_SAMPLES=8


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def load_params(store) -> dict:
    """Persisted *nudges* relative to the live OHLC zone (default = neutral 1/1)."""
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
    """Zone κ = median favorable / median adverse on recent M1 (continuous)."""
    ups,downs,_= _bar_moves(candles,n)
    if len(ups)<5 or len(downs)<5:return None
    du=median(downs)
    if du<=0:return None
    raw=median(ups)/du
    if not math.isfinite(raw) or raw<=0:return None
    return float(raw)


def recent_reachable_move(candles, n: int=60) -> float|None:
    """Largest recent M1 high−low — hard ceiling for TP at this open."""
    _,_,spans=_bar_moves(candles,n)
    if len(spans)<5:return None
    spans=sorted(spans)
    robust=spans[max(0,int(len(spans)*0.9)-1)]
    return float(max(robust,spans[-1]*0.85))


def zone_at_open(candles, *, n: int=40, reach_n: int=60) -> dict:
    """Optimal-ish SL/TP shape from all recent OHLC at the open moment.

    SL distance proxy = median bar range (noise floor the market just printed).
    κ = median upside / median downside (asymmetric zone, not a table pick).
    max_tp = recent reachable move (do not target a move absent from the window).
    """
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
    # Soft floor: at least ~1:1 against the median bar; never invent κ from a table.
    kappa=max(float(kappa),0.5)
    return {
        'kappa':float(kappa),
        'range_distance':med_span,
        'max_tp':float(reach) if reach is not None else None,
        'median_up':med_up,'median_down':med_down,
        'ready':True,
    }


def _cash_nudge(store) -> dict:
    """Mild continuous scale from cash P&L — never a discrete fantasy grid."""
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list) or len(hist)<MIN_NUDGE_SAMPLES:
        return {'kappa':1.0,'sl_scale':1.0,'samples':len(hist) if isinstance(hist,list) else 0}
    # Wider κ/sl correlated with better mean cash → nudge up; else down.
    wide=[];tight=[]
    for row in hist:
        pnl=_finite(row.get('pnl'));k=_finite(row.get('kappa'));s=_finite(row.get('sl_scale'))
        if pnl is None or k is None or s is None:continue
        shape=k*s
        (wide if shape>=1.5 else tight).append(pnl)
    k_nudge=1.0;s_nudge=1.0
    if len(wide)>=3 and len(tight)>=3:
        mw,mt=mean(wide),mean(tight)
        if mw>mt+1e-9:
            k_nudge=min(NUDGE_KAPPA_MAX,1.0+(mw-mt)/(abs(mt)+0.05)*0.15)
            s_nudge=min(NUDGE_SL_MAX,1.0+(mw-mt)/(abs(mt)+0.05)*0.1)
        elif mt>mw+1e-9:
            k_nudge=max(NUDGE_KAPPA_MIN,1.0-(mt-mw)/(abs(mw)+0.05)*0.15)
            s_nudge=max(NUDGE_SL_MIN,1.0-(mt-mw)/(abs(mw)+0.05)*0.1)
    # Blend with stored champion lightly so promote still matters.
    stored=load_params(store)
    k_nudge=0.7*k_nudge+0.3*max(NUDGE_KAPPA_MIN,min(NUDGE_KAPPA_MAX,stored['kappa']))
    s_nudge=0.7*s_nudge+0.3*max(NUDGE_SL_MIN,min(NUDGE_SL_MAX,stored['sl_scale']))
    return {'kappa':float(k_nudge),'sl_scale':float(s_nudge),'samples':len(hist)}


def propose_for_open(store, *, explore_every: int=2, rng=None, ohlc_kappa=None,
                     base_sl: float|None=None, max_tp: float|None=None,
                     candles=None) -> dict:
    """Compute κ / sl_scale from the open-time zone; cash only nudges.

    `explore_every` / `rng` kept for call-site compatibility — unused (no lottery).
    """
    zone=zone_at_open(candles or [],n=40,reach_n=60) if candles is not None else {
        'kappa':float(ohlc_kappa) if ohlc_kappa and ohlc_kappa>0 else DEFAULT_KAPPA,
        'range_distance':float(base_sl or 0),'max_tp':max_tp,'ready':bool(ohlc_kappa),
    }
    if max_tp is None:
        max_tp=zone.get('max_tp')
    kappa=float(zone.get('kappa') or DEFAULT_KAPPA)
    if ohlc_kappa and ohlc_kappa>0:
        # Average continuous OHLC κ with the richer zone_at_open κ when both exist.
        kappa=0.5*kappa+0.5*float(ohlc_kappa)
    nudge=_cash_nudge(store)
    kappa*=nudge['kappa']
    sl_scale=max(1.0,float(nudge['sl_scale']))
    # Final TP must fit the reachable ceiling when we know base SL.
    base=float(base_sl or zone.get('range_distance') or 0)
    if base>0 and max_tp and max_tp>0:
        # κ * base * sl_scale ≤ max_tp
        room=max_tp/(base*sl_scale)
        if kappa>room:
            kappa=max(0.5,room)
    # Persist the computed shape so closed-trade learning tags the real open.
    save_params(store,kappa,sl_scale,
                reason=f'ohlc_zone_open nudge_samples={nudge["samples"]}')
    return {
        'kappa':float(kappa),'sl_scale':float(sl_scale),'explored':False,
        'max_tp':max_tp,'base_sl':base_sl or zone.get('range_distance'),
        'ohlc_kappa':ohlc_kappa,'zone_ready':bool(zone.get('ready')),
        'median_up':zone.get('median_up'),'median_down':zone.get('median_down'),
        'nudge_samples':nudge['samples'],
    }


def evolve_from_history(store, *, min_samples: int=6) -> dict|None:
    """Update continuous nudges from cash P&L buckets (wide vs tight), not a grid crown."""
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
    cur=load_params(store)
    if mw>mt+1e-9:
        k=min(NUDGE_KAPPA_MAX,max(cur['kappa'],1.05))
        s=min(NUDGE_SL_MAX,max(cur['sl_scale'],1.05))
    elif mt>mw+1e-9:
        k=max(NUDGE_KAPPA_MIN,min(cur['kappa'],0.95))
        s=max(1.0,min(cur['sl_scale'],0.95))
    else:
        return None
    if abs(k-cur['kappa'])<1e-6 and abs(s-cur['sl_scale'])<1e-6:
        return None
    save_params(store,k,s,reason=f'cash_nudge wide_mean={mw:.4f} tight_mean={mt:.4f}')
    return {'kappa':k,'sl_scale':s,'mean_pnl':mw,'samples':len(wide)+len(tight)}
