"""SL/TP from OHLC at open + real cash learning when to raise / cut.

Open moment: distances come from recent M1 adverse/favorable moves (same candles
the neural net sees). After each broker £ result the robot *learns* — but only
after separately analysing exit reason, entry direction, price path, volatility,
and costs (audit #19). A loss alone must NOT auto-conclude that SL was too tight.

No hardcoded κ lottery. Learning is the adaptation layer, not a 5% cosmetic nudge.
"""
from __future__ import annotations
import math
from statistics import mean, median


DEFAULT_KAPPA=1.0
DEFAULT_SL_SCALE=1.0
# Soft rails on learned multipliers — operating schedule lives in self_params.
MULT_KAPPA_MIN,MULT_KAPPA_MAX=0.5,2.5
MULT_SL_MIN,MULT_SL_MAX=1.0,2.5
# Seed aliases (tests / no-store callers); Store overrides via _schedule().
MIN_BATCH_SAMPLES=6
# Exit reasons the adaptation layer understands.
EXIT_REASONS=frozenset({'sl','tp','trail','vanish','manual','unknown'})


def _scratch_band(store=None) -> float:
    """Penny/scratch |£| band from self_params Store — not a frozen module constant."""
    from .self_params import DEFAULTS, load
    if store is not None:
        return float(load(store)['scratch_pnl_abs'])
    return float(DEFAULTS['scratch_pnl_abs'])


def _schedule(store=None) -> dict:
    """Bracket teacher thresholds/steps from self_params — seeds only until evolved."""
    from .self_params import DEFAULTS, load
    src=load(store) if store is not None else DEFAULTS
    keys=(
        'bracket_vs_vol_noise','bracket_vs_stop_noise','bracket_shape_wide',
        'bracket_batch_wide_cut','bracket_min_batch_samples',
        'bracket_win_kappa_step','bracket_win_sl_step','bracket_sl_noise_step',
        'bracket_loss_wide_kappa_step','bracket_trail_loss_kappa_step',
        'bracket_scratch_kappa_step','bracket_batch_raise_step',
        'bracket_batch_cut_step','scratch_pnl_abs',
    )
    return {k:float(src.get(k,DEFAULTS[k])) for k in keys}


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def _clip(v,lo,hi):
    return float(max(lo,min(hi,float(v))))


def normalize_exit_reason(reason) -> str:
    if reason is None:return 'unknown'
    r=str(reason).strip().lower()
    aliases={
        'stop':'sl','stop_loss':'sl','stop-loss':'sl','sl_hit':'sl',
        'take_profit':'tp','take-profit':'tp','limit':'tp','tp_hit':'tp',
        'trailing':'trail','trail_stop':'trail','trailing_stop':'trail',
        'position_disappeared':'vanish','disappeared':'vanish','gone':'vanish',
        'user':'manual','close':'manual','market_close':'manual',
    }
    r=aliases.get(r,r)
    return r if r in EXIT_REASONS else 'unknown'


