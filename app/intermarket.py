"""Read-only, broker-sourced, time-aligned cross-market risk confirmation.

No broker order methods are called here. Correlation is descriptive, not
causation or a learned profit forecast. Weak evidence never forces BUY/SELL.
"""
from datetime import datetime,timezone,timedelta
import math
import time
import numpy as np
from .instrument_profile import instrument_profile
from .market_context import candle_time

REFERENCE_TERMS = {
    'FOREX': ('US Dollar Index','US 500','Gold','Brent Crude'),
    'METALS': ('Silver','US Dollar Index','US 500','Brent Crude'),
    'COMMODITIES': ('Brent Crude','US Dollar Index','US 500','Gold'),
    'INDICES': ('US 500','US Dollar Index','Gold','Bitcoin'),
    'CRYPTO': ('Bitcoin','Ethereum','US 500','US Dollar Index'),
    'EQUITIES': ('US 500','US Dollar Index','Gold','Bitcoin'),
    'OTHER': ('US 500','US Dollar Index','Gold','Bitcoin'),
}


def _identity(s):
    return ''.join(c for c in str(s or '').upper() if c.isalnum())


def pick_market(result, term, main_epic):
    """Only use exact broker name/epic matches, never an unrelated search hit."""
    markets = result.get('markets',[]) if isinstance(result,dict) else []
    target = _identity(term)
    for item in markets:
        if not isinstance(item,dict):continue
        epic=item.get('epic')
        if not epic or epic==main_epic:continue
        name=_identity(item.get('instrumentName') or item.get('name'))
        if name == target or _identity(epic)==target:
            return {'epic':epic,'name':item.get('instrumentName') or item.get('name') or term}
    return None


def _closes(payload, current):
    """Never use a forming minute, synthetic quotes, duplicated or invalid bars."""
    cutoff=current.astimezone(timezone.utc).replace(second=0,microsecond=0)
    bars={}
    for p in payload.get('prices',[]):
        if not isinstance(p,dict):continue
        stamp=candle_time(p.get('snapshotTimeUTC') or p.get('snapshotTime'))
        if stamp is None or stamp>=cutoff:continue
        pair=p.get('closePrice') or {}
        try:
            bid=float(pair.get('bid'));ask=float(pair.get('ask'))
        except (ValueError,TypeError,AttributeError):continue
        if not all(math.isfinite(x) for x in (bid,ask)) or bid<=0 or ask<bid:continue
        bars[stamp]=(bid+ask)/2
    return bars


def align_and_assess(main_rows, peer_prices, *, current=None):
    """Compare paired completed one-minute log returns on an identical UTC grid."""
    current=current or datetime.now(timezone.utc)
    cutoff=current.astimezone(timezone.utc).replace(second=0,microsecond=0)
    main={}
    for p in main_rows:
        stamp=candle_time(p.get('at'))
        if stamp is None or stamp>=cutoff:continue
        try:close=float(p['close'])
        except (ValueError,TypeError,KeyError):continue
        if math.isfinite(close) and close>0:main[stamp]=close
    peer=_closes(peer_prices,current)
    if not main:
        return {'ready':False,'reason':'No completed primary-market history'}
    latest=max(main)
    if latest<cutoff-timedelta(minutes=3):
        return {'ready':False,'reason':'Primary-market candles are stale'}
    timeline=[latest-timedelta(minutes=i) for i in range(60,-1,-1)]
    # A contiguous and correctly aligned 60m comparison avoids gap-bias.
    if not all(t in main and t in peer for t in timeline):
        return {'ready':False,'reason':'Missing aligned complete 61-minute peer history'}
    a=np.log(np.array([main[t] for t in timeline]));b=np.log(np.array([peer[t] for t in timeline]))
    ra=np.diff(a);rb=np.diff(b)
    if not (np.isfinite(ra).all() and np.isfinite(rb).all()):
        return {'ready':False,'reason':'Non-finite price returns'}
    if np.std(ra)<1e-9 or np.std(rb)<1e-9:
        return {'ready':False,'reason':'Insufficient return variance'}
    # Calibrate sign using only earlier bars, then check stability on later bars.
    train=float(np.corrcoef(ra[:40],rb[:40])[0,1]);holdout=float(np.corrcoef(ra[40:55],rb[40:55])[0,1])
    if not all(math.isfinite(v) for v in (train,holdout)):
        return {'ready':False,'reason':'Invalid correlation'}
    stable=(abs(train)>=0.3 and abs(holdout)>=0.2 and train*holdout>0)
    # Last five *completed* peer candles, held out from the correlation fit.
    delta=float((b[-1]-b[-6])*10000)
    delta1=float((b[-1]-b[-2])*10000)
    delta15=float((b[-1]-b[-16])*10000)
    # Direction is for diagnostic/veto only; no automatic approval of entries.
    sign=(1 if train>0 else -1)
    direction=(1 if sign*delta>0.8 else -1 if sign*delta<-.8 else 0) if stable else 0
    return {'ready':True,'correlation':round(train,4),'holdout_correlation':round(holdout,4),
            'stable':stable,'peer_change_5m_bps':round(delta,3),
            'peer_return_1m_bps':round(delta1,3),'peer_return_5m_bps':round(delta,3),
            'peer_return_15m_bps':round(delta15,3),'direction':direction,
            'last_completed_bar':latest.isoformat(),'samples':len(ra)}


