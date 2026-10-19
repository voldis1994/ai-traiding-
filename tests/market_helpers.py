"""Explicit synthetic market clock, for OFFLINE routing tests only."""
from datetime import datetime,timedelta,timezone
from app.market_context import FEATURE_DIM,FEATURE_SCHEMA


def install_synthetic_context(monkeypatch):
    from app import engine
    counter=[0]
    def fake_context(rows,*,spread=0.,epic=None):
        counter[0]+=1
        stamp=(datetime(2026,10,13,15,0,tzinfo=timezone.utc)+timedelta(minutes=counter[0])).isoformat()
        return {'ready':True,'reason':'synthetic test context','epic':epic,
                'bar_time':stamp,'close':100.+counter[0]*.01,'features':[.1]*FEATURE_DIM,
                'trend':'UP','returns_bps':{'1':1,'5':5,'15':15,'60':60},
                'feature_schema':FEATURE_SCHEMA}
    monkeypatch.setattr(engine,'market_features',fake_context)


def priced_horizon_history(rows, *, base_price=1000., change=2., append_future=True):
    """Deterministic *completed one-minute* bars for policy evidence tests.

    Eight consecutive rows share a target label. The next eight-bar block
    moves by +/- change (or stays flat for HOLD), allowing exact independent
    price-return replay rather than faking a 60% precision pass.
    """
    from datetime import datetime, timezone, timedelta
    origin=datetime(2026,1,5,tzinfo=timezone.utc)
    price=float(base_price)
    for begin in range(0,len(rows),8):
        block=rows[begin:begin+8]
        for offset,r in enumerate(block):
            r.update(price=price,time=(origin+timedelta(minutes=begin+offset)).isoformat())
            r.setdefault('spread_bps',2.)
        # The aliasing regression deliberately tags only minute 1 as SELL.
        labels=[r.get('label') for r in block]
        direction=(1 if 1 in labels else (0 if 0 in labels else 2))
        price+=(change if direction==0 else (-change if direction==1 else 0.))
    if append_future:
        tail_start=len(rows)
        for j in range(8):
            rows.append({'scores':{'BUY':.1,'SELL':.1,'HOLD':.8},'label':None,
                         'price':price,'spread_bps':2.,
                         'time':(origin+timedelta(minutes=tail_start+j)).isoformat()})
    return rows