def analyze_trade_path(*, direction=None, entry=None, exit_level=None,
                       stop_distance=None, adverse_path=None, volatility=None,
                       costs=None, pnl=None, store=None, scratch_cash_band=None) -> dict:
    """Separate path / vol / cost facts from the raw cash P&L.

    adverse_path: absolute price move against the position (or inferred from
    entry→exit when the move was adverse). Compared to volatility to decide
    whether an SL hit was noise (tight stop) vs a genuine wrong-direction path.
    """
    pnl_n=_finite(pnl)
    costs_n=abs(_finite(costs) or 0.0)
    vol=_finite(volatility)
    stop=_finite(stop_distance)
    adv=_finite(adverse_path)
    entry_n=_finite(entry);exit_n=_finite(exit_level)
    signed=None
    if direction in ('BUY','SELL') and entry_n is not None and exit_n is not None:
        sign=1.0 if direction=='BUY' else -1.0
        signed=sign*(exit_n-entry_n)
        if adv is None and signed<0:
            adv=abs(signed)
        elif adv is None:
            adv=0.0
    sched=_schedule(store)
    band=_finite(scratch_cash_band)
    if band is None:band=float(sched['scratch_pnl_abs'])
    scratch_cash=pnl_n is not None and abs(pnl_n)<=band
    cost_dominated=bool(
        costs_n>0 and pnl_n is not None and abs(pnl_n)<=max(band,1.5*costs_n))
    vs_vol=None
    vs_noise=float(sched['bracket_vs_vol_noise'])
    stop_noise=float(sched['bracket_vs_stop_noise'])
    if adv is not None and vol is not None and vol>1e-12:
        vs_vol=float(adv)/float(vol)
    noise_stop=bool(vs_vol is not None and vs_vol<=vs_noise)
    adverse_entry=bool(vs_vol is not None and vs_vol>vs_noise)
    if vs_vol is None and adv is not None and stop is not None and stop>1e-12:
        ratio=float(adv)/float(stop)
        noise_stop=ratio<=stop_noise
        adverse_entry=ratio>stop_noise and (pnl_n is not None and pnl_n<-band)
    return {
        'adverse_path':adv,'volatility':vol,'costs':costs_n,
        'signed_move':signed,'scratch_cash':scratch_cash,
        'cost_dominated':cost_dominated,'noise_stop':noise_stop,
        'adverse_entry':adverse_entry,'vs_vol':vs_vol,
        'scratch_pnl_abs':float(band),
        'vs_vol_noise':vs_noise,'vs_stop_noise':stop_noise,
    }


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


def record_outcome(store, *, kappa, sl_scale, pnl, currency=None, deal_id=None,
                   exit_reason=None, direction=None, entry=None, exit_level=None,
                   stop_distance=None, adverse_path=None, volatility=None,
                   costs=None):
    """Append cash outcome and learn raise/cut from analysed path — not P&L alone."""
    pnl_n=_finite(pnl)
    if pnl_n is None:return
    k=_finite(kappa);s=_finite(sl_scale)
    if k is None or s is None or k<=0 or s<1:return
    reason=normalize_exit_reason(exit_reason)
    path=analyze_trade_path(
        direction=direction,entry=entry,exit_level=exit_level,
        stop_distance=stop_distance,adverse_path=adverse_path,
        volatility=volatility,costs=costs,pnl=pnl_n,store=store)
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list):hist=[]
    hist.append({
        'kappa':float(k),'sl_scale':float(s),'pnl':float(pnl_n),
        'currency':currency,'deal_id':deal_id,'exit_reason':reason,
        'direction':direction,'adverse_path':path.get('adverse_path'),
        'volatility':path.get('volatility'),'costs':path.get('costs'),
    })
    store.set('bracket_outcome_history',hist[-200:])
    return adapt_after_outcome(
        store,kappa=float(k),sl_scale=float(s),pnl=float(pnl_n),
        exit_reason=reason,direction=direction,entry=entry,exit_level=exit_level,
        stop_distance=stop_distance,adverse_path=adverse_path,
        volatility=volatility,costs=costs)


