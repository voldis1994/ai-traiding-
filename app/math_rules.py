"""Exact dimensional relationships from Parbauditas_Sakartotas_Formulas(1).pdf.

The source leaves tick value, the risk budget and actual dealing-rule conversion
instrument-dependent; no currency value is invented. Trading size always follows
user's initial mobile order. These functions expose the source's risk-size check
separately for instruments whose tick value is known.
"""
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

D = lambda value: Decimal(str(value))


def stop_take(entry, direction, tick_size, spread, min_distance=0, kappa=2):
    """PDF: b=max(2delta,2spread), d=max(20delta,4spread,dmin+b).
    nSL=ceil(d/delta), rho=nSL*delta, nTP=ceil(kappa*rho/delta).
    """
    entry, tick, spread, minimum, kappa = map(D, (entry, tick_size, spread, min_distance, kappa))
    if tick <= 0 or spread < 0 or minimum < 0 or kappa <= 0 or direction not in ('BUY', 'SELL'):
        raise ValueError('Invalid stop/take inputs')
    buffer = max(2*tick, 2*spread)
    distance = max(20*tick, 4*spread, minimum+buffer)
    sl_ticks = int((distance/tick).to_integral_value(rounding=ROUND_CEILING))
    rho = tick*sl_ticks
    tp_ticks = int((kappa*rho/tick).to_integral_value(rounding=ROUND_CEILING))
    profit_distance = tick*tp_ticks
    sign = D(1) if direction == 'BUY' else D(-1)
    return {'stop_distance':rho,'profit_distance':profit_distance,
            'stop_level':entry-sign*rho,'profit_level':entry+sign*profit_distance,
            'stop_ticks':sl_ticks,'profit_ticks':tp_ticks,'buffer':buffer}


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
