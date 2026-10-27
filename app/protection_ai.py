"""Broker-native trailing SL for ROBOT-OPENED positions only.

Live trail uses Capital numbers only:
  - bid/ask (ticks) and completed OHLC extremes *since the position opened*
  - dealingRules minStopOrProfitDistance
  - decimalPlacesFactor / tick price grid

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
    """Best favorable price from live quotes + OHLC since open — nothing invented."""
    if direction=='BUY':
        tip=_finite(bid)
        if tip is None:return None
        if prior_extreme is not None:tip=max(tip,float(prior_extreme))
        for row in candles or ():
            t=candle_time(row.get('at'))
            if opened_at and t and t<opened_at:continue
            h=_finite(row.get('high'))
            if h is not None:tip=max(tip,h)
        return tip
    if direction=='SELL':
        tip=_finite(ask)
        if tip is None:return None
        if prior_extreme is not None:tip=min(tip,float(prior_extreme))
        for row in candles or ():
            t=candle_time(row.get('at'))
            if opened_at and t and t<opened_at:continue
            low=_finite(row.get('low'))
            if low is not None:tip=min(tip,low)
        return tip
    return None


def propose_broker_trail(position,market,initial,*,extreme=None,mark_time=None):
    """Trail SL behind broker extreme by the *opening* stop distance.

    Using only live min_distance glued the stop to the market (tip±min) and
    produced £0 / -£0.02 Gold scratches every minute. Trail gap is the broker
    opening bracket risk already accepted on the deal (`original_risk`), never
    tighter than minStopOrProfitDistance.

    BUY:  stop = peak − gap (floor).  SELL: stop = trough + gap (ceil).
    Activates only after favorable excursion ≥ gap (so first ratchet ≈ BE+).
    """
    if market.get('status')!='TRADEABLE':return None
    direction=position.get('direction');sign={'BUY':1,'SELL':-1}.get(direction)
    if sign is None:return None
    entry=_finite(position.get('level'))
    stop=_finite(position.get('stopLevel'))
    tp=_finite(position.get('profitLevel'))
    tick=_finite(market.get('tick'));bid=_finite(market.get('bid'));ask=_finite(market.get('ask'))
    minimum=_finite(market.get('min_distance'))
    gap=_finite(initial.get('original_risk'))
    if any(v is None for v in (entry,stop,tp,tick,bid,ask,minimum)):
        return None
    if min(entry,stop,tp,tick,bid,ask)<=0 or minimum<0 or ask<bid:return None
    if initial.get('owner')!='AUTO':return None
    if position.get('epic')!=market.get('epic') or position.get('dealId')!=initial.get('deal_id'):
        return None
    if sign*(tp-stop)<=0:return None
    # Gap = opening SL distance (broker-confirmed). Floor at dealing min.
    if gap is None or gap<=0:gap=minimum
    gap=max(gap,minimum)
    exit_quote=bid if sign==1 else ask
    if sign*(tp-exit_quote)<=0:return None  # already at/through TP
    tip=_finite(extreme)
    if tip is None:
        tip=bid if sign==1 else ask
    favorable=sign*(exit_quote-entry)
    tip_fav=sign*(tip-entry)
    # Need the extreme itself to have traveled ≥ gap before ratcheting.
    if tip_fav<gap:return None
    if sign==1:
        tip=max(tip,bid)
        if tip<=entry:return None
        desired=tip-gap
        # Never place tighter than broker min from live bid.
        desired=min(desired,bid-minimum)
        new_stop=_quantize(desired,tick,up=False)
        new_stop=_away_from_market(new_stop,'BUY',bid,ask,minimum,tick)
        if new_stop<=stop or new_stop>=tip or sign*(tp-new_stop)<=0:return None
        if new_stop<stop+tick-1e-12:return None
    else:
        tip=min(tip,ask)
        if tip>=entry:return None
        desired=tip+gap
        desired=max(desired,ask+minimum)
        new_stop=_quantize(desired,tick,up=True)
        new_stop=_away_from_market(new_stop,'SELL',bid,ask,minimum,tick)
        if new_stop>=stop or new_stop<=tip or sign*(tp-new_stop)<=0:return None
        if new_stop>stop-tick+1e-12:return None
    return {'stop_level':new_stop,'profit_level':None,
            'reason':'broker extreme ± opening stop distance (≥ minStop)',
            'extreme':tip,'trail_gap':gap,'min_distance':minimum,'tick':tick,
            'observed_profit_distance':round(favorable,5),
            'bar_time':mark_time}


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