def adapt_after_outcome(store, *, kappa: float, sl_scale: float, pnl: float,
                        exit_reason=None, direction=None, entry=None,
                        exit_level=None, stop_distance=None, adverse_path=None,
                        volatility=None, costs=None) -> dict:
    """Learn SL/TP multipliers from one closed trade — conditioned on context.

    Audit #19: do NOT auto-conclude that a loss means SL was too tight.
    Separate analysis of exit reason, path, volatility, and costs first.

      tp / profitable trail → reinforce shape that paid
      vanish / unknown without path → hold (no SL widen)
      scratch / cost-dominated → hold SL (was NOT "too tight" by default);
                                  optionally cut κ when shape was already wide
      sl + noise vs vol (tight shape) → raise SL room
      sl + adverse entry path → hold SL (entry direction problem, not distance)
      loss + already-wide shape → cut κ (TP fantasy)
      win → reinforce gently
    """
    mult=load_multipliers(store)
    k_m,s_m=mult['kappa'],mult['sl_scale']
    shape=float(kappa)*float(sl_scale)
    reason_x=normalize_exit_reason(exit_reason)
    sched=_schedule(store)
    band=float(sched['scratch_pnl_abs'])
    wide=float(sched['bracket_shape_wide'])
    path=analyze_trade_path(
        direction=direction,entry=entry,exit_level=exit_level,
        stop_distance=stop_distance,adverse_path=adverse_path,
        volatility=volatility,costs=costs,pnl=pnl,store=store,
        scratch_cash_band=band)
    reason='hold'
    if reason_x=='tp' or (pnl>band and reason_x in ('trail','unknown','manual')):
        k_m=min(MULT_KAPPA_MAX,k_m*float(sched['bracket_win_kappa_step']))
        s_m=min(MULT_SL_MAX,s_m*float(sched['bracket_win_sl_step']))
        reason='win_reinforce' if pnl>band else 'tp_reinforce'
    elif reason_x=='trail':
        # Trail already managed the exit — do not treat as "SL too tight".
        if pnl<-band and shape>=wide:
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_trail_loss_kappa_step']))
            reason='trail_loss_cut_tp'
        else:
            reason='trail_hold'
    elif reason_x=='vanish':
        reason='vanish_hold'
    elif path['scratch_cash'] or path['cost_dominated']:
        # Scratch ≠ SL too tight. Old heuristic (raise SL) removed (audit #19).
        if shape>=wide:
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_scratch_kappa_step']))
            reason='scratch_cut_tp'
        else:
            reason='scratch_hold_sl'
    elif reason_x=='sl' or (pnl<-band and reason_x in ('unknown','manual')):
        if path['noise_stop'] and shape<wide and not path['adverse_entry']:
            # Stopped inside the noise band with a tight shape → give SL room.
            s_m=min(MULT_SL_MAX,s_m*float(sched['bracket_sl_noise_step']))
            reason='sl_noise_raise_sl'
        elif shape>=wide:
            # Wide brackets already had room; TP was fantasy — cut κ, do not
            # widen SL further (even when the path was adverse).
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_loss_wide_kappa_step']))
            reason='loss_wide_cut_tp'
        elif path['adverse_entry']:
            # Wrong-direction path beyond vol — widening SL would enlarge losses.
            reason='loss_adverse_hold_sl'
        else:
            # Loss without vol/path evidence — refuse to assume SL was tight.
            reason='loss_insufficient_context_hold'
    elif pnl>band:
        k_m=min(MULT_KAPPA_MAX,k_m*float(sched['bracket_win_kappa_step']))
        s_m=min(MULT_SL_MAX,s_m*float(sched['bracket_win_sl_step']))
        reason='win_reinforce'
    save_multipliers(store,k_m,s_m,
                     reason=f'learned_{reason} exit={reason_x} pnl={pnl:.4f}')
    # Nudge the teacher schedule itself from the adapt reason (queued trial).
    if reason not in ('hold','trail_hold','vanish_hold','scratch_hold_sl',
                      'loss_insufficient_context_hold'):
        try:
            from .self_params import evolve_from_feedback
            evolve_from_feedback(store,bracket_reason=reason,commit=False)
        except Exception:
            pass
    return {
        'kappa_mult':k_m,'sl_mult':s_m,'reason':reason,'pnl':pnl,
        'exit_reason':reason_x,'path':path,
        'shape_wide':wide,'schedule':{k:sched[k] for k in (
            'bracket_vs_vol_noise','bracket_shape_wide','bracket_sl_noise_step')},
    }


