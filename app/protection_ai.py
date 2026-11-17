"""Broker-native trailing SL for ROBOT-OPENED positions only.

Live trail lifecycle (WATCH → ARMED → RATCHET/BE → TRAIL):
  - bid/ask (ticks) and completed 1m OHLC extremes *since the position opened*
  - after BE: stop follows recent 1m swing lows/highs (structure), not a huge
    fraction of opening R / 2×minStop tip±gap
  - dealingRules minStopOrProfitDistance
  - decimalPlacesFactor / tick price grid
  - adaptive arm from MFE / structure EV — never wait for tip_fav ≥ full R
    (that equals TP on 1:1 books and kills trail)

    No invented retention / activation_r / TP-multiplier policy grid on the live path.
Walk-forward `select_policy` remains as an offline research helper / tests only.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import statistics

from .market_context import candle_time


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None


def _quantize(v,tick,up):
    """Snap to the broker price grid; return a clean float (no binary dust)."""
    q=Decimal(str(tick));val=Decimal(str(v))
    if q<=0:raise ValueError('tick must be positive')
    snapped=(val/q).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*q
    return float(snapped)


def _away_from_market(level,direction,bid,ask,minimum,tick):
    """After quantize, step further if still inside broker min distance."""
    q=Decimal(str(tick));lvl=Decimal(str(level))
    mn=Decimal(str(minimum));b=Decimal(str(bid));a=Decimal(str(ask))
    if direction=='BUY':
        while b-lvl+Decimal('1e-12')<mn:
            lvl-=q
        return float(lvl)
    while lvl-a+Decimal('1e-12')<mn:
        lvl+=q
    return float(lvl)


def trail_extreme_from_broker(direction,bid,ask,candles=None,opened_at=None,prior_extreme=None):
    """Best favorable *exit-side* price from live quotes + OHLC since open.

    Stored M1 high/low are midprices. Convert with per-bar spread when present
    so BUY tip ≈ bid extreme and SELL tip ≈ ask extreme — mid highs alone
    inflate the tip and glue SL too tight (early stop-outs vs the chart).
    """
    if direction=='BUY':
        tip=_finite(bid)
        if tip is None:return None
        if prior_extreme is not None:tip=max(tip,float(prior_extreme))
        for row in candles or ():
            t=candle_time(row.get('at'))
            if opened_at and t and t<opened_at:continue
            h=_finite(row.get('high'))
            if h is None:continue
            sp=_finite(row.get('spread'))
            # mid high → approximate bid high for BUY exit path
            if sp is not None and sp>=0:h=h-0.5*sp
            tip=max(tip,h)
        return tip
    if direction=='SELL':
        tip=_finite(ask)
        if tip is None:return None
        if prior_extreme is not None:tip=min(tip,float(prior_extreme))
        for row in candles or ():
            t=candle_time(row.get('at'))
            if opened_at and t and t<opened_at:continue
            low=_finite(row.get('low'))
            if low is None:continue
            sp=_finite(row.get('spread'))
            # mid low → approximate ask low for SELL exit path
            if sp is not None and sp>=0:low=low+0.5*sp
            tip=min(tip,low)
        return tip
    return None


def _trail_knobs(store=None):
    """Post-BE trail fractions from self_params Store — not frozen module constants."""
    from .self_params import DEFAULTS, load
    sp=load(store) if store is not None else dict(DEFAULTS)
    return {
        'min_mult':float(sp.get('trail_min_mult',DEFAULTS['trail_min_mult'])),
        'open_frac':float(sp.get('trail_open_gap_frac',DEFAULTS['trail_open_gap_frac'])),
        'fallback_frac':float(sp.get('trail_fallback_frac',DEFAULTS['trail_fallback_frac'])),
        'ohlc_bars':int(sp.get('trail_ohlc_bars',DEFAULTS.get('trail_ohlc_bars',5))),
        'ohlc_buffer_frac':float(sp.get('trail_ohlc_buffer_frac',
                                        DEFAULTS.get('trail_ohlc_buffer_frac',0.15))),
    }


def ohlc_1m_structure_stop(direction,candles,*,opened_at=None,lookback=5,buffer=0.0,
                           use_close=True):
    """Trail anchor from recent completed 1m OHLC close swings.

    BUY → stop under the lowest *close* of the last N completed bars (since open).
    SELL → stop above the highest *close* of those bars.
    Mid close is adjusted by per-bar spread when present so the anchor sits on
    the exit side (bid / ask), matching live trail tips. Falls back to high/low
    when close is missing.
    """
    if direction not in ('BUY','SELL'):
        return None
    rows=[]
    for row in candles or ():
        t=candle_time(row.get('at'))
        if opened_at and t and t<opened_at:
            continue
        h=_finite(row.get('high'));low=_finite(row.get('low'))
        c=_finite(row.get('close'))
        if h is None or low is None or h<=0 or low<=0 or h<low:
            continue
        sp=_finite(row.get('spread'))
        if use_close and c is not None and c>0:
            # Completed 1m close → exit-side approx for swing low/high.
            if sp is not None and sp>=0:
                c_bid=c-0.5*sp;c_ask=c+0.5*sp
            else:
                c_bid=c_ask=c
            rows.append((t,c_ask,c_bid))
            continue
        if sp is not None and sp>=0:
            h=h-0.5*sp;low=low+0.5*sp
        rows.append((t,h,low))
    if not rows:
        return None
    n=max(1,int(lookback or 5))
    window=rows[-n:]
    buf=max(0.0,float(buffer or 0.0))
    if direction=='BUY':
        swing=min(r[2] for r in window)
        return swing-buf
    swing=max(r[1] for r in window)
    return swing+buf


def _post_be_trail_gap(open_gap,minimum,zone_range=None,store=None,*,
                       min_mult=None,open_frac=None,fallback_frac=None):
    """After BE, follow 1m OHLC noise — never inflate past real candle range.

    When zone_range (median M1 high−low) is known, that is the gap: floored
    only by broker minStop, capped by opening risk. The old 2×min / 35%R
    floor forced a huge tip±gap and ignored tighter 1m structure.
    Without OHLC yet, fall back to Store fractions (≥ minStop).
    """
    knobs=_trail_knobs(store)
    if min_mult is not None:knobs['min_mult']=float(min_mult)
    if open_frac is not None:knobs['open_frac']=float(open_frac)
    if fallback_frac is not None:knobs['fallback_frac']=float(fallback_frac)
    open_gap=float(open_gap);minimum=float(minimum)
    zr=_finite(zone_range)
    if zr is not None and zr>0:
        # Prefer real 1m median range — do not push gap up to 2×min / %R.
        return max(minimum,min(open_gap,zr))
    # No OHLC zone yet — modest Store fallback, never tip±minStop alone.
    soft=max(minimum,min(open_gap,max(knobs['min_mult']*minimum,
                                      knobs['open_frac']*open_gap,
                                      knobs['fallback_frac']*open_gap)))
    return soft


def propose_broker_trail(position,market,initial,*,extreme=None,mark_time=None,
                         zone_range=None,store=None,arm_decision=None,candles=None):
    """Full trail lifecycle — WATCH → ARMED → RATCHET/BE → TRAIL.

    Phases (robot-owned, not human 1R law):
      • WATCH: not armed yet → caller must not propose (returns None if unarmed)
      • ARMED (pre-BE): first moves toward BE once MFE covers adaptive fraction
        of R. If full BE violates broker minStop, still **partial-ratchet** the
        stop toward entry (never widen). Never wait for tip_fav ≥ full R —
        that equals TP on 1:1 books and kills trail forever.
      • BE LOCKED: stop at/through entry → trail under/over recent 1m OHLC
        swings (structure), not a huge fraction of opening R.
      • TRAIL: keep ratcheting; always re-send live profitLevel so Capital
        does not turn Take Profit Off.

    BUY: prefer last-N 1m swing low; fallback tip − OHLC gap.
    SELL: prefer last-N 1m swing high; fallback tip + OHLC gap.
    """
    if market.get('status')!='TRADEABLE':return None
    direction=position.get('direction');sign={'BUY':1,'SELL':-1}.get(direction)
    if sign is None:return None
    entry=_finite(position.get('level'))
    stop=_finite(position.get('stopLevel'))
    # Preserve live TP when present. Trail still works if Capital already
    # shows Take Profit Off (tp is None) — but we never *clear* a live TP.
    tp=_finite(position.get('profitLevel'))
    tick=_finite(market.get('tick'));bid=_finite(market.get('bid'));ask=_finite(market.get('ask'))
    minimum=_finite(market.get('min_distance'))
    gap=_finite(initial.get('original_risk'))
    if any(v is None for v in (entry,stop,tick,bid,ask,minimum)):
        return None
    positives=(entry,stop,tick,bid,ask)+( (tp,) if tp is not None else () )
    if min(positives)<=0 or minimum<0 or ask<bid:return None
    if initial.get('owner')!='AUTO':return None
    if position.get('epic')!=market.get('epic') or position.get('dealId')!=initial.get('deal_id'):
        return None
    if tp is not None and sign*(tp-stop)<=0:return None
    # Refuse tip±minStop glue when opening risk is missing/corrupt.
    if gap is None or gap<=0:
        return None
    open_gap=max(gap,minimum)
    # Stop already on the profit side of entry → BE (or better) is banked.
    be_locked=sign*(stop-entry)>=-1e-12
    early_arm=bool(isinstance(arm_decision,dict) and arm_decision.get('arm'))
    knobs=_trail_knobs(store)
    zr=zone_range if zone_range is not None else initial.get('zone_range')
    zr=_finite(zr)
    structure_stop=None
    structure_used=False
    if be_locked:
        trail_gap=_post_be_trail_gap(open_gap,minimum,zr,store=store)
        # Primary past-BE path: recent completed 1m swing (not tip − %R).
        opened_at=None
        try:
            opened_at=candle_time(initial.get('opened')) if initial.get('opened') else None
        except Exception:
            opened_at=None
        buf=0.0
        if zr is not None and zr>0:
            buf=max(0.0,float(knobs['ohlc_buffer_frac'])*zr)
        structure_stop=ohlc_1m_structure_stop(
            direction,candles,opened_at=opened_at,
            lookback=knobs['ohlc_bars'],buffer=buf)
    else:
        trail_gap=open_gap
    exit_quote=bid if sign==1 else ask
    # Only abort when exit quote is clearly *through* TP by more than a tick
    # (fill imminent). Near-TP on 1:1 books must still allow BE/ratchet —
    # waiting for tip_fav ≥ full R equals waiting for TP itself.
    if tp is not None and sign*(tp-exit_quote)<-tick+1e-12:
        return None
    tip=_finite(extreme)
    if tip is None:
        tip=bid if sign==1 else ask
    favorable=sign*(exit_quote-entry)
    tip_fav=sign*(tip-entry)
    phase='trail' if be_locked else 'watch'
    # Adaptive first arm: once MFE/structure evidence says arm, allow BE-lock
    # or partial ratchet when tip_fav < full open_gap (never require full R).
    if not be_locked and early_arm and tip_fav>0:
        try:
            from .self_params import load as _sp_load
            arm_frac=float((_sp_load(store) or {}).get('trail_arm_mfe_frac',0.30))
        except Exception:
            arm_frac=0.30
        arm_frac=max(0.25,min(1.0,arm_frac))
        # BE from ~30% of opening R (operator), then 1m close-swing trail.
        floor_need=arm_frac*open_gap
        if tip_fav<floor_need:
            return None
        # Shrink gap to tip_fav so the first ratchet can lock near BE.
        trail_gap=min(open_gap,max(minimum,tip_fav))
        need=floor_need
        phase='armed'
    else:
        # After BE with 1m structure, do not wait for another full gap of MFE —
        # any favorable tip past entry can advance the swing-based stop.
        if be_locked and structure_stop is not None:
            need=minimum
        else:
            need=open_gap if not be_locked else trail_gap
    if tip_fav<need:return None
    if sign==1:
        tip=max(tip,bid)
        if tip<=entry:return None
        legal_cap=bid-minimum  # farthest toward market the broker allows
        if be_locked and structure_stop is not None:
            desired=min(float(structure_stop),legal_cap,tip-tick)
            structure_used=True
            trail_gap=max(minimum,tip-desired) if desired<tip else trail_gap
        else:
            desired=tip-trail_gap
            desired=min(desired,legal_cap)
        if early_arm and not be_locked:
            if entry<=legal_cap+1e-12:
                # Full BE lock is legal vs minStop.
                desired=max(desired,entry)
                phase='be_lock'
            else:
                # BE illegal — partial ratchet toward entry (never invent BE).
                desired=min(desired,legal_cap)
                phase='ratchet'
        # Past BE: never give the locked BE back through a deep swing wick.
        if be_locked:
            desired=max(desired,entry)
        new_stop=_quantize(desired,tick,up=False)
        new_stop=_away_from_market(new_stop,'BUY',bid,ask,minimum,tick)
        # After away_from_market, BE lock may have been pushed loss-side of
        # entry — only reject that when we intended a full BE lock. Partial
        # ratchet may sit loss-side of entry while still improving the stop.
        if phase=='be_lock' and new_stop<entry-1e-12:
            # Fall back to best legal improvement toward entry.
            new_stop=_quantize(legal_cap,tick,up=False)
            new_stop=_away_from_market(new_stop,'BUY',bid,ask,minimum,tick)
            phase='ratchet'
        if new_stop<=stop or new_stop>=tip:return None
        if tp is not None and sign*(tp-new_stop)<=0:return None
        if new_stop<stop+tick-1e-12:return None
    else:
        tip=min(tip,ask)
        if tip>=entry:return None
        legal_floor=ask+minimum
        if be_locked and structure_stop is not None:
            desired=max(float(structure_stop),legal_floor,tip+tick)
            structure_used=True
            trail_gap=max(minimum,desired-tip) if desired>tip else trail_gap
        else:
            desired=tip+trail_gap
            desired=max(desired,legal_floor)
        if early_arm and not be_locked:
            if entry>=legal_floor-1e-12:
                desired=min(desired,entry)
                phase='be_lock'
            else:
                desired=max(desired,legal_floor)
                phase='ratchet'
        if be_locked:
            desired=min(desired,entry)
        new_stop=_quantize(desired,tick,up=True)
        new_stop=_away_from_market(new_stop,'SELL',bid,ask,minimum,tick)
        if phase=='be_lock' and new_stop>entry+1e-12:
            new_stop=_quantize(legal_floor,tick,up=True)
            new_stop=_away_from_market(new_stop,'SELL',bid,ask,minimum,tick)
            phase='ratchet'
        if new_stop>=stop or new_stop<=tip:return None
        if tp is not None and sign*(tp-new_stop)<=0:return None
        if new_stop>stop-tick+1e-12:return None
    if be_locked and structure_used:
        reason='trail past BE (1m OHLC structure)'
    elif be_locked:
        reason='trail past BE (OHLC zone gap)'
    elif phase=='be_lock':
        reason='adaptive early arm → BE lock'
    elif phase=='ratchet':
        reason='adaptive early arm → partial ratchet toward BE'
    else:
        reason='broker extreme ± opening stop distance (≥ minStop)'
    return {'stop_level':new_stop,'profit_level':tp,
            'reason':reason,'phase':phase,
            'extreme':tip,'trail_gap':trail_gap,'open_gap':open_gap,
            'zone_range':zr,
            'structure_stop':_finite(structure_stop) if structure_used else None,
            'be_locked':be_locked,'early_arm':early_arm,
            'min_distance':minimum,'tick':tick,
            'observed_profit_distance':round(favorable,5),
            'bar_time':mark_time,'preserve_tp':tp is not None}


# --- Offline research helper (not used on the live trail path) -----------------

def _candles(rows):
    valid=[]
    for b in rows:
        t=candle_time(b.get('at'))
        vals=[_finite(b.get(k)) for k in ('open','high','low','close')]
        if t is None or any(v is None or v<=0 for v in vals):continue
        o,h,l,c=vals
        if h<max(o,l,c) or l>min(o,c,h):continue
        valid.append((t,o,h,l,c))
    valid.sort()
    out=[]
    for b in valid:
        if out and b[0]==out[-1][0]:out[-1]=b
        else:out.append(b)
    return out


def _replay(window,direction,risk,profit,spread,retain,trigger,tp_mult):
    sign=1 if direction=='BUY' else -1
    entry=window[0][4]
    peak=0.
    stop=-risk
    target=profit*tp_mult
    for b in window[1:]:
        worst=(b[3] if sign==1 else b[2])
        best=(b[2] if sign==1 else b[3])
        worst=sign*(worst-entry)
        best=sign*(best-entry)
        if worst<=stop:return stop-spread
        if best>=target:return target-spread
        peak=max(peak,best)
        if peak>=trigger*risk:
            stop=max(stop,retain*peak)
    return sign*(window[-1][4]-entry)-spread


def _windows(rows,horizon=8):
    clean=_candles(rows)
    wins=[]
    for k in range(0,len(clean)-horizon,horizon):
        w=clean[k:k+horizon+1]
        if len(w)==horizon+1 and all(w[i+1][0]-w[i][0]==timedelta(minutes=1) for i in range(horizon)):
            wins.append(w)
    return wins


def select_policy(rows,direction,risk,profit,spread):
    """Offline walk-forward research only — live engine does not call this."""
    if direction not in ('BUY','SELL') or min(risk,profit)<=0 or spread<0:
        return {'ready':False,'reason':'Invalid instrument or initial bracket'}
    windows=_windows(rows)
    count=len(windows)
    if count<15:
        return {'ready':False,'reason':'Insufficient non-overlapping historical paths','samples':count}
    split=max(9,int(count*.67))
    train,holdout=windows[:split],windows[split:]
    if len(holdout)<5:
        return {'ready':False,'reason':'Insufficient untouched holdout paths','samples':count}
    baseline=(0.0,1e9,1.0)
    candidates=[(retain,trigger,multi) for retain in (.35,.5,.65)
                for trigger in (.8,1.2,1.6) for multi in (1.,1.25,1.5)]
    def measure(paths,policy):
        vals=[_replay(w,direction,risk,profit,spread,*policy)/risk for w in paths]
        return statistics.mean(vals)-.5*statistics.mean(max(0.,-v) for v in vals)
    base_train=measure(train,baseline)
    ranked=sorted(((measure(train,p),p) for p in candidates),reverse=True)
    candidate=ranked[0][1]
    val=measure(holdout,candidate)
    base_val=measure(holdout,baseline)
    if val<=base_val+.04:
        return {'ready':False,'reason':'No trailing/target candidate improved forward holdout',
                'samples':count,'validation_uplift_r':round(val-base_val,4)}
    return {'ready':True,'reason':'Historical candidate passed chronological holdout',
            'samples':count,'train_score_r':round(ranked[0][0],4),
            'baseline_train_r':round(base_train,4),
            'holdout_score_r':round(val,4),'baseline_holdout_r':round(base_val,4),
            'retention':candidate[0],'activation_r':candidate[1],
            'tp_multiplier':candidate[2]}


def propose_update(position,market,initial,policy,bar_time):
    """Compat wrapper: live path ignores invented policy and uses broker trail."""
    return propose_broker_trail(position,market,initial,extreme=None,mark_time=bar_time)
