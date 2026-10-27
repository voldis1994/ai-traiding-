"""Dimensional stop/take helpers.

Adopted lot size + instrument are the operator risk frame. SL distance uses the
broker minimum (+ spread buffer) scaled by a *learned* sl_scale — not a frozen
20-tick invent. TP = kappa * SL with learned kappa.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

D = lambda value: Decimal(str(value))


def ohlc_range_distance(candles, n=20):
    """Median M1 high−low from broker OHLC — live noise floor, not a magic constant."""
    ranges=[]
    for row in list(candles or [])[-max(1,int(n)):]:
        try:
            h=D(row.get('high'));l=D(row.get('low'))
        except Exception:
            continue
        if h>0 and l>0 and h>=l:
            ranges.append(h-l)
    if not ranges:
        return None
    ranges.sort()
    mid=len(ranges)//2
    if len(ranges)%2:
        return ranges[mid]
    return (ranges[mid-1]+ranges[mid])/2


def stop_take(entry, direction, tick_size, spread, min_distance=0, kappa=2, sl_scale=1,
              range_distance=0):
    """Broker-min + optional broker OHLC range, × learned sl_scale; TP = kappa × SL.

    Hard floor: dealing min + spread/tick buffer. Soft floor from recent broker
    M1 ranges (stops noise scratches when min alone is inside the spread).
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
    profit_distance = tick*tp_ticks
    sign = D(1) if direction == 'BUY' else D(-1)
    return {'stop_distance':rho,'profit_distance':profit_distance,
            'stop_level':entry-sign*rho,'profit_level':entry+sign*profit_distance,
            'stop_ticks':sl_ticks,'profit_ticks':tp_ticks,'buffer':buffer,
            'range_distance':float(range_distance),
            'kappa':float(kappa),'sl_scale':float(sl_scale)}


def position_size(max_risk, already_committed_risk, stop_ticks, tick_value, step, minimum, maximum):
    """PDF risk-size formula, separate from adopted size (never overridden)."""
    r,c,n,v,step,minimum,maximum = map(D,(max_risk,already_committed_risk,stop_ticks,tick_value,step,minimum,maximum))
    if r <= 0 or c < 0 or n <= 0 or v <= 0 or step <= 0 or minimum < 0 or maximum < minimum:
        raise ValueError('Invalid risk inputs')
    raw = max(D(0),r-c)/(n*v)
    floor = (min(raw,maximum)/step).to_integral_value(rounding=ROUND_FLOOR)*step
    size = floor if floor >= minimum and floor > 0 else D(0)
    return {'raw':raw,'floored':floor,'size':size,'estimated_risk':n*v*size+(c if size else 0)}


def pnl(entry, exit_price, direction, size, tick, tick_value, total_costs=0):
    """PDF: signed (Px-Pe)/delta * Vtick * Qpos - C."""
    pe,px,q,dt,v,c=map(D,(entry,exit_price,size,tick,tick_value,total_costs))
    if direction not in ('BUY','SELL') or q < 0 or dt <= 0 or v <= 0 or c < 0:
        raise ValueError('Invalid P&L inputs')
    sign=D(1) if direction=='BUY' else D(-1)
    return sign*(px-pe)/dt*v*q-c
