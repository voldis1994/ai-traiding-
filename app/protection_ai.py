"""Evidence-led SL/TP evolution for ROBOT-OPENED positions only.

Learn from CLOSED, consecutive minute candles. Candidate trailing and take-profit
policies compete against the original bracket on chronological, non-overlapping
walk-forward windows; no lookahead candle from the current minute is used.

The evaluator is deliberately conservative about within-candle ambiguity: a stop
is checked BEFORE a target and a trailing stop ratchets only AFTER that candle.
Returns are price units, not cash P&L; commissions, slippage and financing are
not simulated. A winning shadow candidate is not evidence of future profit.
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
    # str→float keeps values like 4006.1 exact enough for JSON stopLevel
    return float(snapped)

def _broker_guard(minimum,tick,spread):
    """Same dealing cushion as open brackets: min + max(2·tick, 2·spread)."""
    return float(Decimal(str(minimum))+max(2*Decimal(str(tick)),2*Decimal(str(spread))))

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
    """Price-unit outcome for one historical path; pessimistic stop-first OHLC."""
    sign=1 if direction=='BUY' else -1
    entry=window[0][4]
    peak=0.
    stop=-risk
    target=profit*tp_mult
    for b in window[1:]:
        # In signed profit units: BUY worst=low, best=high; SELL inverse.
        worst=(b[3] if sign==1 else b[2])  # low for BUY, high for SELL
        best=(b[2] if sign==1 else b[3])   # high for BUY, low for SELL
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
    # Windows are non-overlapping, chronological, with a complete observed path.
    for k in range(0,len(clean)-horizon,horizon):
        w=clean[k:k+horizon+1]
        if len(w)==horizon+1 and all(w[i+1][0]-w[i][0]==timedelta(minutes=1) for i in range(horizon)):
            wins.append(w)
    return wins


def select_policy(rows,direction,risk,profit,spread):
    """Return validated policy or None, with transparency about sample counts.

    The candidate's held-out risk-adjusted score must beat keeping the initial
    fixed bracket. Evaluation never sees future candles beyond available rows.
    """
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
    baseline=(0.0,1e9,1.0)  # retain=0 + never-trigger trailing, TP 1x
    candidates=[(retain,trigger,multi) for retain in (.35,.5,.65)
                for trigger in (.8,1.2,1.6) for multi in (1.,1.25,1.5)]
    def measure(paths,policy):
        vals=[_replay(w,direction,risk,profit,spread,*policy)/risk for w in paths]
        # Penalise left tail. This is not a calibrated expected return.
        return statistics.mean(vals)-.5*statistics.mean(max(0.,-v) for v in vals)
    base_train=measure(train,baseline)
    ranked=sorted(((measure(train,p),p) for p in candidates),reverse=True)
    candidate=ranked[0][1]
    val=measure(holdout,candidate)
    base_val=measure(holdout,baseline)
    # Fail closed if the improvement fails in the unseen chronological segment.
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
    """Calculate market-compatible changes. Never widen SL / remove protection.

    Returns None for HOLD. No movement can exceed the broker's minimum distance;
    bot-origin metadata and broker confirmation are verified by the engine.
    """
    if not policy.get('ready') or not bar_time:return None
    if market.get('status')!='TRADEABLE':return None
    direction=position.get('direction');sign={'BUY':1,'SELL':-1}.get(direction)
    if sign is None:return None
    entry=_finite(position.get('level'))
    stop=_finite(position.get('stopLevel'))
    tp=_finite(position.get('profitLevel'))
    tick=_finite(market.get('tick'));bid=_finite(market.get('bid'));ask=_finite(market.get('ask'))
    minimum=_finite(market.get('min_distance'));spread=_finite(market.get('spread'))
    risk=_finite(initial.get('original_risk'));original_tp=_finite(initial.get('original_profit'))
    if any(v is None for v in (entry,stop,tp,tick,bid,ask,minimum,spread,risk,original_tp)):
        return None
    if min(entry,stop,tp,tick,bid,ask,risk,original_tp)<=0 or minimum<0 or spread<0 or ask<bid:return None
    if initial.get('owner')!='AUTO':return None
    if position.get('epic')!=market.get('epic') or position.get('dealId')!=initial.get('deal_id'):return None
    # A profitable, ratcheted SL may cross the entry, but must stay on the
    # correct side of the existing profit target.
    if sign*(tp-stop)<=0:return None
    exit_quote=bid if sign==1 else ask
    # A broker close may be in flight if price has already crossed the TP.
    # Never issue a modification against an effectively finished bracket.
    if sign*(tp-exit_quote)<=0:return None
    favorable=sign*(exit_quote-entry)
    if favorable<=0:return None
    activation=policy['activation_r']*risk
    if favorable<activation:return None
    # Same floor as open path — Capital rejects stops inside minStopOrProfitDistance.
    # Old 0.2·spread cushion + wrong tick produced rejected levels like 4006.075.
    guard=_broker_guard(minimum,tick,spread)
    step=max(2*tick,spread*.1)
    desired=entry+sign*(policy['retention']*favorable)
    if sign==1:
        desired=min(desired,bid-guard)
        new_stop=_quantize(desired,tick,up=False)
        new_stop=_away_from_market(new_stop,'BUY',bid,ask,minimum,tick)
        stop_changed=new_stop>=stop+step
    else:
        desired=max(desired,ask+guard)
        new_stop=_quantize(desired,tick,up=True)
        new_stop=_away_from_market(new_stop,'SELL',bid,ask,minimum,tick)
        stop_changed=new_stop<=stop-step
    # TP may be EXTENDED, never pulled into the current spread. Trend evidence
    # comes from the already completed OHLC context via the caller's gate.
    new_tp=None
    if initial.get('trend_aligned') and policy['tp_multiplier']>1.:
        remaining=sign*(tp-exit_quote)
        if 0<remaining<=risk:
            target=entry+sign*(original_tp*policy['tp_multiplier'])
            if sign==1:
                target=max(target,ask+guard)
                new_tp=_quantize(target,tick,up=True)
                new_tp=_away_from_market(new_tp,'SELL',bid,ask,minimum,tick)  # TP above ask
                if new_tp<=tp+2*tick:new_tp=None
            else:
                target=min(target,bid-guard)
                new_tp=_quantize(target,tick,up=False)
                new_tp=_away_from_market(new_tp,'BUY',bid,ask,minimum,tick)  # TP below bid
                if new_tp>=tp-2*tick:new_tp=None
    if not stop_changed and new_tp is None:return None
    return {'stop_level':new_stop if stop_changed else None,'profit_level':new_tp,
            'reason':'historical walk-forward policy + current favorable excursion',
            'observed_profit_distance':round(favorable,5),'bar_time':bar_time,
            'guard':guard,'tick':tick}
