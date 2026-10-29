"""Autonomous OHLC feature discovery — formulas over completed candles only.

Honest limits (Capital.com REST):
  - No true volume, DOM, or liquidity depth → those are NOT searched.
  - Volatility = OHLC range / return dispersion; cost proxy = bid/ask spread.
  - News / intermarket enter via market_fusion peers + calendar, not invented here.

Beyond a seed library, candidates are *generated* combinatorially from OHLC
primitives (horizons × operators × pairwise diffs). That is not free-form
symbolic AI inventing arbitrary maths — it is an open search over a defined
OHLC algebra, validated independently before LIVE use.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from .market_context import candle_time, FEATURE_NAMES as BASE_NAMES

STORE_KEY = 'feature_discovery_state_v1'
# Seeds only — runtime cadence/horizons come from self_params (robot-owned).
_SEED_BARS_BETWEEN_SEARCH = 30
_SEED_MAX_PROPOSALS_KEPT = 80
_SEED_GEN_HORIZONS = (3, 5, 8, 13, 21, 34)


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
    dt=candle_time(bar_time) if not isinstance(bar_time,datetime) else bar_time
    if dt is None:return 0.
    h=dt.astimezone(timezone.utc).hour
    return float(math.sin(2*math.pi*h/24.))


# Candidate library: id → (name, formula docstring, compute(bars, spread, bar_time)->float|None)
CANDIDATE_LIBRARY: dict[str, tuple[str, str, Callable]] = {}
_GENERATED = False


def _reg(fid, name, formula, fn):
    CANDIDATE_LIBRARY[fid]=(name,formula,fn)


def _seed_library():
    """Hand-authored seeds (readable defaults). Combinatorial gens added separately."""
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
        return float(np.mean([_shadows(b,c)[0] for b in bars[-8:]]))
    _reg('upper_shadow_mean_8','upper_shadow_mean_8','mean upper wick fraction last 8 M1',shadow_mean_up)

    def shadow_mean_lo(bars, spread, bar_time):
        c=bars[-1][3]
        return float(np.mean([_shadows(b,c)[1] for b in bars[-8:]]))
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


def _make_ret(h):
    def fn(bars, spread, bar_time, _h=h):
        if len(bars)<=_h:return None
        c=bars[-1][3];p=bars[-1-_h][3]
        if p<=0:return None
        return float(np.clip((c/p-1.)*1000.,-20,20))
    return fn


def _make_range_pos(h):
    def fn(bars, spread, bar_time, _h=h):
        w=bars[-_h:];hi=max(b[1] for b in w);lo=min(b[2] for b in w);c=bars[-1][3]
        if hi<=lo:return 0.
        return float(np.clip(2*(c-lo)/(hi-lo)-1.,-20,20))
    return fn


def _make_atr_ratio(short_h, long_h):
    def fn(bars, spread, bar_time, _s=short_h, _l=long_h):
        a=_atr(bars,_s);b=_atr(bars,_l)
        if a is None or b is None or b<=0:return None
        return float(np.clip(a/b,-20,20))
    return fn


def _make_wick_imb(h):
    def fn(bars, spread, bar_time, _h=h):
        c=bars[-1][3]
        vals=[_shadows(b,c)[0]-_shadows(b,c)[1] for b in bars[-_h:]]
        return float(np.clip(np.mean(vals),-20,20))
    return fn


def _make_body_mean(h):
    def fn(bars, spread, bar_time, _h=h):
        vals=[]
        for b in bars[-_h:]:
            span=max(b[1]-b[2],1e-12)
            vals.append((b[3]-b[0])/span)
        return float(np.clip(np.mean(vals),-20,20))
    return fn


def _make_spread_vs_atr(h):
    def fn(bars, spread, bar_time, _h=h):
        a=_atr(bars,_h)
        if a is None or a<=0:return None
        return float(np.clip(float(spread)/a,-20,20))
    return fn


def _make_ret_diff(h1, h2):
    def fn(bars, spread, bar_time, _a=h1, _b=h2):
        if len(bars)<=max(_a,_b):return None
        c=bars[-1][3]
        r1=(c/bars[-1-_a][3]-1.)*1000.
        r2=(c/bars[-1-_b][3]-1.)*1000.
        return float(np.clip(r1-r2,-20,20))
    return fn


def _combinatorial_ids(horizons=None):
    """Ids that belong to one client's gen_horizons — not another client's leftovers."""
    hs=tuple(horizons) if horizons else _SEED_GEN_HORIZONS
    ids=set()
    for h in hs:
        ids.update({
            f'gen_ret_{h}',f'gen_range_pos_{h}',f'gen_wick_imb_{h}',
            f'gen_body_mean_{h}',f'gen_spread_atr_{h}',
        })
    pairs=[(hs[i],hs[j]) for i in range(len(hs)) for j in range(i+1,len(hs)) if hs[j]>=hs[i]*2][:8]
    for s,l in pairs or ((3,13),(5,21)):
        ids.add(f'gen_atr_ratio_{s}_{l}')
    for a,b in pairs or ((3,8),(5,13)):
        ids.add(f'gen_ret_diff_{a}_{b}')
    return ids


def _generate_combinatorial(horizons=None):
    """Open-ended OHLC algebra — horizons come from self_params when provided."""
    global _GENERATED
    hs=tuple(horizons) if horizons else _SEED_GEN_HORIZONS
    # Always (re)register for current horizon set; ids are stable per horizon.
    for h in hs:
        _reg(f'gen_ret_{h}',f'gen_ret_{h}',f'(close/close[-{h}]-1)*1000',_make_ret(h))
        _reg(f'gen_range_pos_{h}',f'gen_range_pos_{h}',
             f'close location in last-{h} high-low',_make_range_pos(h))
        _reg(f'gen_wick_imb_{h}',f'gen_wick_imb_{h}',
             f'mean (upper-lower) wick over {h}',_make_wick_imb(h))
        _reg(f'gen_body_mean_{h}',f'gen_body_mean_{h}',
             f'mean body/range over {h}',_make_body_mean(h))
        _reg(f'gen_spread_atr_{h}',f'gen_spread_atr_{h}',
             f'spread/ATR({h})',_make_spread_vs_atr(h))
    pairs=[(hs[i],hs[j]) for i in range(len(hs)) for j in range(i+1,len(hs)) if hs[j]>=hs[i]*2][:8]
    for s,l in pairs or ((3,13),(5,21)):
        _reg(f'gen_atr_ratio_{s}_{l}',f'gen_atr_ratio_{s}_{l}',
             f'ATR({s})/ATR({l})',_make_atr_ratio(s,l))
    for a,b in pairs or ((3,8),(5,13)):
        _reg(f'gen_ret_diff_{a}_{b}',f'gen_ret_diff_{a}_{b}',
             f'ret({a})-ret({b})',_make_ret_diff(a,b))
    _GENERATED=True


def _build_library(horizons=None):
    if not CANDIDATE_LIBRARY:
        _seed_library()
    _generate_combinatorial(horizons=horizons)


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


REJECTED_KEY = 'feature_discovery_rejected_v1'
# After reject, skip that id for this many search rounds (rotated, not stuck).
REJECT_COOLDOWN_ROUNDS = 8


class FeatureDiscovery:
    """Continuous proposer — rotates library; skips recently rejected ids."""

    def __init__(self, store):
        self.store=store
        self._load()

    def _load(self):
        raw=self.store.get(STORE_KEY) or {}
        self.bars_seen=int(raw.get('bars_seen') or 0)
        self.proposals=list(raw.get('proposals') or [])
        self.last_search_bar=raw.get('last_search_bar')
        self.cursor=int(raw.get('cursor') or 0)
        self.search_rounds=int(raw.get('search_rounds') or 0)
        rej=self.store.get(REJECTED_KEY) or {}
        self.rejected=dict(rej) if isinstance(rej,dict) else {}

    def _save(self):
        keep=int(self._params().get('max_proposals_kept',_SEED_MAX_PROPOSALS_KEPT))
        self.store.set(STORE_KEY,{
            'bars_seen':self.bars_seen,
            'proposals':self.proposals[-keep:],
            'last_search_bar':self.last_search_bar,
            'cursor':self.cursor,
            'search_rounds':self.search_rounds,
            'library_size':len(CANDIDATE_LIBRARY),
            'note':'OHLC/spread combinatorial algebra — no volume/DOM from Capital'})
        self.store.set(REJECTED_KEY,self.rejected)

    def _params(self):
        from .self_params import load as load_params
        return load_params(self.store)

    def mark_rejected(self, feature_id, reason=''):
        """Cooldown out a rejected candidate so the next search tries others first."""
        self.rejected[str(feature_id)]={
            'round':self.search_rounds,'reason':str(reason)[:160]}
        self._save()

    def mark_approved(self, feature_id):
        self.rejected.pop(str(feature_id),None)
        self._save()

    def _is_cooling(self, feature_id):
        meta=self.rejected.get(str(feature_id))
        if not meta:return False
        age=self.search_rounds-int(meta.get('round') or 0)
        return age<REJECT_COOLDOWN_ROUNDS

    def on_closed_bar(self, bar_time, bars, *, spread=0., registry=None):
        if not bar_time or not bars or len(bars)<61:return []
        if bar_time==self.last_search_bar:return []
        self.bars_seen+=1
        self.last_search_bar=bar_time
        p=self._params()
        every=max(1,int(p['bars_between_search']))
        if self.bars_seen % every != 0:
            self._save();return []
        return self.search(bars,spread=spread,registry=registry,bar_time=bar_time)

    def search(self, bars, *, spread=0., registry=None, bar_time=None, limit=None):
        """Rotate through library; skip cooling-off rejects so other formulas get tried."""
        p=self._params()
        horizons=p.get('gen_horizons')
        _build_library(horizons=horizons)
        limit=int(p['search_limit'] if limit is None else limit)
        keep=int(p['max_proposals_kept'])
        exclude=set(BASE_NAMES)
        if registry is not None:
            exclude |= set(registry.approved_names())|set(registry.approved_ids())
        # Scope gen_* proposals to THIS store's horizons — other clients may have
        # registered extra gens into the process-global library.
        mine=_combinatorial_ids(horizons)
        library=[spec for spec in list_library(exclude_names=exclude)
                 if (not str(spec['id']).startswith('gen_')) or spec['id'] in mine]
        if not library:return []
        self.search_rounds+=1
        n=len(library)
        start=self.cursor % n
        ordered=library[start:]+library[:start]
        found=[];scanned=0
        for spec in ordered:
            scanned+=1
            if self._is_cooling(spec['id']):
                continue
            val=compute_candidate(spec['id'],bars,spread=spread,bar_time=bar_time)
            if val is None:continue
            proposal={**spec,'value':val,'bar_time':bar_time,
                      'status':'proposed','source':'ohlc_algebra'}
            found.append(proposal)
            self.proposals.append(proposal)
            if len(found)>=limit:break
        # Advance cursor past what we scanned so next round starts elsewhere.
        self.cursor=(start+max(1,scanned))%n
        # If everything was cooling, clear oldest rejects and retry once.
        if not found and self.rejected:
            oldest=sorted(self.rejected.items(),key=lambda kv:int(kv[1].get('round') or 0))
            for fid,_ in oldest[:max(1,len(oldest)//3)]:
                self.rejected.pop(fid,None)
            for spec in ordered:
                val=compute_candidate(spec['id'],bars,spread=spread,bar_time=bar_time)
                if val is None:continue
                found.append({**spec,'value':val,'bar_time':bar_time,
                              'status':'proposed','source':'ohlc_algebra_retry'})
                if len(found)>=limit:break
        self.proposals=self.proposals[-keep:]
        self._save()
        if found:
            self.store.event('discovery','Feature candidates proposed (rotated library)',
                {'count':len(found),'library':len(library),'bar_time':bar_time,
                 'ids':[x['id'] for x in found],'cursor':self.cursor,
                 'cooling':len([1 for k in self.rejected if self._is_cooling(k)]),
                 'search_limit':limit})
        return found

    def pending_proposals(self):
        return [p for p in self.proposals if p.get('status')=='proposed']
