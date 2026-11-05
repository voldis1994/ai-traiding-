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
# LIVE must not learn κ<1 — that made TP≪SL after loss streaks (−EV machine).
MULT_KAPPA_MIN,MULT_KAPPA_MAX=1.0,2.5
MULT_SL_MIN,MULT_SL_MAX=1.0,2.5
# Peak-to-trough structure may inform SL, but never dominate after a win streak
# blew the lookback window (live: 3 wins → fantasy SL/TP from full swing).
STRUCTURE_VS_MEDIAN_CAP=2.0
# OPEN refuses brackets where reachable TP cannot cover SL (κ<1 after crush).
MIN_LIVE_RR=1.0
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


def _recent_sl_losses_growing(store, pnl) -> bool:
    """True when the last SL losses show rising |£| — risk ratchet already hurt."""
    hist=store.get('bracket_outcome_history',[]) or []
    if not isinstance(hist,list):
        return False
    losses=[]
    for row in reversed(hist):
        if normalize_exit_reason(row.get('exit_reason'))!='sl':
            continue
        p=_finite(row.get('pnl'))
        if p is None or p>=0:
            continue
        losses.append(abs(float(p)))
        if len(losses)>=3:
            break
    cur=_finite(pnl)
    if cur is not None and cur<0:
        losses.insert(0,abs(float(cur)))
    if len(losses)<3:
        return False
    # Newest first: each prior loss was smaller → risk is climbing.
    return losses[0]+1e-9>=losses[1]>=losses[2]-1e-9