class IntermarketMonitor:
    def __init__(self):
        self.epic=None;self.peers=[];self.last_discovery=0.;self.last_refresh=0.
        self.state={'status':'WAITING','entry_allowed':False,'reason':'Waiting for broker reference markets','peers':[]}

    async def refresh(self,broker,epic,instrument,main_rows,*,current=None,force=False):
        current=current or datetime.now(timezone.utc)
        moment=time.monotonic()
        if epic!=self.epic:
            self.epic=epic;self.peers=[];self.last_discovery=0.;self.last_refresh=0.
            self.state={'status':'WAITING','entry_allowed':False,'reason':'New instrument: discovering reference markets','peers':[]}
        latest=max((t for t in (candle_time(p.get('at')) for p in main_rows) if t is not None), default=None)
        # Cached observations must belong to this EXACT primary completed bar.
        if (not force and self.last_refresh and moment-self.last_refresh<75
                and latest is not None and
                self.state.get('bar_time')==latest.isoformat()):
            return self.state
        self.last_refresh=moment
        profile=instrument_profile(epic,instrument)
        if not self.peers or not self.last_discovery or moment-self.last_discovery>1800:
            self.last_discovery=moment
            found=[]
            if not hasattr(broker,'search_markets') or not hasattr(broker,'prices'):
                self.state={'status':'UNAVAILABLE','entry_allowed':False,'reason':'Broker reference market API unavailable','peers':[],'profile':profile}
                return self.state
            for term in REFERENCE_TERMS[profile['kind']]:
                try:
                    market=pick_market(await broker.search_markets(term),term,epic)
                except Exception:market=None
                if market and market['epic'] not in {p['epic'] for p in found}:
                    found.append(market)
                if len(found)>=3:break
            self.peers=found
        observations=[]
        for p in self.peers:
            try:
                prices=await broker.prices(p['epic'],resolution='MINUTE',maximum=90)
                reading=align_and_assess(main_rows,prices,current=current)
            except Exception as exc:
                reading={'ready':False,'reason':'Broker peer price unavailable: '+type(exc).__name__}
            observations.append({**p,**reading})
        ready=[p for p in observations if p.get('ready')]
        if not ready:
            self.state={'status':'UNAVAILABLE','entry_allowed':False,'reason':'No fresh aligned Capital.com peer history; wait for data','peers':observations,'profile':profile,
                        'bar_time':latest.isoformat() if latest else None}
        else:
            self.state={'status':'READY','entry_allowed':True,'reason':'Broker reference prices aligned; cross-market veto enabled',
                        'peers':observations,'profile':profile,
                        'bar_time':latest.isoformat() if latest else None,
                        'reliable_peers':sum(bool(p.get('stable')) for p in ready)}
        return self.state

    def assess(self,action):
        state=self.state
        if state.get('status')!='READY':return {'entry_allowed':False,'status':state.get('status'),'reason':state.get('reason','Cross-market data unavailable')}
        if action not in ('BUY','SELL'):
            return {'entry_allowed':True,'status':'NEUTRAL','reason':'No directional proposal to cross-check'}
        direction=1 if action=='BUY' else -1
        votes=[p['direction'] for p in state['peers'] if p.get('ready') and p.get('stable') and p.get('direction')]
        # Independent *peer price series*, not independent causal evidence.
        if len(votes)>=2 and all(v==-direction for v in votes):
            return {'entry_allowed':False,'status':'CONTRADICTION',
                    'reason':'At least two aligned, stable peer market movements oppose '+action}
        return {'entry_allowed':True,'status':'NO_CONFIRMED_CONTRADICTION',
                'reason':'No multi-peer contradiction confirmed (not a trade approval)'}
