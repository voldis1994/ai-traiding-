"""Dimensional stop/take helpers.

Adopted lot size + instrument are the operator risk frame. SL distance uses the
broker minimum (+ spread buffer) scaled by a *learned* sl_scale — not a frozen
20-tick invent. TP = kappa * SL with learned kappa.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

D = lambda value: Decimal(str(value))


def _bar_spans(candles):
    spans=[]
    for row in candles or ():
        try:
            h=D(row.get('high'));l=D(row.get('low'))
        except Exception:
            continue
        if h>0 and l>0 and h>=l:
            spans.append(h-l)
    return spans


def _median_decimal(values):
    if not values:return None
    values=sorted(values)
    mid=len(values)//2
    if len(values)%2:
        return values[mid]
    return (values[mid-1]+values[mid])/2


def _knee_index(values):
    """Index of the elbow on a curve — pure geometry, no % threshold.

    Maximises distance from each point to the chord between the first and last
    samples (standard knee/elbow pick on the median-vs-lookback curve).
    """
    n=len(values)
    if n<=1:return 0
    if n==2:return 1
    y0=float(values[0]);y1=float(values[-1])
    best_i=0;best_d=-1.0
    # Line from (0,y0) to (n-1,y1); distance of (i,yi) to that segment.
    dx=float(n-1);dy=y1-y0
    norm=(dx*dx+dy*dy)**0.5
    if norm<=0:
        return n-1
    for i,yi in enumerate(values):
        # | (yi-y0)*dx - i*dy | / norm   (2D cross-product distance)
        d=abs((float(yi)-y0)*dx-float(i)*dy)/norm
        if d>=best_d:
            best_d=d;best_i=i
    return best_i


def adaptive_lookback(candles):
    """How many recent M1 bars to use — analysed from the OHLC series itself.

    Builds median(high−low) for every feasible window length, then picks the
    elbow on that curve. No fixed 20/60 count and no hardcoded 10% stop rule.
    Needs ≥3 bars only because a median is undefined below that.
    """
    rows=list(candles or [])
    if not rows:return 0
    if len(rows)<3:return len(rows)
    series=[]  # (n, median_span)
    for n in range(3,len(rows)+1):
        med=_median_decimal(_bar_spans(rows[-n:]))
        if med is None:continue
        series.append((n,med))
    if not series:return len(rows)
    if len(series)==1:return int(series[0][0])
    knee=_knee_index([m for _,m in series])
    return int(series[knee][0])


def ohlc_range_distance(candles, n=None):
    """Median M1 high−low from broker OHLC — live noise floor.

    If `n` is omitted, lookback is chosen by adaptive_lookback() from the data.
    """
    rows=list(candles or [])
    if not rows:return None
    if n is None:
        n=adaptive_lookback(rows)
    if n<=0:n=len(rows)
    ranges=_bar_spans(rows[-max(1,int(n)):])
    return _median_decimal(ranges)


def _min_ticks(minimum, tick):
    """Ceiling of broker min distance onto the price grid (0 when min is 0)."""
    if minimum <= 0 or tick <= 0:
        return 0
    n = int((minimum / tick).to_integral_value(rounding=ROUND_CEILING))
    return n if n >= 1 else 1


def distances_meet_broker_min(stop_distance, profit_distance, min_distance, eps=None):
    """True iff both SL and TP distances meet broker min after all rounding.

    Audit #9: never send an order with non-compliant SL or TP. When min_distance
    is 0/None, any positive finite distances are accepted.
    """
    try:
        sd = D(stop_distance); pd = D(profit_distance)
        mn = D(0 if min_distance is None else min_distance)
    except Exception:
        return False
    if sd <= 0 or pd <= 0 or mn < 0:
        return False
    if mn == 0:
        return True
    tol = D(str(eps)) if eps is not None else D('0')
    return sd + tol >= mn and pd + tol >= mn


def levels_meet_broker_min(direction, stop_level, profit_level, bid, ask, min_distance, eps=None):
    """True iff protect PUT levels stay ≥ broker min from live bid/ask.

    Missing profit_level is allowed (Take Profit Off); stop_level is required.
    """
    if direction not in ('BUY', 'SELL'):
        return False
    try:
        mn = D(0 if min_distance is None else min_distance)
        b = D(bid); a = D(ask); stop = D(stop_level)
    except Exception:
        return False
    if stop <= 0 or b <= 0 or a <= 0 or mn < 0 or a < b:
        return False
    tol = D(str(eps)) if eps is not None else D('0')
    if direction == 'BUY':
        if b - stop + tol < mn:
            return False
    else:
        if stop - a + tol < mn:
            return False
    if profit_level is None:
        return True
    try:
        tp = D(profit_level)
    except Exception:
        return False
    if tp <= 0:
        return False
    if direction == 'BUY':
        return tp - a + tol >= mn
    return b - tp + tol >= mn


def stop_take(entry, direction, tick_size, spread, min_distance=0, kappa=1, sl_scale=1,
              range_distance=0):
    """Broker-min + optional broker OHLC range, × learned sl_scale; TP = kappa × SL.

    Default κ=1 (1:1), not a frozen 1:2. Learned/explored κ comes from cash P&L.
    Hard floor: dealing min + spread/tick buffer. Soft floor from broker M1 ranges.

    Audit #9: after κ and tick snap, both SL and TP distances are raised to at
    least the broker minimum when κ<1 would otherwise leave TP below min.
    Callers must still refuse to send if distances_meet_broker_min is False.
    """
    entry, tick, spread, minimum, kappa, sl_scale, range_distance = map(
        D, (entry, tick_size, spread, min_distance, kappa, sl_scale, range_distance or 0))
    if tick <= 0 or spread < 0 or minimum < 0 or kappa <= 0 or sl_scale < 1 or range_distance < 0:
        raise ValueError('Invalid stop/take inputs')
    if direction not in ('BUY', 'SELL'):
        raise ValueError('Invalid stop/take inputs')
    buffer = max(2*tick, 2*spread)
    base = minimum + buffer
    if base <= 0:
        base = buffer if buffer > 0 else tick
    if range_distance > base:
        base = range_distance
    distance = base * sl_scale
    sl_ticks = int((distance/tick).to_integral_value(rounding=ROUND_CEILING))
    if sl_ticks < 1:sl_ticks=1
    rho = tick*sl_ticks
    tp_ticks = int((kappa*rho/tick).to_integral_value(rounding=ROUND_CEILING))
    if tp_ticks < 1:tp_ticks=1
    # κ<1 (e.g. MULT_KAPPA_MIN=0.5) can leave TP < broker min after snap —
    # raise both distances onto the min grid so the order is sendable.
    floor_ticks = _min_ticks(minimum, tick)
    raised = False
    if floor_ticks and sl_ticks < floor_ticks:
        sl_ticks = floor_ticks; rho = tick * sl_ticks; raised = True
    if floor_ticks and tp_ticks < floor_ticks:
        tp_ticks = floor_ticks; raised = True
    profit_distance = tick * tp_ticks
    sign = D(1) if direction == 'BUY' else D(-1)
    return {'stop_distance':rho,'profit_distance':profit_distance,
            'stop_level':entry-sign*rho,'profit_level':entry+sign*profit_distance,
            'stop_ticks':sl_ticks,'profit_ticks':tp_ticks,'buffer':buffer,
            'range_distance':float(range_distance),
            'kappa':float(kappa),'sl_scale':float(sl_scale),
            'raised_to_broker_min':raised}


def position_size(max_risk, already_committed_risk, stop_ticks, tick_value, step, minimum, maximum):
    """PDF risk-size formula, separate from adopted size (never overridden)."""
    r,c,n,v,step,minimum,maximum = map(D,(max_risk,already_committed_risk,stop_ticks,tick_value,step,minimum,maximum))
    if r <= 0 or c < 0 or n <= 0 or v <= 0 or step <= 0 or minimum < 0 or maximum < minimum:
        raise ValueError('Invalid risk inputs')
    raw = max(D(0),r-c)/(n*v)
    floor = (min(raw,maximum)/step).to_integral_value(rounding=ROUND_FLOOR)*step
    size = floor if floor >= minimum and floor > 0 else D(0)
    return {'raw':raw,'floored':floor,'size':size,'estimated_risk':n*v*size+(c if size else 0)}


def cash_risk(size, stop_ticks, tick_value):
    """Cash at risk for a fixed size: size × stop_ticks × tick_value (audit #20)."""
    q,n,v = map(D,(size,stop_ticks,tick_value))
    if q <= 0 or n <= 0 or v <= 0:
        raise ValueError('Invalid cash-risk inputs')
    return q*n*v


def tick_value_from_instrument(instrument, tick_size=None):
    """Cash value of one price tick from Capital instrument fields.

    Prefers valueOfOnePip; scales by tick/onePipMeans when the order grid tick
    differs from the pip size. Returns None when the broker did not publish it.
    """
    inst = instrument or {}
    pip_value = None
    for key in ('valueOfOnePip','valueOfOnePoint','valueOfOneTick'):
        try:
            raw = inst.get(key)
            if raw is None:continue
            pip_value = D(raw)
            if pip_value > 0:break
            pip_value = None
        except Exception:
            pip_value = None
    if pip_value is None or pip_value <= 0:
        return None
    pip_means = None
    try:
        pm = inst.get('onePipMeans')
        if pm is not None:
            pip_means = D(pm)
            if pip_means <= 0:pip_means = None
    except Exception:
        pip_means = None
    tick = None
    try:
        if tick_size is not None:
            tick = D(tick_size)
            if tick <= 0:tick = None
    except Exception:
        tick = None
    if tick is not None and pip_means is not None and pip_means != tick:
        return pip_value * (tick / pip_means)
    return pip_value


def pnl(entry, exit_price, direction, size, tick, tick_value, total_costs=0):
    """PDF: signed (Px-Pe)/delta * Vtick * Qpos - C."""
    pe,px,q,dt,v,c=map(D,(entry,exit_price,size,tick,tick_value,total_costs))
    if direction not in ('BUY','SELL') or q < 0 or dt <= 0 or v <= 0 or c < 0:
        raise ValueError('Invalid P&L inputs')
    sign=D(1) if direction=='BUY' else D(-1)
    return sign*(px-pe)/dt*v*q-c
