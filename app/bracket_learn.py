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
# LIVE OPEN width is OHLC-decided (not these rails).
MULT_KAPPA_MIN,MULT_KAPPA_MAX=0.5,2.5
MULT_SL_MIN,MULT_SL_MAX=1.0,2.5
# Peak-to-trough structure may inform SL, but never dominate after a win streak
# blew the lookback window (live: 3 wins → fantasy SL/TP from full swing).
STRUCTURE_VS_MEDIAN_CAP=2.0
# Live Gold bug: SL 2pts while ATR≈4.65 — floor stop to a real volatility width.
ATR_STOP_FLOOR_FRAC=0.50
# Default min reward:risk when Store has no bracket_min_rr yet (>1 → not equal SL/TP).
DEFAULT_MIN_RR=1.5
# Swing SL beyond this × ATR is unrealistically far from entry (Gold ~26pt vs ATR~4).
DEFAULT_SL_MAX_ATR=2.0
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


def infer_exit_reason_from_path(*, direction=None, entry=None, exit_level=None,
                                bars=None, stop_distance=None,
                                profit_distance=None, tick=None,
                                trail_active=False) -> str:
    """Classify sl/tp/trail from the OHLC path when the broker omits a reason.

    Walks entry→exit candles; whichever of stop/profit was tagged first wins.
    No RR hardcode — uses the deal's own stop/profit distances.
    """
    entry_n=_finite(entry);exit_n=_finite(exit_level)
    stop=_finite(stop_distance);profit=_finite(profit_distance)
    tick_n=_finite(tick) or 0.
    if direction not in ('BUY','SELL') or entry_n is None:
        return 'unknown'
    tol=max(float(tick_n)*1.5,1e-9)
    stop_lvl=profit_lvl=None
    if stop is not None and stop>0:
        stop_lvl=(entry_n-stop) if direction=='BUY' else (entry_n+stop)
    if profit is not None and profit>0:
        profit_lvl=(entry_n+profit) if direction=='BUY' else (entry_n-profit)
    first=None
    for raw in bars or []:
        if not isinstance(raw,dict):
            continue
        hi=_finite(raw.get('high'));lo=_finite(raw.get('low'))
        mid=_finite(raw.get('mid')) or _finite(raw.get('close'))
        if hi is None:hi=mid
        if lo is None:lo=mid
        if hi is None or lo is None:
            continue
        hit_sl=hit_tp=False
        if stop_lvl is not None:
            if direction=='BUY' and lo<=stop_lvl+tol:hit_sl=True
            if direction=='SELL' and hi>=stop_lvl-tol:hit_sl=True
        if profit_lvl is not None:
            if direction=='BUY' and hi>=profit_lvl-tol:hit_tp=True
            if direction=='SELL' and lo<=profit_lvl+tol:hit_tp=True
        if hit_sl and hit_tp:
            # Same bar: adverse first for classification conservatism.
            first='sl'
            break
        if hit_sl:
            first='sl'
            break
        if hit_tp:
            first='tp'
            break
    if first=='sl':
        return 'trail' if trail_active else 'sl'
    if first=='tp':
        return 'tp'
    # Fallback: exit proximity to planned levels.
    if entry_n is not None and exit_n is not None:
        move=(exit_n-entry_n) if direction=='BUY' else (entry_n-exit_n)
        if stop is not None and move<=-(float(stop)-tol):
            return 'trail' if trail_active else 'sl'
        if profit is not None and move>=(float(profit)-tol):
            return 'tp'
        if trail_active and move<0:
            return 'trail'
    return 'unknown'


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
    """Append cash outcome and learn raise/cut from analysed path — not P&L alone.

    Same ``deal_id`` replaces the prior row (UPL→broker refine) instead of
    double-appending teacher history.
    """
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
    deal=str(deal_id) if deal_id is not None else ''
    if deal:
        hist=[r for r in hist
              if not (isinstance(r,dict) and str(r.get('deal_id') or '')==deal)]
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
    """Build higher-TF OHLC from calendar-aligned completed M1 buckets.

    M5/M15/M30/M60 bars start on wall-clock UTC boundaries (…:00/:05/:15/:30).
    Incomplete buckets (gaps / missing minutes) are skipped so a hole does not
    glue unrelated M1 rows into a fake higher-TF candle.
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
    # Group by UTC epoch-minute floor to calendar period.
    buckets = {}
    order = []
    for r in cleaned:
        epoch_min = int(r['_dt'].timestamp()) // 60
        key = (epoch_min // m) * m
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)
    out = []
    for key in order:
        chunk = buckets[key]
        # Require a full period of distinct minutes — drop gapped buckets.
        minutes_seen = {int(c['_dt'].timestamp()) // 60 for c in chunk}
        if len(minutes_seen) < m:
            continue
        chunk = sorted(chunk, key=lambda c: c['_dt'])
        spreads = [c['spread'] for c in chunk if c.get('spread') is not None]
        row = {
            'at': chunk[-1]['at'],
            'open': chunk[0]['open'],
            'high': max(c['high'] for c in chunk),
            'low': min(c['low'] for c in chunk),
            'close': chunk[-1]['close'],
            'bucket_start': key,
            'm1_count': len(minutes_seen),
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


def _pick_structural_levels(*, action, entry, highs, lows, floor, buffer):
    """Pick SL/TP from the full swing-pivot lists (not only last SH/SL).

    After BOS continuation the last swing high/low is often already broken —
    that used to fail with ``entry_above_swing_high`` and fall back to invented
    median-zone ±distance. Instead:
      BUY  SL = nearest swing low still below entry
           TP = nearest swing high still above entry, else measured move
               from the broken swing range (last_SH + span)
      SELL symmetric.
    """
    highs = [h for h in (highs or []) if _finite((h or {}).get('price')) is not None]
    lows = [lo for lo in (lows or []) if _finite((lo or {}).get('price')) is not None]
    if not highs or not lows:
        return None
    entry_f = float(entry)
    floor = float(floor)
    buffer = float(buffer)

    if action == 'BUY':
        sl_cands = [s for s in lows if float(s['price']) <= entry_f - floor]
        if not sl_cands:
            return None
        # Nearest protective low (highest still below entry).
        sl_pivot = max(sl_cands, key=lambda s: float(s['price']))
        stop_level = float(sl_pivot['price']) - buffer
        tp_cands = [s for s in highs if float(s['price']) >= entry_f + floor]
        if tp_cands:
            # Nearest reachable high above entry (not a far day extreme).
            tp_pivot = min(tp_cands, key=lambda s: float(s['price']))
            profit_level = float(tp_pivot['price'])
            swing_hi = float(tp_pivot['price'])
            reason = 'swing_levels'
        else:
            # Continuation past last SH — measured move from last broken swing.
            last_sh = highs[-1]
            prior_sl = None
            for s in reversed(lows):
                if int(s.get('i') or -1) < int(last_sh.get('i') or -1):
                    prior_sl = s
                    break
            if prior_sl is None:
                prior_sl = sl_pivot
            span = float(last_sh['price']) - float(prior_sl['price'])
            if span < floor:
                return None
            profit_level = float(last_sh['price']) + span
            if profit_level < entry_f + floor:
                profit_level = entry_f + span
            swing_hi = float(last_sh['price'])
            reason = 'swing_measured_move'
        return {
            'stop_level': float(stop_level),
            'profit_level': float(profit_level),
            'swing_lo': float(sl_pivot['price']),
            'swing_hi': float(swing_hi),
            'reason': reason,
        }

    # SELL
    sl_cands = [s for s in highs if float(s['price']) >= entry_f + floor]
    if not sl_cands:
        return None
    sl_pivot = min(sl_cands, key=lambda s: float(s['price']))
    stop_level = float(sl_pivot['price']) + buffer
    tp_cands = [s for s in lows if float(s['price']) <= entry_f - floor]
    if tp_cands:
        tp_pivot = max(tp_cands, key=lambda s: float(s['price']))
        profit_level = float(tp_pivot['price'])
        swing_lo = float(tp_pivot['price'])
        reason = 'swing_levels'
    else:
        last_sl = lows[-1]
        prior_sh = None
        for s in reversed(highs):
            if int(s.get('i') or -1) < int(last_sl.get('i') or -1):
                prior_sh = s
                break
        if prior_sh is None:
            prior_sh = sl_pivot
        span = float(prior_sh['price']) - float(last_sl['price'])
        if span < floor:
            return None
        profit_level = float(last_sl['price']) - span
        if profit_level > entry_f - floor:
            profit_level = entry_f - span
        swing_lo = float(last_sl['price'])
        reason = 'swing_measured_move'
    return {
        'stop_level': float(stop_level),
        'profit_level': float(profit_level),
        'swing_hi': float(sl_pivot['price']),
        'swing_lo': float(swing_lo),
        'reason': reason,
    }


def _min_rr(store=None) -> float:
    """Minimum TP/SL distance ratio for LIVE brackets (seed 1.5 — not 1:1)."""
    from .self_params import DEFAULTS, load
    try:
        if store is not None:
            return max(1.0, min(3.0, float(load(store)['bracket_min_rr'])))
        return float(DEFAULTS.get('bracket_min_rr', DEFAULT_MIN_RR))
    except Exception:
        return float(DEFAULT_MIN_RR)


def _sl_max_atr_mult(store=None) -> float:
    from .self_params import DEFAULTS, load
    try:
        if store is not None:
            return max(1.0, min(4.0, float(load(store)['bracket_sl_max_atr'])))
        return float(DEFAULTS.get('bracket_sl_max_atr', DEFAULT_SL_MAX_ATR))
    except Exception:
        return float(DEFAULT_SL_MAX_ATR)


def _set_stop_from_distance(*, action, entry, stop_d):
    entry_f = float(entry)
    stop_d = float(stop_d)
    if action == 'BUY':
        return entry_f - stop_d
    return entry_f + stop_d


def levels_correct_side(*, action, entry, stop_level, profit_level) -> bool:
    """BUY: SL below entry below TP. SELL: TP below entry below SL. Never swapped."""
    try:
        e = float(entry)
        sl = float(stop_level)
        tp = float(profit_level)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(e) and math.isfinite(sl) and math.isfinite(tp)):
        return False
    if action == 'BUY':
        return sl < e < tp
    if action == 'SELL':
        return tp < e < sl
    return False


def _extend_structural_tp(*, action, entry, need_d, highs=None, lows=None):
    """Nearest swing TP that still clears ``need_d`` (correct side only)."""
    entry_f = float(entry)
    need = float(need_d)
    if need <= 0:
        return None
    if action == 'BUY':
        cands = [s for s in (highs or [])
                 if _finite((s or {}).get('price')) is not None
                 and float(s['price']) >= entry_f + need]
        if not cands:
            return None
        tp = min(cands, key=lambda s: float(s['price']))
        lvl = float(tp['price'])
        return {'profit_level': lvl, 'profit_distance': lvl - entry_f,
                'swing_hi': lvl}
    if action == 'SELL':
        cands = [s for s in (lows or [])
                 if _finite((s or {}).get('price')) is not None
                 and float(s['price']) <= entry_f - need]
        if not cands:
            return None
        tp = max(cands, key=lambda s: float(s['price']))
        lvl = float(tp['price'])
        return {'profit_level': lvl, 'profit_distance': entry_f - lvl,
                'swing_lo': lvl}
    return None


def cap_far_swing_sl(*, action, entry, stop_level, stop_d, floor, atr=None,
                     highs=None, lows=None, buffer=0., store=None,
                     max_atr_mult=None) -> dict:
    """When OHLC swing SL is unrealistically far from entry, pull it in.

    Primary rule (Gold SELL screenshot: SL ~26pts while ATR ~ few pts):
      1. Prefer a closer protective pivot still inside ``max_atr × ATR``.
      2. Else hard-cap SL distance to ``max_atr × ATR`` (keep structural TP).
    Never widens TP. Never invents a mirrored TP=SL.
    """
    entry_f = float(entry)
    stop_d = float(stop_d)
    stop_level = float(stop_level)
    floor_f = float(floor) if floor and floor > 0 else 0.0
    atr_v = _finite(atr)
    mult = float(max_atr_mult) if max_atr_mult is not None else _sl_max_atr_mult(store)
    mult = max(1.0, min(4.0, mult))
    out = {
        'capped': False, 'closer_pivot': False, 'max_atr_mult': mult,
        'stop_distance': stop_d, 'stop_level': stop_level,
        'sl_max': None, 'reason': 'sl_ok',
    }
    if atr_v is None or atr_v <= 0:
        return out
    sl_max = max(floor_f, mult * float(atr_v))
    out['sl_max'] = float(sl_max)
    if stop_d <= sl_max + 1e-9:
        return out
    # Prefer a real closer pivot inside the ATR cap before inventing a soft level.
    buf = float(buffer or 0.)
    if action == 'BUY' and lows:
        cands = [s for s in lows
                 if float(s['price']) <= entry_f - floor_f
                 and (entry_f - (float(s['price']) - buf)) <= sl_max + 1e-9
                 and (entry_f - (float(s['price']) - buf)) + 1e-9 >= floor_f]
        if cands:
            closer = max(cands, key=lambda s: float(s['price']))
            stop_level = float(closer['price']) - buf
            stop_d = entry_f - stop_level
            return {
                'capped': True, 'closer_pivot': True, 'max_atr_mult': mult,
                'stop_distance': float(stop_d), 'stop_level': float(stop_level),
                'sl_max': float(sl_max), 'swing_lo': float(closer['price']),
                'reason': 'atr_closer_sl',
            }
    elif action == 'SELL' and highs:
        cands = [s for s in highs
                 if float(s['price']) >= entry_f + floor_f
                 and ((float(s['price']) + buf) - entry_f) <= sl_max + 1e-9
                 and ((float(s['price']) + buf) - entry_f) + 1e-9 >= floor_f]
        if cands:
            closer = min(cands, key=lambda s: float(s['price']))
            stop_level = float(closer['price']) + buf
            stop_d = stop_level - entry_f
            return {
                'capped': True, 'closer_pivot': True, 'max_atr_mult': mult,
                'stop_distance': float(stop_d), 'stop_level': float(stop_level),
                'sl_max': float(sl_max), 'swing_hi': float(closer['price']),
                'reason': 'atr_closer_sl',
            }
    # No closer swing inside the ATR band — keep the real far swing.
    # Never invent a soft ATR stop that is not an OHLC pivot.
    return {
        'capped': False, 'closer_pivot': False, 'refuse': False,
        'max_atr_mult': mult,
        'stop_distance': float(stop_d), 'stop_level': float(stop_level),
        'sl_max': float(sl_max), 'reason': 'keep_far_swing',
    }


def apply_min_rr(*, action, entry, stop_level, profit_level, stop_d, profit_d,
                 floor, min_rr=None, store=None, atr_locked=False, atr=None,
                 highs=None, lows=None, allow_soft_shrink=True) -> dict:
    """Enforce ``profit_d / stop_d >= min_rr`` without swapping SL↔TP.

    Order (after far-SL ATR cap):
      1. Prefer a farther structural TP that clears RR (correct side only).
      2. Soft-shrink SL toward entry — but if ATR-locked, never below ~1×ATR
         (no hairline stop that looked like TP/SL swapped). When
         ``allow_soft_shrink=False`` (kept far OHLC swing), skip this step —
         never invent a non-pivot soft stop that abandons structure.
      3. Else refuse — never invent fantasy TP.
    """
    rr_need = float(min_rr) if min_rr is not None else _min_rr(store)
    rr_need = max(1.0, min(3.0, rr_need))
    entry_f = float(entry)
    stop_d = float(stop_d)
    profit_d = float(profit_d)
    stop_level = float(stop_level)
    profit_level = float(profit_level)
    floor_f = float(floor) if floor and floor > 0 else 0.0
    atr_v = _finite(atr)
    # After far-swing ATR cap, keep at least ~1×ATR of protective width.
    soft_floor = floor_f
    if atr_locked and atr_v is not None and atr_v > 0:
        soft_floor = max(floor_f, float(atr_v))
    out = {
        'ok': True, 'capped': False, 'tp_extended': False, 'min_rr': rr_need,
        'stop_distance': stop_d, 'profit_distance': profit_d,
        'stop_level': stop_level, 'profit_level': profit_level,
        'rr': (profit_d / stop_d) if stop_d > 1e-12 else None,
        'reason': 'rr_ok',
    }
    if stop_d <= 1e-12 or profit_d <= 1e-12:
        return dict(out, ok=False, reason='rr_bad_distances')
    if not levels_correct_side(action=action, entry=entry_f,
                               stop_level=stop_level, profit_level=profit_level):
        return dict(out, ok=False, reason='rr_sides_swapped')
    if profit_d / stop_d + 1e-12 >= rr_need:
        return out
    need_tp = float(stop_d) * rr_need
    # Prefer farther real TP on the correct side — never move TP onto SL side.
    ext = _extend_structural_tp(
        action=action, entry=entry_f, need_d=need_tp, highs=highs, lows=lows)
    if ext and float(ext['profit_distance']) + 1e-12 >= need_tp:
        profit_level = float(ext['profit_level'])
        profit_d = float(ext['profit_distance'])
        return {
            'ok': True, 'capped': False, 'tp_extended': True, 'min_rr': rr_need,
            'stop_distance': float(stop_d), 'profit_distance': float(profit_d),
            'stop_level': float(stop_level), 'profit_level': float(profit_level),
            'rr': float(profit_d / stop_d) if stop_d > 1e-12 else None,
            'reason': 'rr_extend_tp',
            'swing_hi': ext.get('swing_hi'), 'swing_lo': ext.get('swing_lo'),
        }
    if not allow_soft_shrink:
        return dict(out, ok=False,
                    reason='rr_need_farther_tp_keep_swing',
                    stop_distance=stop_d, profit_distance=profit_d,
                    rr=profit_d / stop_d)
    # Soft shrink SL — never below soft_floor (1×ATR when ATR-locked).
    capped = float(profit_d) / rr_need
    if capped + 1e-12 < soft_floor:
        return dict(out, ok=False,
                    reason=('rr_need_farther_tp' if atr_locked
                            else 'rr_unachievable_vs_floor'),
                    stop_distance=stop_d, profit_distance=profit_d,
                    rr=profit_d / stop_d)
    if capped + 1e-12 >= stop_d:
        return dict(out, ok=False, reason='rr_unachievable',
                    rr=profit_d / stop_d)
    stop_d = capped
    stop_level = _set_stop_from_distance(action=action, entry=entry_f, stop_d=stop_d)
    if not levels_correct_side(action=action, entry=entry_f,
                               stop_level=stop_level, profit_level=profit_level):
        return dict(out, ok=False, reason='rr_sides_swapped')
    return {
        'ok': True, 'capped': True, 'tp_extended': False, 'min_rr': rr_need,
        'stop_distance': float(stop_d), 'profit_distance': float(profit_d),
        'stop_level': float(stop_level), 'profit_level': float(profit_level),
        'rr': float(profit_d / stop_d) if stop_d > 1e-12 else None,
        'reason': 'rr_cap_sl',
    }


def swing_anchor_brackets(*, candles, entry, action, tick=0.01, spread=0.,
                          min_distance=0., atr=None, zone=None, kama=None,
                          store=None) -> dict:
    """Place SL/TP on OHLC swing High/Low — never invent median ±distance.

    Uses the full pivot list so post-BOS re-entry (entry outside last SH/SL)
    still anchors to the nearest protective swing and a structural target
    (next swing or measured move from the broken range). When ``kama`` is on
    the protective side of entry, SL clears both swing and KAMA (ATR buffer).
    """
    from . import structure_memory
    out = {
        'ready': False, 'reason': 'no_swings', 'source': 'swing_anchor',
        'stop_distance': None, 'profit_distance': None,
        'stop_level': None, 'profit_level': None,
        'swing_hi': None, 'swing_lo': None,
        'kama_stop': False, 'atr_used': None,
    }
    if action not in ('BUY', 'SELL'):
        return dict(out, reason='bad_action')
    entry_f = _finite(entry)
    if entry_f is None or entry_f <= 0:
        return dict(out, reason='bad_entry')
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
    try:
        mn = float(min_distance or 0.)
    except (TypeError, ValueError):
        mn = 0.
    atr_v = _finite(atr)
    buffer = max(2.0 * tick_f, 2.0 * sp)
    if atr_v is not None and atr_v > 0:
        buffer = max(buffer, 0.15 * atr_v)
        out['atr_used'] = float(atr_v)
    # Minimum protective distance: broker min + buffer, OR half ATR —
    # never place a hairline SL that ATR noise will stop out immediately.
    floor = mn + buffer if (mn + buffer) > 0 else tick_f
    if atr_v is not None and atr_v > 0:
        atr_floor = float(ATR_STOP_FLOOR_FRAC) * float(atr_v)
        if atr_floor > floor:
            floor = atr_floor
        out['atr_floor'] = float(atr_floor)
    kama_v = _finite(kama)

    path = structure_memory.normalise(candles, limit=80)
    piv = structure_memory.swing_pivots(path, left=2, right=2)
    bos = structure_memory.bos_choch(path, left=2, right=2)
    highs = list(piv.get('swing_highs') or [])
    lows = list(piv.get('swing_lows') or [])
    out['bos_event'] = bos.get('event')
    out['bos_bias'] = bos.get('bias')
    if bos.get('range_hi') is not None:
        out['swing_hi'] = float(bos['range_hi'])
    if bos.get('range_lo') is not None:
        out['swing_lo'] = float(bos['range_lo'])
    if not highs or not lows:
        return dict(out, reason='no_swing_pivots', bos_event=bos.get('event'))

    picked = _pick_structural_levels(
        action=action, entry=entry_f, highs=highs, lows=lows,
        floor=floor, buffer=buffer)
    if picked is None:
        return dict(out, reason='no_structural_swings_for_entry',
                    bos_event=bos.get('event'))

    stop_level = float(picked['stop_level'])
    profit_level = float(picked['profit_level'])
    if action == 'BUY':
        stop_d = entry_f - stop_level
        profit_d = profit_level - entry_f
    else:
        stop_d = stop_level - entry_f
        profit_d = entry_f - profit_level

    if stop_d < floor or profit_d < floor:
        return dict(out, reason='swing_distances_below_floor',
                    stop_distance=stop_d, profit_distance=profit_d,
                    stop_level=stop_level, profit_level=profit_level,
                    swing_hi=picked.get('swing_hi'), swing_lo=picked.get('swing_lo'))

    # Prefer a closer protective swing when one exists inside the multi-TF
    # soft cap — but NEVER rewrite to entry ± median, NEVER go below ATR floor,
    # and NEVER refuse a real structural swing just because it is wider than
    # the zone median.
    z = zone if isinstance(zone, dict) else {}
    zone_sl = _finite(z.get('range_distance')) or _finite(z.get('noise_distance'))
    if zone_sl is not None and zone_sl > 0 and stop_d > STRUCTURE_VS_MEDIAN_CAP * float(zone_sl):
        if action == 'BUY':
            cands = [s for s in lows
                     if float(s['price']) <= entry_f - floor
                     and (entry_f - (float(s['price']) - buffer))
                     <= STRUCTURE_VS_MEDIAN_CAP * float(zone_sl) + 1e-9
                     and (entry_f - (float(s['price']) - buffer)) + 1e-9 >= floor]
            if cands:
                closer = max(cands, key=lambda s: float(s['price']))
                stop_level = float(closer['price']) - buffer
                stop_d = entry_f - stop_level
                picked['swing_lo'] = float(closer['price'])
                picked['reason'] = 'swing_levels_closer_sl'
        else:
            cands = [s for s in highs
                     if float(s['price']) >= entry_f + floor
                     and ((float(s['price']) + buffer) - entry_f)
                     <= STRUCTURE_VS_MEDIAN_CAP * float(zone_sl) + 1e-9
                     and ((float(s['price']) + buffer) - entry_f) + 1e-9 >= floor]
            if cands:
                closer = min(cands, key=lambda s: float(s['price']))
                stop_level = float(closer['price']) + buffer
                stop_d = stop_level - entry_f
                picked['swing_hi'] = float(closer['price'])
                picked['reason'] = 'swing_levels_closer_sl'

    # KAMA (adaptive trend from same OHLC) — protective stop beyond swing+KAMA.
    if kama_v is not None:
        if action == 'BUY' and kama_v < entry_f:
            kama_stop = kama_v - buffer
            if kama_stop < stop_level:
                stop_level = kama_stop
                stop_d = entry_f - stop_level
                out['kama_stop'] = True
                picked['reason'] = (picked.get('reason') or 'swing_levels') + '+kama'
        elif action == 'SELL' and kama_v > entry_f:
            kama_stop = kama_v + buffer
            if kama_stop > stop_level:
                stop_level = kama_stop
                stop_d = stop_level - entry_f
                out['kama_stop'] = True
                picked['reason'] = (picked.get('reason') or 'swing_levels') + '+kama'

    if stop_d < floor or profit_d < floor:
        return dict(out, reason='swing_distances_below_floor',
                    stop_distance=stop_d, profit_distance=profit_d,
                    stop_level=stop_level, profit_level=profit_level,
                    kama_stop=out.get('kama_stop'))

    reason = picked.get('reason') or 'swing_levels'
    # 1) Far swing SL vs ATR → closer swing pivot only (never invent ATR stop).
    far = cap_far_swing_sl(
        action=action, entry=entry_f, stop_level=stop_level, stop_d=stop_d,
        floor=floor, atr=atr_v, highs=highs, lows=lows, buffer=buffer,
        store=store)
    if far.get('capped') and far.get('closer_pivot'):
        stop_d = float(far['stop_distance'])
        stop_level = float(far['stop_level'])
        reason = str(reason) + '+' + str(far.get('reason') or 'atr_closer_sl')
        if far.get('swing_hi') is not None:
            picked['swing_hi'] = float(far['swing_hi'])
        if far.get('swing_lo') is not None:
            picked['swing_lo'] = float(far['swing_lo'])
    elif far.get('reason') == 'keep_far_swing':
        # Far OHLC swing kept — never rewrite to ATR invent.
        reason = str(reason) + '+keep_far_swing'

    # 2) Min RR — prefer farther TP; never soft-shrink a kept far OHLC swing
    # into an invented ATR stop (that abandoned structure and looked swapped).
    keep_far = far.get('reason') == 'keep_far_swing'
    atr_locked = bool(far.get('capped') or keep_far)
    rr = apply_min_rr(
        action=action, entry=entry_f, stop_level=stop_level,
        profit_level=profit_level, stop_d=stop_d, profit_d=profit_d,
        floor=floor, store=store, atr_locked=atr_locked,
        atr=atr_v, highs=highs, lows=lows,
        allow_soft_shrink=not keep_far)
    if not rr.get('ok'):
        return dict(out, ready=False, reason=rr.get('reason') or 'rr_fail',
                    stop_distance=float(stop_d), profit_distance=float(profit_d),
                    stop_level=float(stop_level), profit_level=float(profit_level),
                    min_rr=rr.get('min_rr'), rr=rr.get('rr'),
                    atr_capped=bool(far.get('capped')),
                    kama_stop=out.get('kama_stop'))
    stop_d = float(rr['stop_distance']); profit_d = float(rr['profit_distance'])
    stop_level = float(rr['stop_level']); profit_level = float(rr['profit_level'])
    if rr.get('tp_extended'):
        reason = str(reason) + '+rr_extend_tp'
        if rr.get('swing_hi') is not None:
            picked['swing_hi'] = float(rr['swing_hi'])
        if rr.get('swing_lo') is not None:
            picked['swing_lo'] = float(rr['swing_lo'])
    if rr.get('capped'):
        reason = str(reason) + '+rr_cap_sl'
    if not levels_correct_side(action=action, entry=entry_f,
                               stop_level=stop_level, profit_level=profit_level):
        return dict(out, ready=False, reason='rr_sides_swapped',
                    stop_distance=float(stop_d), profit_distance=float(profit_d),
                    stop_level=float(stop_level), profit_level=float(profit_level),
                    atr_capped=bool(far.get('capped')))

    return {
        'ready': True,
        'reason': reason,
        'source': 'swing_anchor',
        'stop_distance': float(stop_d),
        'profit_distance': float(profit_d),
        'stop_level': float(stop_level),
        'profit_level': float(profit_level),
        'swing_hi': float(picked.get('swing_hi') or out.get('swing_hi') or 0),
        'swing_lo': float(picked.get('swing_lo') or out.get('swing_lo') or 0),
        'kappa': float(profit_d / stop_d) if stop_d > 1e-12 else DEFAULT_KAPPA,
        'min_rr': float(rr.get('min_rr') or DEFAULT_MIN_RR),
        'rr': float(profit_d / stop_d) if stop_d > 1e-12 else None,
        'rr_capped': bool(rr.get('capped')),
        'rr_tp_extended': bool(rr.get('tp_extended')),
        'atr_capped': bool(far.get('capped')),
        'sl_max': far.get('sl_max'),
        'bos_event': bos.get('event'),
        'bos_bias': bos.get('bias'),
        'buffer': float(buffer),
        'kama_stop': bool(out.get('kama_stop')),
        'atr_used': out.get('atr_used'),
        'kama': float(kama_v) if kama_v is not None else None,
    }


def decide_brackets_from_ohlc(zone, *, tick, spread=0., min_distance=0.,
                              tighten=1.0, entry=None, action=None,
                              candles=None, atr=None, kama=None,
                              require_swing=None, store=None) -> dict:
    """System decides SL/TP from this OHLC window — not multipliers, not hard fantasy.

    Preference order:
      1. Swing High/Low anchors when ``entry``+``action``+``candles`` are given
         (real chart levels — not entry ± median bar span).
      2. Else zone geometry: SL = adverse range, TP = κ×SL capped by ``max_tp``.

    When live OPEN provides entry/action/candles, ``require_swing`` defaults True:
    if swings cannot place levels, return ``ready=False`` instead of inventing
    median-zone ±distance (the post-BOS 2nd-open bug).

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

    live_ctx = (entry is not None and action in ('BUY', 'SELL') and candles)
    if require_swing is None:
        require_swing = bool(live_ctx)

    # --- Prefer swing-anchored levels (chart structure) ---
    swing = None
    if live_ctx:
        swing = swing_anchor_brackets(
            candles=candles, entry=entry, action=action, tick=tick_f,
            spread=sp, min_distance=mn, atr=atr, zone=z, kama=kama,
            store=store)
        if swing.get('ready'):
            # Correct order: swing → tighten → tick-snap → re-check RR → levels.
            # Never replace SL with stop_take() — that rewrites swing structure.
            from .math_rules import (
                snap_distance_to_tick, distances_meet_broker_min, _min_ticks)
            from decimal import Decimal as _Dec, ROUND_FLOOR
            # Decimal tighten — avoid float dust before snap (0.8*0.9 → 0.73).
            stop_d = float(_Dec(str(swing['stop_distance'])) * _Dec(str(t)))
            profit_d = float(_Dec(str(swing['profit_distance'])) * _Dec(str(t)))
            stop_d, stop_ticks = snap_distance_to_tick(stop_d, tick_f)
            profit_d, profit_ticks = snap_distance_to_tick(profit_d, tick_f)
            raised = False
            floor_ticks = _min_ticks(_Dec(str(mn)), _Dec(str(tick_f)))
            if floor_ticks and stop_ticks < floor_ticks:
                stop_ticks = int(floor_ticks)
                stop_d = float(_Dec(str(tick_f)) * stop_ticks)
                raised = True
            if floor_ticks and profit_ticks < floor_ticks:
                profit_ticks = int(floor_ticks)
                profit_d = float(_Dec(str(tick_f)) * profit_ticks)
                raised = True
            # Re-check min RR AFTER snap — ceil can drop 1.60 → 1.40.
            min_rr_need = float(swing.get('min_rr') or _min_rr(store))
            min_rr_need = max(1.0, min(3.0, min_rr_need))
            rr_now = (profit_d / stop_d) if stop_d > 1e-12 else 0.0
            if rr_now + 1e-12 < min_rr_need:
                # Prefer raise TP onto RR grid; else shrink SL (tick floor).
                need_tp = float(_Dec(str(stop_d)) * _Dec(str(min_rr_need)))
                new_tp, new_tp_ticks = snap_distance_to_tick(need_tp, tick_f)
                if new_tp / stop_d + 1e-12 >= min_rr_need:
                    profit_d, profit_ticks = float(new_tp), int(new_tp_ticks)
                else:
                    max_stop = float(_Dec(str(profit_d)) / _Dec(str(min_rr_need)))
                    tick_d = _Dec(str(tick_f))
                    floored = int(
                        (_Dec(str(max_stop)) / tick_d).to_integral_value(
                            rounding=ROUND_FLOOR))
                    if floored < 1:
                        floored = 0
                    if floor_ticks and floored < floor_ticks:
                        floored = 0  # cannot shrink enough
                    if floored >= 1 and floored < stop_ticks:
                        stop_ticks = int(floored)
                        stop_d = float(tick_d * stop_ticks)
                    if stop_d <= 1e-12 or profit_d / stop_d + 1e-12 < min_rr_need:
                        return {
                            'stop_distance': float(stop_d),
                            'profit_distance': float(profit_d),
                            'stop_level': None, 'profit_level': None,
                            'kappa': float(rr_now), 'sl_scale': 1.0,
                            'tighten': float(t), 'source': 'swing_anchor',
                            'ready': False, 'reason': 'rr_below_min_after_snap',
                            'min_rr': min_rr_need, 'rr': float(rr_now),
                            'swing': swing, 'swing_reason': swing.get('reason'),
                            'raised_to_broker_min': raised,
                        }
            sign = 1 if action == 'BUY' else -1
            e = float(entry)
            stop_level = e - sign * stop_d
            profit_level = e + sign * profit_d
            if not levels_correct_side(
                    action=action, entry=e, stop_level=stop_level,
                    profit_level=profit_level):
                return {
                    'stop_distance': None, 'profit_distance': None,
                    'stop_level': None, 'profit_level': None,
                    'kappa': None, 'sl_scale': 1.0,
                    'tighten': float(t), 'source': 'swing_anchor',
                    'ready': False, 'reason': 'rr_sides_swapped',
                    'swing': swing, 'swing_reason': swing.get('reason'),
                }
            if not distances_meet_broker_min(stop_d, profit_d, mn):
                return {
                    'stop_distance': float(stop_d),
                    'profit_distance': float(profit_d),
                    'stop_level': float(stop_level),
                    'profit_level': float(profit_level),
                    'kappa': float(profit_d / stop_d) if stop_d > 1e-12 else None,
                    'sl_scale': 1.0, 'tighten': float(t),
                    'source': 'swing_anchor', 'ready': False,
                    'reason': 'below_broker_min_after_snap',
                    'swing': swing, 'swing_reason': swing.get('reason'),
                    'raised_to_broker_min': raised,
                }
            eff_kappa = (profit_d / stop_d) if stop_d > 1e-12 else DEFAULT_KAPPA
            return {
                'stop_distance': float(stop_d),
                'profit_distance': float(profit_d),
                'stop_level': float(stop_level),
                'profit_level': float(profit_level),
                'kappa': float(eff_kappa),
                'sl_scale': 1.0,
                'ohlc_kappa': float(swing.get('kappa') or eff_kappa),
                'ohlc_sl': float(swing['stop_distance']),
                'ohlc_tp': float(swing['profit_distance']),
                'tighten': float(t),
                'source': 'swing_anchor',
                'ready': True,
                'lookback': z.get('lookback'),
                'median_up': z.get('median_up'),
                'median_down': z.get('median_down'),
                'pull_share': z.get('pull_share'),
                'favor_share': z.get('favor_share'),
                'swing_hi': swing.get('swing_hi'),
                'swing_lo': swing.get('swing_lo'),
                'bos_event': swing.get('bos_event'),
                'min_rr': min_rr_need,
                'rr': float(eff_kappa),
                'rr_capped': swing.get('rr_capped'),
                'stop_ticks': int(stop_ticks),
                'profit_ticks': int(profit_ticks),
                'raised_to_broker_min': raised,
                'swing': swing,
                'swing_reason': swing.get('reason'),
            }
        if require_swing:
            # Do NOT invent ohlc_zone ±distance for live OPEN — caller must skip.
            return {
                'stop_distance': None, 'profit_distance': None,
                'stop_level': None, 'profit_level': None,
                'kappa': None, 'sl_scale': 1.0,
                'ohlc_kappa': None, 'ohlc_sl': None, 'ohlc_tp': None,
                'tighten': float(t),
                'source': 'swing_anchor',
                'ready': False,
                'reason': (swing or {}).get('reason') or 'swing_unavailable',
                'lookback': z.get('lookback'),
                'median_up': z.get('median_up'),
                'median_down': z.get('median_down'),
                'swing_hi': (swing or {}).get('swing_hi'),
                'swing_lo': (swing or {}).get('swing_lo'),
                'bos_event': (swing or {}).get('bos_event'),
                'swing': swing,
                'swing_reason': (swing or {}).get('reason'),
            }

    ohlc_sl = _finite(z.get('range_distance'))
    if ohlc_sl is None or ohlc_sl <= 0:
        ohlc_sl = _finite(z.get('noise_distance'))
    ohlc_kappa = _finite(z.get('kappa')) or DEFAULT_KAPPA
    if ohlc_kappa <= 0 or not math.isfinite(ohlc_kappa):
        ohlc_kappa = DEFAULT_KAPPA
    ohlc_tp = _finite(z.get('max_tp'))
    buffer = max(2.0 * tick_f, 2.0 * sp)
    floor = mn + buffer if (mn + buffer) > 0 else tick_f
    # Fallback: SL from OHLC adverse move (or broker floor if quieter).
    raw_sl = float(ohlc_sl) if ohlc_sl is not None and ohlc_sl > 0 else floor
    stop_d = max(floor, raw_sl) * t
    # TP from OHLC κ, limited by what this window actually reaches.
    raw_tp = float(ohlc_kappa) * stop_d
    if ohlc_tp is not None and ohlc_tp > 0:
        profit_d = min(raw_tp, float(ohlc_tp) * t)
    else:
        profit_d = raw_tp
    profit_d = max(floor, profit_d)
    # Effective κ from OHLC only — no hardcoded 1:1 reshape / RR floor.
    eff_kappa = (profit_d / stop_d) if stop_d > 1e-12 else float(ohlc_kappa)
    # Snap onto broker tick grid via stop_take (sl_scale=1 — no invent layer).
    entry_f = _finite(entry) or 1000.0
    act = action if action in ('BUY', 'SELL') else 'BUY'
    snapped = stop_take(
        entry_f, act, tick_f, sp, mn,
        kappa=max(0.5, float(eff_kappa)), sl_scale=1.0,
        range_distance=stop_d)
    sign = 1 if act == 'BUY' else -1
    return {
        'stop_distance': float(snapped['stop_distance']),
        'profit_distance': float(snapped['profit_distance']),
        'stop_level': float(entry_f) - sign * float(snapped['stop_distance']),
        'profit_level': float(entry_f) + sign * float(snapped['profit_distance']),
        'kappa': float(snapped.get('kappa') or eff_kappa),
        'sl_scale': 1.0,
        'ohlc_kappa': float(ohlc_kappa),
        'ohlc_sl': float(raw_sl),
        'ohlc_tp': float(ohlc_tp) if ohlc_tp is not None else None,
        'tighten': float(t),
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
        'swing': swing,
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
                     min_distance: float=0., tighten: float=1.0,
                     entry=None, action=None, atr=None, kama=None,
                     require_swing=None) -> dict:
    """OHLC swing (or zone) decides κ / SL / TP — learned multipliers do not set width."""
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
    # LIVE protect/OPEN share require_swing default when entry+action+candles given.
    # Pass store so learned min_rr / ATR-cap knobs match OPEN path (audit).
    decided=decide_brackets_from_ohlc(
        zone,tick=tick,spread=spread,min_distance=min_distance,tighten=tighten,
        entry=entry,action=action,candles=candles,atr=atr,kama=kama,
        require_swing=require_swing,store=store)
    src=str(decided.get('source') or 'ohlc_zone')
    if decided.get('kappa') is not None:
        save_params(store,decided['kappa'],1.0,
                    reason=(f"{src} κ={decided.get('ohlc_kappa')} "
                            f"sl={decided.get('ohlc_sl')} tp={decided.get('ohlc_tp')}"))
    learned=load_multipliers(store)
    return {
        'kappa':float(decided['kappa']) if decided.get('kappa') is not None else DEFAULT_KAPPA,
        'sl_scale':1.0,'explored':False,
        'max_tp':decided.get('ohlc_tp'),'base_sl':decided.get('ohlc_sl'),
        'ohlc_kappa':decided.get('ohlc_kappa'),
        'zone_ready':bool(zone.get('ready')),
        'ready':bool(decided.get('ready')),
        'median_up':zone.get('median_up'),'median_down':zone.get('median_down'),
        'kappa_mult':learned['kappa'],'sl_mult':learned['sl_scale'],
        'lookback':zone.get('lookback'),
        'stop_distance':decided.get('stop_distance'),
        'profit_distance':decided.get('profit_distance'),
        'stop_level':decided.get('stop_level'),
        'profit_level':decided.get('profit_level'),
        'bracket_source':src,
        'swing_hi':decided.get('swing_hi'),
        'swing_lo':decided.get('swing_lo'),
        'reason':decided.get('reason') or decided.get('swing_reason'),
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