def adapt_after_outcome(store, *, kappa: float, sl_scale: float, pnl: float,
                        exit_reason=None, direction=None, entry=None,
                        exit_level=None, stop_distance=None, adverse_path=None,
                        volatility=None, costs=None) -> dict:
    """Learn SL/TP multipliers from one closed trade — conditioned on context.

    Audit #19: do NOT auto-conclude that a loss means SL was too tight.
    Cash risk must not ratchet up after every scratch/win — lot stays fixed,
    but wider SL was growing £ losses (£0.50→£1.34) while decisions stayed wrong.

      tp / win → NEVER widen SL or κ (OHLC decides width; win-ratchet was the
                 live bug after 3-win streaks). Decay legacy mults toward 1.
      scratch → never widen SL; cut κ if already wide
      growing SL-loss streak → cut sl_scale back toward 1
      sl + noise + tight shape + NOT growing losses → rare SL room
      sl + adverse / wide / thin evidence → hold or cut κ
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
    growing=_recent_sl_losses_growing(store,pnl)
    reason='hold'
    last_open=store.get('bracket_last_open_meta') if store is not None else None
    if not isinstance(last_open,dict):
        last_open={}
    at_ohlc_tp=bool(last_open.get('tp_at_ohlc_cap') or last_open.get('capped_to_ohlc'))

    def _decay_mults_toward_one():
        """Heal legacy win-ratchet inflation without inventing wider targets."""
        nonlocal k_m,s_m
        if k_m>1.0+1e-9:
            k_m=max(1.0,k_m*0.98)
        if s_m>1.0+1e-9:
            s_m=max(1.0,s_m*0.98)

    if reason_x=='tp' or (pnl>band and reason_x in ('trail','unknown','manual')):
        # Wins must not inflate next SL/TP. OHLC zone already sets width.
        _decay_mults_toward_one()
        reason=('tp_at_ohlc_cap_hold' if at_ohlc_tp else 'win_hold_ohlc')
    elif reason_x=='trail':
        if pnl<-band and shape>=wide:
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_trail_loss_kappa_step']))
            reason='trail_loss_cut_tp'
        else:
            reason='trail_hold'
    elif reason_x=='vanish':
        reason='vanish_hold'
    elif path['scratch_cash'] or path['cost_dominated']:
        # Scratch is chop / spread — widening SL only grew the next £ hole.
        if shape>=wide:
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_scratch_kappa_step']))
            reason='scratch_cut_tp'
        else:
            reason='scratch_hold_sl'
    elif reason_x=='sl' or (pnl<-band and reason_x in ('unknown','manual')):
        if growing and s_m>1.0+1e-9:
            # |£| SL losses climbing with fixed lot → cut risk back.
            s_m=max(MULT_SL_MIN,s_m*float(sched['bracket_batch_cut_step']))
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_loss_wide_kappa_step']))
            reason='sl_loss_streak_cut_risk'
        elif path['noise_stop'] and shape<wide and not path['adverse_entry']:
            s_m=min(MULT_SL_MAX,s_m*float(sched['bracket_sl_noise_step']))
            reason='sl_noise_raise_sl'
        elif shape>=wide:
            k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_loss_wide_kappa_step']))
            reason='loss_wide_cut_tp'
        elif path['adverse_entry']:
            reason='loss_adverse_hold_sl'
        else:
            reason='loss_insufficient_context_hold'
    elif pnl>band:
        _decay_mults_toward_one()
        reason=('tp_at_ohlc_cap_hold' if at_ohlc_tp else 'win_hold_ohlc')
    save_multipliers(store,k_m,s_m,
                     reason=f'learned_{reason} exit={reason_x} pnl={pnl:.4f}')
    if reason not in ('hold','trail_hold','vanish_hold','scratch_hold_sl',
                      'loss_insufficient_context_hold','loss_adverse_hold_sl',
                      'tp_at_ohlc_cap_hold','win_hold_ohlc'):
        try:
            from .self_params import evolve_from_feedback
            evolve_from_feedback(store,bracket_reason=reason,commit=False)
        except Exception:
            pass
    return {
        'kappa_mult':k_m,'sl_mult':s_m,'reason':reason,'pnl':pnl,
        'exit_reason':reason_x,'path':path,'growing_sl_losses':growing,
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
    """Robust reachable M1 span — median of the larger half by rank (no fixed %ile)."""
    _,_,spans,_used=_bar_moves(candles,n)
    if len(spans)<5:return None
    ordered=sorted(spans)
    upper=ordered[len(ordered)//2:]
    return float(median(upper)) if upper else float(median(ordered))


def _window_structure(candles, n: int|None=None) -> float|None:
    """Peak-to-trough over the lookback — the swing, not 1-bar noise."""
    rows=list(candles or [])
    if not rows:return None
    if n is None:
        from .math_rules import adaptive_lookback
        n=adaptive_lookback(rows) or len(rows)
    hi=lo=None
    for row in rows[-max(1,int(n)):]:
        try:
            h=float(row['high']);l=float(row['low'])
        except (KeyError,TypeError,ValueError):
            continue
        if h<=0 or l<=0 or h<l:continue
        hi=h if hi is None else max(hi,h)
        lo=l if lo is None else min(lo,l)
    if hi is None or lo is None or hi<=lo:return None
    span=float(hi-lo)
    return span if math.isfinite(span) and span>0 else None


# Same horizons the neural market context already reads (M1/M5/M15/H1).
BRACKET_TF_MINUTES = (1, 5, 15, 60)


def resample_m1_ohlc(candles, minutes: int) -> list:
    """Build higher-TF OHLC by packing sequential completed M1 bars.

    Uses elapsed-bar packs (5/15/60 M1 → M5/M15/H1), matching the neural
    multi-horizon tape — not a single minute window alone.
    """
    from .market_context import candle_time
    m = max(1, int(minutes))
    cleaned = []
    for raw in candles or []:
        dt = candle_time(raw.get('at'))
        if dt is None:
            continue
        try:
            o, h, l, c = (float(raw[k]) for k in ('open', 'high', 'low', 'close'))
        except (TypeError, ValueError, KeyError):
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)) or min(o, h, l, c) <= 0 or h < l:
            continue
        cleaned.append({
            'at': dt.isoformat(), 'open': o, 'high': h, 'low': l, 'close': c,
            'spread': _finite(raw.get('spread')), '_dt': dt,
        })
    cleaned.sort(key=lambda r: r['_dt'])
    if m <= 1:
        out1 = []
        for r in cleaned:
            row = {k: r[k] for k in ('at', 'open', 'high', 'low', 'close')}
            if r.get('spread') is not None:
                row['spread'] = r['spread']
            out1.append(row)
        return out1
    out = []
    for i in range(0, len(cleaned) - m + 1, m):
        chunk = cleaned[i:i + m]
        if len(chunk) < m:
            break
        spreads = [c['spread'] for c in chunk if c.get('spread') is not None]
        row = {
            'at': chunk[-1]['at'],
            'open': chunk[0]['open'],
            'high': max(c['high'] for c in chunk),
            'low': min(c['low'] for c in chunk),
            'close': chunk[-1]['close'],
        }
        if spreads:
            row['spread'] = float(sum(spreads) / len(spreads))
        out.append(row)
    return out


def _zone_single_tf(candles, *, n: int|None=None, reach_n: int|None=None) -> dict:
    """SL/TP geometry from one OHLC series (one timeframe)."""
    ups,downs,spans,used=_bar_moves(candles,n)
    reach=recent_reachable_move(candles,reach_n if reach_n is not None else used)
    structure=_window_structure(candles,used)
    if len(spans)<5:
        return {'kappa':DEFAULT_KAPPA,'range_distance':0.0,'max_tp':reach,
                'noise_distance':0.0,'structure':structure,
                'pull_share':None,'favor_share':None,
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
    if med_span>1e-12:
        pull_share=float(med_down/med_span)
        favor_share=float(med_up/med_span)
    else:
        pull_share=1.0
        favor_share=1.0
    if not math.isfinite(pull_share) or pull_share<0:pull_share=1.0
    if not math.isfinite(favor_share) or favor_share<0:favor_share=1.0
    # Cap structure vs typical bar — full lookback peak-trough after a win
    # streak was inventing day-range SL/TP (live Gold after 3 wins).
    struct_cap=float(med_span)*STRUCTURE_VS_MEDIAN_CAP if med_span>1e-12 else 0.0
    struct_sl=(float(structure)*pull_share) if structure and structure>0 else 0.0
    struct_tp=(float(structure)*favor_share) if structure and structure>0 else 0.0
    if struct_cap>0:
        struct_sl=min(struct_sl,struct_cap)
        struct_tp=min(struct_tp,struct_cap)
    sl_distance=max(med_span,med_down,struct_sl)
    # struct_tp already capped — cannot invent day-range TP alone.
    target_bits=[x for x in (reach,med_up,struct_tp) if x is not None and float(x)>0]
    max_tp=float(max(target_bits)) if target_bits else (float(reach) if reach else None)
    return {
        'kappa':float(kappa),
        'range_distance':float(sl_distance),
        'noise_distance':float(med_span),
        'structure':float(structure) if structure is not None else None,
        'pull_share':float(pull_share),'favor_share':float(favor_share),
        'max_tp':float(max_tp) if max_tp is not None else None,
        'median_up':med_up,'median_down':med_down,
        'ready':True,'lookback':used,
    }


def zone_at_open(candles, *, n: int|None=None, reach_n: int|None=None,
                 multi_tf: bool=True) -> dict:
    """SL/TP from the whole tape — M1+M5+M15+H1 consensus, not one timeframe alone.

    Each TF builds its own OHLC geometry (adverse/favor/reach). The OPEN decision
    is the median across ready timeframes so micro M1 noise cannot dominate and
    a single slow bar cannot invent an unreachable target by itself.
    """
    rows = list(candles or [])
    if not multi_tf or len(rows) < 20:
        out = _zone_single_tf(rows, n=n, reach_n=reach_n)
        out['horizons_minutes'] = [1]
        out['timeframes'] = {'1': {k: out.get(k) for k in (
            'kappa', 'range_distance', 'max_tp', 'ready', 'lookback')}}
        out['multi_tf'] = False
        return out

    per = {}
    for minutes in BRACKET_TF_MINUTES:
        tf_rows = rows if minutes == 1 else resample_m1_ohlc(rows, minutes)
        # Need enough TF bars for a meaningful median geometry.
        need = 5 if minutes == 1 else 5
        if len(tf_rows) < need:
            continue
        z = _zone_single_tf(tf_rows, n=(n if minutes == 1 else None),
                            reach_n=(reach_n if minutes == 1 else None))
        z['tf_minutes'] = int(minutes)
        z['tf_bars'] = len(tf_rows)
        per[int(minutes)] = z

    ready = [z for z in per.values()
             if z.get('ready') and _finite(z.get('range_distance'))
             and float(z['range_distance']) > 0]
    if not ready:
        out = _zone_single_tf(rows, n=n, reach_n=reach_n)
        out['horizons_minutes'] = [1]
        out['timeframes'] = {'1': {k: out.get(k) for k in (
            'kappa', 'range_distance', 'max_tp', 'ready', 'lookback')}}
        out['multi_tf'] = False
        return out
    if len(ready) == 1:
        # One TF only — keep its native κ/SL/TP (no self-cap via pair median).
        only = dict(ready[0])
        only['multi_tf'] = False
        only['horizons_minutes'] = [int(only.get('tf_minutes') or 1)]
        only['timeframes'] = {
            str(m): {
                'kappa': z.get('kappa'),
                'range_distance': z.get('range_distance'),
                'max_tp': z.get('max_tp'),
                'ready': z.get('ready'),
                'lookback': z.get('lookback'),
                'tf_bars': z.get('tf_bars'),
            } for m, z in per.items()
        }
        return only

    noises = [float(z['noise_distance']) for z in ready
              if _finite(z.get('noise_distance')) and float(z['noise_distance']) > 0]
    pulls = [float(z['pull_share']) for z in ready if _finite(z.get('pull_share'))]
    favors = [float(z['favor_share']) for z in ready if _finite(z.get('favor_share'))]
    ups = [float(z['median_up']) for z in ready if _finite(z.get('median_up'))]
    downs = [float(z['median_down']) for z in ready if _finite(z.get('median_down'))]
    # Longer TF structure when available — whole-swing context.
    structure = None
    for minutes in (60, 15, 5, 1):
        z = per.get(minutes) or {}
        if z.get('ready') and _finite(z.get('structure')) and float(z['structure']) > 0:
            structure = float(z['structure'])
            break

    # Each TF proposes a *coherent* (SL, TP) pair from its own OHLC. Consensus
    # is the median of those pairs — never mix H1-wide SL with M1-tiny TP
    # (that looked like "not using OHLC": SL 25 / TP 12.5 with κ≈0.5).
    pair_sl = []
    pair_tp = []
    pair_kappa = []
    for z in ready:
        sl_i = float(z['range_distance'])
        if sl_i <= 0:
            continue
        k_i = float(z.get('kappa') or DEFAULT_KAPPA)
        if k_i <= 0 or not math.isfinite(k_i):
            k_i = DEFAULT_KAPPA
        raw_tp = k_i * sl_i
        tp_cap = _finite(z.get('max_tp'))
        tp_i = min(raw_tp, float(tp_cap)) if tp_cap is not None and float(tp_cap) > 0 else raw_tp
        if tp_i <= 0:
            continue
        pair_sl.append(sl_i)
        pair_tp.append(float(tp_i))
        pair_kappa.append(float(tp_i) / sl_i)
        z['proposed_sl'] = sl_i
        z['proposed_tp'] = float(tp_i)

    if not pair_sl:
        out = _zone_single_tf(rows, n=n, reach_n=reach_n)
        out['horizons_minutes'] = [1]
        out['multi_tf'] = False
        return out

    sl = float(median(pair_sl))
    max_tp = float(median(pair_tp))
    kappa = float(median(pair_kappa))
    if kappa <= 0 or not math.isfinite(kappa):
        kappa = DEFAULT_KAPPA
    # Keep reachable bound consistent with the consensus TP plan.
    if sl > 0 and max_tp > 0 and kappa * sl > max_tp * 1.0001:
        kappa = max(0.5, max_tp / sl)
    noise = float(median(noises)) if noises else sl
    pull = float(median(pulls)) if pulls else None
    favor = float(median(favors)) if favors else None
    med_up = float(median(ups)) if ups else None
    med_down = float(median(downs)) if downs else None

    return {
        'kappa': float(kappa),
        'range_distance': float(sl),
        'noise_distance': float(noise),
        'structure': float(structure) if structure is not None else None,
        'pull_share': float(pull) if pull is not None else None,
        'favor_share': float(favor) if favor is not None else None,
        'max_tp': float(max_tp) if max_tp is not None else None,
        'median_up': med_up,
        'median_down': med_down,
        'ready': True,
        'lookback': (per.get(1) or {}).get('lookback'),
        'multi_tf': True,
        'horizons_minutes': sorted(per.keys()),
        'timeframes': {
            str(m): {
                'kappa': z.get('kappa'),
                'range_distance': z.get('range_distance'),
                'max_tp': z.get('max_tp'),
                'proposed_sl': z.get('proposed_sl'),
                'proposed_tp': z.get('proposed_tp'),
                'ready': z.get('ready'),
                'lookback': z.get('lookback'),
                'tf_bars': z.get('tf_bars'),
            } for m, z in per.items()
        },
    }


def decide_brackets_from_ohlc(zone, *, tick, spread=0., min_distance=0.,
                              tighten=1.0) -> dict:
    """System decides SL/TP from this OHLC window — not multipliers, not hard fantasy.

    Decision from candles:
      SL = observed adverse range (``range_distance``), floored by broker min
      κ  = observed favor/adverse ratio (``zone.kappa``)
      TP = κ×SL, but not beyond reachable favor span (``max_tp``)

    ``tighten`` ∈ (0,1] only after loss cool-down — still a fraction of the
    *same* OHLC decision, never an invented wider target.
    """
    from .math_rules import stop_take

    z = zone if isinstance(zone, dict) else {}
    try:
        tick_f = float(tick)
    except (TypeError, ValueError):
        tick_f = 0.01
    if not math.isfinite(tick_f) or tick_f <= 0:
        tick_f = 0.01
    try:
        sp = float(spread or 0.)
    except (TypeError, ValueError):
        sp = 0.
    if not math.isfinite(sp) or sp < 0:
        sp = 0.
    try:
        mn = float(min_distance or 0.)
    except (TypeError, ValueError):
        mn = 0.
    if not math.isfinite(mn) or mn < 0:
        mn = 0.
    t = _finite(tighten)
    if t is None or t <= 0:
        t = 1.0
    t = max(0.7, min(1.0, float(t)))
    ohlc_sl = _finite(z.get('range_distance'))
    if ohlc_sl is None or ohlc_sl <= 0:
        ohlc_sl = _finite(z.get('noise_distance'))
    ohlc_kappa = _finite(z.get('kappa')) or DEFAULT_KAPPA
    if ohlc_kappa <= 0 or not math.isfinite(ohlc_kappa):
        ohlc_kappa = DEFAULT_KAPPA
    ohlc_tp = _finite(z.get('max_tp'))
    buffer = max(2.0 * tick_f, 2.0 * sp)
    floor = mn + buffer if (mn + buffer) > 0 else tick_f
    # Core decision: SL from OHLC adverse move (or broker floor if quieter).
    raw_sl = float(ohlc_sl) if ohlc_sl is not None and ohlc_sl > 0 else floor
    stop_d = max(floor, raw_sl) * t
    # TP from OHLC κ, limited by what this window actually reaches.
    raw_tp = float(ohlc_kappa) * stop_d
    if ohlc_tp is not None and ohlc_tp > 0:
        profit_d = min(raw_tp, float(ohlc_tp) * t)
    else:
        profit_d = raw_tp
    profit_d = max(floor, profit_d)
    # Different approach than "HOLD / skip OPEN": keep trading like early days,
    # but *reshape* to ≥1:1 so TP≪SL crush cannot invent −EV. Shrink SL to the
    # reachable TP (still ≥ broker floor); never refuse the whole day.
    reshaped = False
    reshape_reason = None
    if stop_d > 1e-12 and profit_d + 1e-12 < stop_d * MIN_LIVE_RR:
        reach_cap = float(ohlc_tp) * t if ohlc_tp is not None and ohlc_tp > 0 else None
        if reach_cap is None or reach_cap + 1e-12 >= stop_d * MIN_LIVE_RR:
            profit_d = max(floor, stop_d * MIN_LIVE_RR)
            reshape_reason = 'raised_tp_to_1R'
        else:
            # Reachable move < wide SL → cut risk to what OHLC can actually pay.
            stop_d = max(floor, min(stop_d, max(reach_cap, floor)))
            profit_d = max(floor, stop_d * MIN_LIVE_RR)
            # If reach still below floor, both sit on broker floor at 1:1.
            if profit_d + 1e-12 < stop_d:
                stop_d = max(floor, profit_d)
                profit_d = stop_d
            reshape_reason = 'shrunk_sl_to_reachable_1R'
        reshaped = True
    # Effective κ after reshape (for tagging / learning).
    eff_kappa = (profit_d / stop_d) if stop_d > 1e-12 else float(ohlc_kappa)
    # LIVE floor κ≥1 — never send TP≪SL via stop_take(kappa=0.5).
    live_kappa = max(MIN_LIVE_RR, float(eff_kappa))
    # Snap onto broker tick grid via stop_take (sl_scale=1 — no invent layer).
    entry = 1000.0  # levels unused by callers that only need distances
    snapped = stop_take(
        entry, 'BUY', tick_f, sp, mn,
        kappa=live_kappa, sl_scale=1.0,
        range_distance=stop_d)
    sd = float(snapped['stop_distance'])
    pd = float(snapped['profit_distance'])
    # After tick snap, nudge TP up to SL if ceil left RR slightly short.
    if sd > 1e-12 and pd + 1e-12 < sd * MIN_LIVE_RR:
        pd = sd
        reshaped = True
        reshape_reason = (reshape_reason or 'snap') + '+ceil_1R'
    return {
        'stop_distance': sd,
        'profit_distance': pd,
        'kappa': max(MIN_LIVE_RR, (pd / sd) if sd > 1e-12 else live_kappa),
        'sl_scale': 1.0,
        'ohlc_kappa': float(ohlc_kappa),
        'ohlc_sl': float(raw_sl),
        'ohlc_tp': float(ohlc_tp) if ohlc_tp is not None else None,
        'tighten': float(t),
        'blocked': False,
        'block_reason': None,
        'reshaped': reshaped,
        'reshape_reason': reshape_reason,
        'source': 'ohlc_zone',
        'ready': bool(z.get('ready')),
        'lookback': z.get('lookback'),
        'median_up': z.get('median_up'),
        'median_down': z.get('median_down'),
        'pull_share': z.get('pull_share'),
        'favor_share': z.get('favor_share'),
        'stop_ticks': snapped.get('stop_ticks'),
        'profit_ticks': snapped.get('profit_ticks'),
        'raised_to_broker_min': bool(snapped.get('raised_to_broker_min')),
    }


def note_open_bracket_meta(store, meta: dict) -> dict:
    """Remember the OHLC decision used on the last OPEN (for learn feedback)."""
    clean = {
        'source': str((meta or {}).get('source') or 'ohlc_zone'),
        'ohlc_kappa': _finite((meta or {}).get('ohlc_kappa')),
        'ohlc_sl': _finite((meta or {}).get('ohlc_sl')),
        'ohlc_tp': _finite((meta or {}).get('ohlc_tp')),
        'stop_distance': _finite((meta or {}).get('stop_distance')),
        'profit_distance': _finite((meta or {}).get('profit_distance')),
        'tighten': _finite((meta or {}).get('tighten')),
        # Kept for older learn path: TP already used full reachable span.
        'tp_at_ohlc_cap': bool(
            (meta or {}).get('tp_at_ohlc_cap')
            or (
                _finite((meta or {}).get('ohlc_tp')) is not None
                and _finite((meta or {}).get('profit_distance')) is not None
                and abs(float(meta['profit_distance']) - float(meta['ohlc_tp']))
                <= max(1e-9, 0.02 * float(meta['ohlc_tp']))
            )),
        'capped_to_ohlc': False,
    }
    if store is not None:
        store.set('bracket_last_open_meta', clean)
    return clean


def propose_for_open(store, *, explore_every: int=2, rng=None, ohlc_kappa=None,
                     base_sl: float|None=None, max_tp: float|None=None,
                     candles=None, tick: float=0.01, spread: float=0.,
                     min_distance: float=0., tighten: float=1.0) -> dict:
    """OHLC zone decides κ / SL / TP — learned multipliers do not set width."""
    zone=zone_at_open(candles or []) if candles is not None else {
        'kappa':float(ohlc_kappa) if ohlc_kappa and ohlc_kappa>0 else DEFAULT_KAPPA,
        'range_distance':float(base_sl or 0),'max_tp':max_tp,'ready':bool(ohlc_kappa),
        'lookback':None,
    }
    if ohlc_kappa and ohlc_kappa>0:
        # Blend two OHLC κ readings (caller hint + zone) — still candle-driven.
        zk=_finite(zone.get('kappa')) or DEFAULT_KAPPA
        zone=dict(zone)
        zone['kappa']=0.5*float(zk)+0.5*float(ohlc_kappa)
    if base_sl is not None and _finite(base_sl) and float(base_sl)>0:
        zone=dict(zone)
        # Prefer explicit OHLC base when caller already measured range.
        zone['range_distance']=float(base_sl)
    if max_tp is not None:
        zone=dict(zone);zone['max_tp']=max_tp
    decided=decide_brackets_from_ohlc(
        zone,tick=tick,spread=spread,min_distance=min_distance,tighten=tighten)
    save_params(store,decided['kappa'],1.0,
                reason=(f"ohlc_zone κ={decided.get('ohlc_kappa')} "
                        f"sl={decided.get('ohlc_sl')} tp={decided.get('ohlc_tp')}"))
    learned=load_multipliers(store)
    return {
        'kappa':float(decided['kappa']),'sl_scale':1.0,'explored':False,
        'max_tp':decided.get('ohlc_tp'),'base_sl':decided.get('ohlc_sl'),
        'ohlc_kappa':decided.get('ohlc_kappa'),
        'zone_ready':bool(zone.get('ready')),
        'median_up':zone.get('median_up'),'median_down':zone.get('median_down'),
        'kappa_mult':learned['kappa'],'sl_mult':learned['sl_scale'],
        'lookback':zone.get('lookback'),
        'stop_distance':decided['stop_distance'],
        'profit_distance':decided['profit_distance'],
        'blocked':False,
        'reshaped':bool(decided.get('reshaped')),
        'reshape_reason':decided.get('reshape_reason'),
        'bracket_source':'ohlc_zone',
        'nudge_samples':len(store.get('bracket_outcome_history',[]) or []),
    }


def evolve_from_history(store, *, min_samples: int|None=None) -> dict|None:
    """Batch confirm: only CUT when tight beats wide — never raise SL/κ after wins.

    Width is an OHLC decision. Old batch_raise after winning wide trades was the
    3-win → fantasy SL/TP ratchet (raised both κ and sl_scale, then saved them
    into bracket_kappa/sl_scale).
    """
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
    if mt>mw+1e-9:
        k_m=max(MULT_KAPPA_MIN,k_m*float(sched['bracket_batch_cut_step']))
        if s_m>1.0+1e-9:
            s_m=max(MULT_SL_MIN,s_m*float(sched['bracket_batch_cut_step']))
        reason=f'batch_cut_tp wide_mean={mw:.4f} tight_mean={mt:.4f}'
    else:
        # Wide winning or tied → do NOT raise. Heal legacy inflate toward 1.
        changed=False
        if k_m>1.0+1e-9:
            k_m=max(1.0,k_m*0.99);changed=True
        if s_m>1.0+1e-9:
            s_m=max(1.0,s_m*0.99);changed=True
        if not changed:
            return None
        reason=f'batch_hold_no_raise wide_mean={mw:.4f} tight_mean={mt:.4f}'
    before=dict(mult)
    save_multipliers(store,k_m,s_m,reason=reason)
    if abs(k_m-before['kappa'])<1e-6 and abs(s_m-before['sl_scale'])<1e-6:
        return None
    # Do NOT write multipliers into bracket_kappa/sl_scale — those tag OHLC decisions.
    return {'kappa':k_m,'sl_scale':s_m,'mean_pnl':mw,'samples':len(wide)+len(tight),
            'reason':reason}
