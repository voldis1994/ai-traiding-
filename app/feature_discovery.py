"""Autonomous OHLC feature discovery — formulas over completed candles only.

Honest limits (Capital.com REST):
  - No true volume, DOM, or liquidity depth → those are NOT searched.
  - Volatility = OHLC range / return dispersion; cost proxy = bid/ask spread.
  - News / intermarket enter via market_fusion peers + calendar, not invented here.

Discovers candidate formulas, scores them lightly on past completed bars, and
persists proposals for feature_validation to Walk-Forward approve/reject.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from .market_context import candle_time, FEATURE_NAMES as BASE_NAMES

STORE_KEY = 'feature_discovery_state_v1'
# Continuous search cadence: one proposal batch per N completed M1 bars.
BARS_BETWEEN_SEARCH = 30
MAX_PROPOSALS_KEPT = 40


def _shadows(bar, ref_c):
    o,h,l,c=bar;span=max(h-l,ref_c*1e-9)
    return (h-max(o,c))/span,(min(o,c)-l)/span


def _atr(bars, n):
    if len(bars)<n+1:return None
    trs=[]
    for i in range(-n,0):
        o,h,l,c=bars[i];prev=bars[i-1][3]
        trs.append(max(h-l,abs(h-prev),abs(l-prev)))
    return float(np.mean(trs)) if trs else None


def _session_bucket(bar_time):
    """UTC hour → coarse session encoding in [-1,1] (not a hardcode trade gate)."""
    dt=candle_time(bar_time) if not isinstance(bar_time,datetime) else bar_time
    if dt is None:return 0.
    h=dt.astimezone(timezone.utc).hour
    # London ~7–16, NY overlap ~13–17, Asia ~0–7 — soft sine of hour.
    return float(math.sin(2*math.pi*h/24.))


# Candidate library: id → (name, formula docstring, compute(bars, spread, bar_time)->float|None)
CANDIDATE_LIBRARY: dict[str, tuple[str, str, Callable]] = {}


def _reg(fid, name, formula, fn):
    CANDIDATE_LIBRARY[fid]=(name,formula,fn)


def _build_library():
    if CANDIDATE_LIBRARY:return

    def swing_high(bars, spread, bar_time):
        c=bars[-1][3];hi=max(b[1] for b in bars[-20:])
        return float((hi-c)/c*1000.) if c>0 else None
    _reg('swing_dist_high_20','swing_dist_high_20',
         '(max(high[-20:])-close)/close*1000',swing_high)

    def swing_low(bars, spread, bar_time):
        c=bars[-1][3];lo=min(b[2] for b in bars[-20:])
        return float((c-lo)/c*1000.) if c>0 else None
    _reg('swing_dist_low_20','swing_dist_low_20',
         '(close-min(low[-20:]))/close*1000',swing_low)

    def body_streak(bars, spread, bar_time):
        streak=0;sign=0
        for b in reversed(bars[-12:]):
            s=1 if b[3]>b[0] else (-1 if b[3]<b[0] else 0)
            if s==0:break
            if sign==0:sign=s;streak=1
            elif s==sign:streak+=1
            else:break
        return float(sign*streak)
    _reg('body_streak_12','body_streak_12',
         'signed consecutive same-color M1 bodies (≤12)',body_streak)

    def atr_ratio(bars, spread, bar_time):
        a5=_atr(bars,5);a20=_atr(bars,20)
        if a5 is None or a20 is None or a20<=0:return None
        return float(np.clip(a5/a20,-20,20))
    _reg('atr_ratio_5_20','atr_ratio_5_20','ATR(5)/ATR(20) compression/expansion',atr_ratio)

    def session_phase(bars, spread, bar_time):
        return _session_bucket(bar_time)
    _reg('session_phase_utc','session_phase_utc','sin(2π·UTC_hour/24)',session_phase)

    def cost_vs_range(bars, spread, bar_time):
        c=bars[-1][3];hi=max(b[1] for b in bars[-60:]);lo=min(b[2] for b in bars[-60:])
        rng=hi-lo
        if rng<=0 or c<=0:return None
        return float(np.clip(float(spread)/rng,-20,20))
    _reg('spread_vs_range_60','spread_vs_range_60','spread / 60m high-low range',cost_vs_range)

    def shadow_mean_up(bars, spread, bar_time):
        c=bars[-1][3]
        vals=[_shadows(b,c)[0] for b in bars[-8:]]
        return float(np.mean(vals))
    _reg('upper_shadow_mean_8','upper_shadow_mean_8','mean upper wick fraction last 8 M1',shadow_mean_up)

    def shadow_mean_lo(bars, spread, bar_time):
        c=bars[-1][3]
        vals=[_shadows(b,c)[1] for b in bars[-8:]]
        return float(np.mean(vals))
    _reg('lower_shadow_mean_8','lower_shadow_mean_8','mean lower wick fraction last 8 M1',shadow_mean_lo)

    def close_loc_5(bars, spread, bar_time):
        window=bars[-5:];hi=max(b[1] for b in window);lo=min(b[2] for b in window)
        if hi<=lo:return 0.
        return float(2*(bars[-1][3]-lo)/(hi-lo)-1.)
    _reg('close_location_5m','close_location_5m','close in last-5m high-low ∈[-1,1]',close_loc_5)

    def sign_agree(bars, spread, bar_time):
        c=bars[-1][3]
        r1=c/bars[-2][3]-1.;r15=c/bars[-16][3]-1.
        if r1==0 or r15==0:return 0.
        return 1. if (r1>0)==(r15>0) else -1.
    _reg('return_sign_agree_1_15','return_sign_agree_1_15','sign(r1m)==sign(r15m) → ±1',sign_agree)

    def range_compress(bars, spread, bar_time):
        def rng(n):
            w=bars[-n:];return max(b[1] for b in w)-min(b[2] for b in w)
        r5,r20=rng(5),rng(20)
        if r20<=0:return None
        return float(np.clip(r5/r20,-20,20))
    _reg('range_compression_5_20','range_compression_5_20','range(5)/range(20)',range_compress)

    def gap_open(bars, spread, bar_time):
        prev_c=bars[-2][3];o=bars[-1][0]
        if prev_c<=0:return None
        return float(np.clip((o/prev_c-1.)*1000.,-20,20))
    _reg('gap_open_1m','gap_open_1m','(open-prev_close)/prev_close*1000',gap_open)


_build_library()


def compute_candidate(feature_id, bars, *, spread=0., bar_time=None):
    """Evaluate one library formula on a completed contiguous bar window."""
    _build_library()
    spec=CANDIDATE_LIBRARY.get(feature_id)
    if spec is None or not bars or len(bars)<61:return None
    try:
        value=spec[2](bars,float(spread),bar_time)
    except (TypeError,ValueError,ZeroDivisionError,IndexError):
        return None
    if value is None or not math.isfinite(float(value)):return None
    return float(np.clip(float(value),-20.,20.))


def compute_approved(bars, approved_specs, *, spread=0., bar_time=None):
    """Return values aligned to approved registry order (None → fail caller)."""
    out=[]
    for spec in approved_specs:
        fid=spec.get('id') if isinstance(spec,dict) else spec
        val=compute_candidate(fid,bars,spread=spread,bar_time=bar_time)
        if val is None:return None
        out.append(val)
    return out


def list_library(*, exclude_names=None):
    _build_library()
    ban=set(exclude_names or ())|set(BASE_NAMES)
    return [{'id':fid,'name':name,'formula':formula}
            for fid,(name,formula,_) in CANDIDATE_LIBRARY.items()
            if name not in ban]


class FeatureDiscovery:
    """Continuous proposer — stores candidates for validation, does not LIVE-gate."""

    def __init__(self, store):
        self.store=store
        self._load()

    def _load(self):
        raw=self.store.get(STORE_KEY) or {}
        self.bars_seen=int(raw.get('bars_seen') or 0)
        self.proposals=list(raw.get('proposals') or [])
        self.last_search_bar=raw.get('last_search_bar')

    def _save(self):
        self.store.set(STORE_KEY,{
            'bars_seen':self.bars_seen,
            'proposals':self.proposals[-MAX_PROPOSALS_KEPT:],
            'last_search_bar':self.last_search_bar,
            'note':'OHLC/spread only — no volume/DOM from Capital'})

    def on_closed_bar(self, bar_time, bars, *, spread=0., registry=None):
        """Call once per completed M1. Returns new proposals or []."""
        if not bar_time or not bars or len(bars)<61:return []
        if bar_time==self.last_search_bar:return []
        self.bars_seen+=1
        self.last_search_bar=bar_time
        if self.bars_seen % BARS_BETWEEN_SEARCH != 0:
            self._save();return []
        return self.search(bars,spread=spread,registry=registry,bar_time=bar_time)

    def search(self, bars, *, spread=0., registry=None, bar_time=None):
        """Generate candidate feature snapshots from the library (no leakage: bars must be past)."""
        exclude=set(BASE_NAMES)
        if registry is not None:
            exclude |= set(registry.approved_names())|set(registry.approved_ids())
        found=[]
        for spec in list_library(exclude_names=exclude):
            val=compute_candidate(spec['id'],bars,spread=spread,bar_time=bar_time)
            if val is None:continue
            proposal={**spec,'value':val,'bar_time':bar_time,
                      'status':'proposed','source':'ohlc_library'}
            found.append(proposal)
            self.proposals.append(proposal)
        self.proposals=self.proposals[-MAX_PROPOSALS_KEPT:]
        self._save()
        if found:
            self.store.event('discovery','Feature candidates proposed from OHLC library',
                {'count':len(found),'bar_time':bar_time,
                 'ids':[p['id'] for p in found]})
        return found

    def pending_proposals(self):
        return [p for p in self.proposals if p.get('status')=='proposed']
