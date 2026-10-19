"""Leakage-safe, instrument-local multi-horizon OHLC market features.

Only COMPLETE one-minute candles enter model inference and delayed training.
The current forming minute is never a supervised feature. The 1/5/15/60
minute lookbacks are measured in elapsed UTC time, not quote polling ticks.
Outputs are dimensionless, finite and ordered consistently across instruments.
"""
from datetime import datetime, timezone, timedelta
import math
import numpy as np

FEATURE_SCHEMA = 'closed_m1_m5_m15_h1_v3'
FEATURE_NAMES = ('return_1m','return_5m','return_15m','return_60m',
                 'ema_5_15','ema_15_60','volatility_15m',
                 'range_position_60m','candle_body_1m','spread_bps_scaled')
FEATURE_DIM = len(FEATURE_NAMES)
MIN_BARS = 61


def candle_time(value):
    if isinstance(value, datetime):
        dt=value
    elif isinstance(value,str):
        try:
            dt=datetime.fromisoformat(value.strip().replace('Z','+00:00'))
        except ValueError:
            try:dt=datetime.strptime(value.strip(),'%Y/%m/%d %H:%M:%S')
            except ValueError:return None
    else:return None
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0,microsecond=0)


def candle_bucket(value):
    dt=candle_time(value)
    return dt.isoformat() if dt is not None else None


def market_features(rows, *, now_utc=None, spread=0., epic=None):
    """Return a time-aligned feature snapshot or an explicit unavailable reason.

    Stored current-minute quote-derived OHLC is excluded. Bars must cover a
    continuous 60 minute window. Gaps or stale API data cause a safe HOLD.
    """
    current=now_utc if now_utc is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:current=current.replace(tzinfo=timezone.utc)
    cutoff=current.astimezone(timezone.utc).replace(second=0,microsecond=0)
    by_minute={}
    invalid_minutes=set()
    for raw in rows:
        dt=candle_time(raw.get('at'))
        if dt is None or dt>=cutoff:continue
        try:
            o,h,l,c=(float(raw[k]) for k in ('open','high','low','close'))
        except (TypeError,ValueError,KeyError):continue
        if not all(math.isfinite(v) for v in (o,h,l,c)) or min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h):
            invalid_minutes.add(dt);continue
        by_minute[dt]=(o,h,l,c)
    def fail(reason):return {'ready':False,'reason':reason,'epic':epic,'bars':len(by_minute),'features':None}
    if not math.isfinite(float(spread)) or spread<0:return fail('Invalid bid/ask spread')
    if any(dt>=cutoff-timedelta(minutes=3) for dt in invalid_minutes):
        return fail('Invalid recent completed OHLC candle')
    if len(by_minute)<MIN_BARS:return fail(f'Need at least {MIN_BARS} completed MINUTE candles ({len(by_minute)} available)')
    latest=max(by_minute)
    if cutoff-latest>timedelta(minutes=3):return fail('Historical MINUTE candles are stale')
    # Exact minute spacing ensures a gap is not mistaken for a smaller timeframe.
    timeline=[latest-timedelta(minutes=i) for i in range(60,-1,-1)]
    if any(t not in by_minute for t in timeline):return fail('Missing MINUTE candles in 60-minute context; waiting for contiguous history')
    bars=[by_minute[t] for t in timeline]
    closes=np.array([b[3] for b in bars],dtype=float)
    c=float(closes[-1]);high60=max(b[1] for b in bars);low60=min(b[2] for b in bars)
    def ret(minutes):return float((c/closes[-1-minutes]-1.)*1000.)
    def ema(values,span):
        k=2./(span+1.);value=float(values[0])
        for item in values[1:]:value=value+k*(float(item)-value)
        return value
    short=ema(closes,5);middle=ema(closes,15);long=ema(closes,60)
    last=bars[-1]
    returns=np.diff(np.log(closes[-16:]))*1000.
    values=[ret(1),ret(5),ret(15),ret(60),
            (short-middle)/c*1000.,(middle-long)/c*1000.,
            float(np.std(returns)),
            2*(c-low60)/(high60-low60)-1. if high60>low60 else 0.,
            (last[3]-last[0])/max(last[1]-last[2],c*1e-9),
            float(spread)/c*1000.]
    if not np.isfinite(values).all():return fail('Non-finite multi-timeframe features')
    # Bound extreme one-minute gaps to avoid poison gradients; scale consistent.
    values=np.clip(values,-20.,20.).tolist()
    direction=('UP' if ret(15)>0 and ret(60)>0 else
               'DOWN' if ret(15)<0 and ret(60)<0 else 'MIXED')
    return {'ready':True,'reason':'60-minute complete OHLC context','epic':epic,
            'bars':len(by_minute),'bar_time':latest.isoformat(),'close':c,
            'horizons_minutes':[1,5,15,60],
            'returns_bps':{str(k):round(ret(k)*10,3) for k in (1,5,15,60)},
            'trend':direction,'volatility_bps':round(values[6]*10,3),
            'range_position':round(values[7],4),'features':values,
            'feature_schema':FEATURE_SCHEMA}