def _bar_moves(candles, n: int|None=None):
    from .math_rules import adaptive_lookback
    rows=list(candles or [])
    if n is None:
        n=adaptive_lookback(rows) or len(rows)
    ups=[];downs=[];spans=[]
    for row in rows[-max(1,int(n)):]:
        try:
            o=float(row['open']);h=float(row['high']);l=float(row['low']);c=float(row['close'])
        except (KeyError,TypeError,ValueError):
            continue
        if min(o,h,l,c)<=0 or h<l:continue
        ups.append(max(0.,h-min(o,c)))
        downs.append(max(0.,max(o,c)-l))
        spans.append(h-l)
    return ups,downs,spans,int(n)


def kappa_from_ohlc(candles, n: int|None=None) -> float|None:
    ups,downs,_,_=_bar_moves(candles,n)
    if len(ups)<5 or len(downs)<5:return None
    du=median(downs)
    if du<=0:return None
    raw=median(ups)/du
    if not math.isfinite(raw) or raw<=0:return None
    return float(raw)


def recent_reachable_move(candles, n: int|None=None) -> float|None:
    _,_,spans,used=_bar_moves(candles,n)
    if len(spans)<5:return None
    spans=sorted(spans)
    robust=spans[max(0,int(len(spans)*0.9)-1)]
    return float(max(robust,spans[-1]*0.85))


def zone_at_open(candles, *, n: int|None=None, reach_n: int|None=None) -> dict:
    """SL/TP shape from recent OHLC at the open — lookback sized from the data."""
    ups,downs,spans,used=_bar_moves(candles,n)
    # Reach uses the same adaptive window (or a wider explicit one if given).
    reach=recent_reachable_move(candles,reach_n if reach_n is not None else used)
    if len(spans)<5:
        return {'kappa':DEFAULT_KAPPA,'range_distance':0.0,'max_tp':reach,
                'median_up':None,'median_down':None,'ready':False,'lookback':used}
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
        'ready':True,'lookback':used,
    }


def propose_for_open(store, *, explore_every: int=2, rng=None, ohlc_kappa=None,
                     base_sl: float|None=None, max_tp: float|None=None,
                     candles=None) -> dict:
    """Zone distances × learned raise/cut multipliers; TP still capped by reach."""
    zone=zone_at_open(candles or []) if candles is not None else {
        'kappa':float(ohlc_kappa) if ohlc_kappa and ohlc_kappa>0 else DEFAULT_KAPPA,
        'range_distance':float(base_sl or 0),'max_tp':max_tp,'ready':bool(ohlc_kappa),
        'lookback':None,
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
        'lookback':zone.get('lookback'),
        'nudge_samples':len(store.get('bracket_outcome_history',[]) or []),
    }


def evolve_from_history(store, *, min_samples: int|None=None) -> dict|None:
    """Batch confirm: if wide brackets earn more £ than tight, raise; else cut TP/SL."""
    sched=_schedule(store)
    need=int(min_samples if min_samples is not None else sched['bracket_min_batch_samples'])
    cut=float(sched['bracket_batch_wide_cut'])
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list) or len(hist)<need:
        return None
    wide=[];tight=[]
    for row in hist:
        pnl=_finite(row.get('pnl'));k=_finite(row.get('kappa'));s=_finite(row.get('sl_scale'))
        if pnl is None or k is None or s is None:continue
        (wide if k*s>=cut else tight).append(pnl)
    if len(wide)<2 or len(tight)<2:
        return None
    mw,mt=mean(wide),mean(tight)
    mult=load_multipliers(store)
    k_m,s_m=mult['kappa'],mult['sl_scale']
    if mw>mt+1e-9:
        raise_step=float(sched['bracket_batch_raise_step'])
        k_m=min(MULT_KAPPA_MAX,k_m*raise_step)
        s_m=min(MULT_SL_MAX,s_m*raise_step)
        reason=f'batch_raise wide_mean={mw:.4f} tight_mean={mt:.4f}'
    elif mt>mw+1e-9:
        k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_batch_cut_step']))
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
