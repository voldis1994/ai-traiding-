"""Independent shadow paper trading — own position + cash-comparable PnL.

Never remaps LIVE broker £ onto the shadow vote. Each closed M1 bar the shadow
neural core votes BUY/SELL/HOLD; this journal opens/closes a virtual position
using the *same* execution rules as LIVE: spread fill, optional slip, fill
delay, SL/TP brackets, and trailing stop. Opposite-signal flip exits are
forbidden — LIVE closes only via SL/TP/trail.

Price-unit moves are converted to account-currency cash via a scale estimated
from recent LIVE broker trades (broker_£ / signed price move). Without that
scale, cash fields stay None — autonomy must not compare price units to £.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .trade_costs import fill_price as _shared_fill_price, half_spread

STORE_KEY = 'shadow_paper_journal_v1'
STATE_KEY = 'shadow_paper_state_v1'
_SEED_MAX_ROWS = 400

# Match LIVE defaults when engine does not pass explicit values.
DEFAULT_FILL_DELAY_BARS = 0
DEFAULT_SLIP_BPS = 0.


def _finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None


def _fill_price(direction, mid, spread, *, slip_bps=0.):
    """Conservative fill via shared trade_costs (BUY ask / SELL bid + slip)."""
    return float(_shared_fill_price(direction,mid,spread,slip_bps=slip_bps))


def _close_price_pnl(direction, entry, exit_px):
    sign=1 if direction=='BUY' else -1
    return float(sign*(exit_px-entry))


def cash_per_price_unit(live_rows, *, min_samples=5):
    """Median broker-£ per unit price move from LIVE closed trades.

    Uses actual_pnl / signed(exit-entry). Captures size + FX into account
    currency without inventing tick_value. Returns None if under-powered.
    """
    ratios=[]
    for r in live_rows or []:
        pnl=_finite(r.get('actual_pnl'))
        entry=_finite(r.get('entry'));exit_px=_finite(r.get('exit'))
        direction=r.get('direction')
        if pnl is None or entry is None or exit_px is None:continue
        if direction not in ('BUY','SELL'):continue
        sign=1 if direction=='BUY' else -1
        move=sign*(exit_px-entry)
        if abs(move)<1e-12:continue
        ratios.append(pnl/move)
    if len(ratios)<int(min_samples):return None
    return float(np.median(ratios))


def _bar_mid_hl(bar, *, default_spread=0.):
    """Normalize a path bar dict/tuple to (mid, high, low, spread)."""
    if isinstance(bar,(list,tuple)):
        if len(bar)>=4:
            o,h,l,c=bar[0],bar[1],bar[2],bar[3]
            sp=bar[4] if len(bar)>4 else default_spread
            mid=_finite(c) if _finite(c) is not None else _finite(o)
            return mid,_finite(h),_finite(l),_finite(sp) or 0.
        if len(bar)==3:
            mid,h,l=bar
            return _finite(mid),_finite(h),_finite(l),float(default_spread or 0)
    mid=_finite(bar.get('mid') if isinstance(bar,dict) else None)
    if mid is None and isinstance(bar,dict):
        mid=_finite(bar.get('close'))
    if mid is None and isinstance(bar,dict):
        mid=_finite(bar.get('price'))
    high=_finite(bar.get('high')) if isinstance(bar,dict) else None
    low=_finite(bar.get('low')) if isinstance(bar,dict) else None
    sp=_finite(bar.get('spread')) if isinstance(bar,dict) else None
    if sp is None:sp=float(default_spread or 0)
    if high is None:high=mid
    if low is None:low=mid
    return mid,high,low,float(sp)


def simulate_action_on_path(action, bars, *, spread=0., stop_distance=None,
                            profit_distance=None, tick=None, min_distance=None,
                            slip_bps=0., trail=True):
    """Replay BUY/SELL/HOLD on a full OHLC path with LIVE SL/TP/trail/costs.

    Same execution rules as ShadowPaperJournal / LIVE: adverse fills, SL before
    TP on the same bar, optional trail, never flip-exit on opposite signal.
    HOLD → pnl 0. Price-unit pnl from fill-to-fill (spread embedded once).

    bars: chronological mids after (and including) entry — dicts with
    mid|close, high, low, optional per-bar spread; or OHLC tuples.
    """
    if action=='HOLD':
        return {'pnl':0.,'reason':'hold','entry':None,'exit':None,'bars_held':0,
                'path':True}
    if action not in ('BUY','SELL') or not bars:
        return None
    slip=float(slip_bps or 0)
    mid0,hi0,lo0,sp0=_bar_mid_hl(bars[0],default_spread=spread)
    if mid0 is None or mid0<=0:return None
    sp=float(spread if spread is not None else sp0) or float(sp0 or 0)
    entry=_fill_price(action,mid0,sp,slip_bps=slip)
    tick_v=_finite(tick)
    if tick_v is None or tick_v<=0:
        tick_v=max(half_spread(sp),1e-4)
    min_d=_finite(min_distance)
    if min_d is None or min_d<0:min_d=tick_v
    sd=_finite(stop_distance)
    pd=_finite(profit_distance)
    if sd is None or sd<=0:
        sd=max(2*tick_v,2*abs(sp))
    if pd is None or pd<=0:
        pd=sd
    sign=1 if action=='BUY' else -1
    stop=entry-sign*float(sd)
    profit=entry+sign*float(pd)
    original_risk=float(sd)
    trailed=False
    for i,bar in enumerate(bars):
        mid,hi,lo,sp_i=_bar_mid_hl(bar,default_spread=sp)
        if mid is None:continue
        if hi is None:hi=mid
        if lo is None:lo=mid
        hi=max(hi,mid);lo=min(lo,mid)
        sp_bar=float(sp_i if sp_i is not None else sp)
        if trail:
            new_stop=paper_trail_stop(
                action,entry,stop,profit,high=hi,low=lo,mid=mid,spread=sp_bar,
                original_risk=original_risk,tick=tick_v,min_distance=min_d)
            if new_stop is not None:
                stop=float(new_stop);trailed=True
        hit=hit_sl_tp(action,high=hi,low=lo,stop=stop,profit=profit,
                      mid=mid,spread=sp_bar,slip_bps=slip)
        if hit is not None:
            exit_px,reason=hit
            if reason=='sl' and trailed:reason='trail'
            pnl=_close_price_pnl(action,entry,exit_px)
            return {'pnl':float(pnl),'reason':reason,'entry':float(entry),
                    'exit':float(exit_px),'bars_held':i+1,'path':True,
                    'stop':float(stop),'profit':float(profit)}
    mid_l,_,_,sp_l=_bar_mid_hl(bars[-1],default_spread=sp)
    if mid_l is None:return None
    close_dir='SELL' if action=='BUY' else 'BUY'
    exit_px=_fill_price(close_dir,mid_l,sp_l if sp_l is not None else sp,slip_bps=slip)
    pnl=_close_price_pnl(action,entry,exit_px)
    return {'pnl':float(pnl),'reason':'path_end','entry':float(entry),
            'exit':float(exit_px),'bars_held':len(bars),'path':True,
            'stop':float(stop),'profit':float(profit)}


def paper_trail_stop(direction, entry, stop, profit, *, high, low, mid, spread,
                     original_risk, tick, min_distance):
    """Mirror LIVE broker trail using bar extremes (bid/ask ≈ mid ± half-spread).

    Same BE / post-BE gap idea as `propose_broker_trail` — never flips on signal.
    Returns new stop or None when no improvement.
    """
    direction=direction if direction in ('BUY','SELL') else None
    if direction is None:return None
    entry=_finite(entry);stop=_finite(stop);tick=_finite(tick)
    high=_finite(high);low=_finite(low);mid=_finite(mid)
    minimum=_finite(min_distance);gap=_finite(original_risk)
    sp=_finite(spread) or 0.
    if None in (entry,stop,tick,high,low,mid) or tick<=0:return None
    if minimum is None or minimum<0:minimum=tick
    if gap is None or gap<=0:gap=max(minimum,abs(entry-stop))
    open_gap=max(float(gap),float(minimum))
    half=half_spread(sp)
    bid=mid-half;ask=mid+half
    sign=1 if direction=='BUY' else -1
    tip=high if direction=='BUY' else low
    if direction=='BUY':
        tip=tip-half
    else:
        tip=tip+half
    be_locked=sign*(stop-entry)>=-1e-12
    trail_gap=open_gap if not be_locked else max(minimum,min(open_gap,max(2.0*minimum,0.35*open_gap)))
    need=open_gap if not be_locked else trail_gap
    tip_fav=sign*(tip-entry)
    if tip_fav<need:return None
    profit=_finite(profit)
    if direction=='BUY':
        tip=max(tip,bid)
        if tip<=entry:return None
        desired=min(tip-trail_gap,bid-minimum)
        new_stop=math.floor(desired/tick+1e-12)*tick
        if new_stop<=stop or new_stop>=tip:return None
        if profit is not None and profit<=new_stop:return None
        if new_stop<stop+tick-1e-12:return None
        return float(new_stop)
    tip=min(tip,ask)
    if tip>=entry:return None
    desired=max(tip+trail_gap,ask+minimum)
    new_stop=math.ceil(desired/tick-1e-12)*tick
    if new_stop>=stop or new_stop<=tip:return None
    if profit is not None and profit>=new_stop:return None
    if new_stop>stop-tick+1e-12:return None
    return float(new_stop)


def hit_sl_tp(direction, *, high, low, stop, profit, mid, spread, slip_bps=0.):
    """Return (exit_px, reason) if SL or TP is touched this bar; SL wins if both."""
    high=_finite(high);low=_finite(low)
    stop=_finite(stop);profit=_finite(profit)
    if high is None or low is None:return None
    half=half_spread(spread)
    if direction=='BUY':
        if stop is not None and low-half<=stop:
            return float(stop),'sl'
        if profit is not None and high+half>=profit:
            exit_px=_fill_price('SELL',profit,0.,slip_bps=slip_bps)
            return float(exit_px),'tp'
    elif direction=='SELL':
        if stop is not None and high+half>=stop:
            return float(stop),'sl'
        if profit is not None and low-half<=profit:
            exit_px=_fill_price('BUY',profit,0.,slip_bps=slip_bps)
            return float(exit_px),'tp'
    return None


class ShadowPaperJournal:
    """Paper ledger for the shadow NeuralCore — never places LIVE orders."""

    def __init__(self, store):
        self.store=store
        self.history=list(self.store.get(STORE_KEY) or [])
        st=self.store.get(STATE_KEY) or {}
        self.position=st.get('position')
        self.pending=st.get('pending')
        self.last_bar=st.get('last_bar')
        self.cash_scale=st.get('cash_scale')
        self.bars_seen=int(st.get('bars_seen') or 0)

    def _persist(self):
        from .self_params import get as sp_get
        keep=int(sp_get(self.store,'profit_history_rows',_SEED_MAX_ROWS))
        self.store.set(STORE_KEY,self.history[-keep:])
        self.store.set(STATE_KEY,{
            'position':self.position,'pending':self.pending,
            'last_bar':self.last_bar,'cash_scale':self.cash_scale,
            'bars_seen':self.bars_seen})

    def update_cash_scale(self, live_rows, *, min_samples=5):
        """Refresh £/price-unit from LIVE broker history when possible."""
        scale=cash_per_price_unit(live_rows,min_samples=min_samples)
        if scale is not None and math.isfinite(scale):
            self.cash_scale=float(scale)
            self._persist()
        return self.cash_scale

    def _close_row(self, pos, exit_px, *, bar_time, spread, reason):
        direction=pos['direction']
        price_pnl=_close_price_pnl(direction,float(pos['entry']),float(exit_px))
        scale=_finite(self.cash_scale)
        cash=float(price_pnl*scale) if scale is not None else None
        closed={
            'at':datetime.now(timezone.utc).isoformat(),
            'bar_time':bar_time,
            'direction':direction,
            'entry':float(pos['entry']),'exit':float(exit_px),
            'price_pnl':price_pnl,
            'paper_pnl':cash,
            'cash_scale':scale,
            'unit':'account_currency' if cash is not None else 'price',
            'spread':float(spread or 0),
            'exit_reason':reason,  # sl | tp | trail — never 'flip'
            'source':'shadow_paper',
            'model_version':pos.get('model_version'),
            'stop_level':pos.get('stop'),'profit_level':pos.get('profit'),
            'shadow_transition':pos.get('shadow_transition'),
        }
        self.history.append(closed)
        self.position=None
        return closed

    def _open_position(self, action, *, mid, spread, slip_bps, bar_time,
                       stop_distance, profit_distance, tick, min_distance,
                       model_version, shadow_transition, kappa, sl_scale):
        entry=_fill_price(action,mid,spread,slip_bps=slip_bps)
        tick=float(tick) if _finite(tick) and float(tick)>0 else max(abs(float(spread or 0))/2.,1e-4)
        sd=_finite(stop_distance)
        pd=_finite(profit_distance)
        if sd is None or sd<=0:
            sd=max(2*tick,2*abs(float(spread or 0)))
        if pd is None or pd<=0:
            pd=sd
        sign=1 if action=='BUY' else -1
        stop=entry-sign*float(sd)
        profit=entry+sign*float(pd)
        self.position={
            'direction':action,'entry':float(entry),
            'stop':float(stop),'profit':float(profit),
            'original_risk':float(sd),
            'tick':float(tick),
            'min_distance':float(min_distance) if _finite(min_distance) is not None else float(tick),
            'opened_at':datetime.now(timezone.utc).isoformat(),
            'bar_time':bar_time,
            'model_version':model_version,
            'kappa':kappa,'sl_scale':sl_scale,
            'shadow_transition':shadow_transition,
            'trail_extreme':None,
        }
        self.pending=None

    def on_bar(self, action, *, mid, spread=0., bar_time=None, cash_scale=None,
               high=None, low=None, tick=None, min_distance=None,
               stop_distance=None, profit_distance=None,
               kappa=None, sl_scale=None,
               model_version=None, slip_bps=None, fill_delay_bars=None,
               shadow_transition=None):
        """Advance paper book one closed bar. Returns closed row or None.

        Execution matches LIVE: SL/TP/trail only — never exit on opposite signal.
        """
        mid=_finite(mid);sp=_finite(spread) or 0.
        if mid is None or mid<=0:return None
        if bar_time and bar_time==self.last_bar:return None
        self.last_bar=bar_time
        self.bars_seen=int(self.bars_seen or 0)+1
        if cash_scale is not None:
            cs=_finite(cash_scale)
            if cs is not None:self.cash_scale=cs
        action=action if action in ('BUY','SELL','HOLD') else 'HOLD'
        slip=DEFAULT_SLIP_BPS if slip_bps is None else float(slip_bps or 0)
        delay=DEFAULT_FILL_DELAY_BARS if fill_delay_bars is None else max(0,int(fill_delay_bars))
        hi=_finite(high);lo=_finite(low)
        if hi is None:hi=mid
        if lo is None:lo=mid
        hi=max(hi,mid);lo=min(lo,mid)
        closed=None

        pos=self.position
        if pos is not None:
            direction=pos.get('direction')
            new_stop=paper_trail_stop(
                direction,pos.get('entry'),pos.get('stop'),pos.get('profit'),
                high=hi,low=lo,mid=mid,spread=sp,
                original_risk=pos.get('original_risk'),
                tick=pos.get('tick') or tick,
                min_distance=pos.get('min_distance') or min_distance)
            if new_stop is not None:
                pos=dict(pos);pos['stop']=float(new_stop);pos['trailed']=True
                self.position=pos
            hit=hit_sl_tp(direction,high=hi,low=lo,stop=pos.get('stop'),
                          profit=pos.get('profit'),mid=mid,spread=sp,slip_bps=slip)
            if hit is not None:
                exit_px,reason=hit
                if reason=='sl' and pos.get('trailed'):
                    reason='trail'
                closed=self._close_row(pos,exit_px,bar_time=bar_time,spread=sp,reason=reason)

        if self.position is None and self.pending is not None:
            pend=self.pending
            due=int(pend.get('open_on_bar') or 0)
            if self.bars_seen>=due and pend.get('direction') in ('BUY','SELL'):
                self._open_position(
                    pend['direction'],mid=mid,spread=sp,slip_bps=slip,bar_time=bar_time,
                    stop_distance=pend.get('stop_distance'),
                    profit_distance=pend.get('profit_distance'),
                    tick=pend.get('tick') or tick,
                    min_distance=pend.get('min_distance') or min_distance,
                    model_version=pend.get('model_version'),
                    shadow_transition=pend.get('shadow_transition'),
                    kappa=pend.get('kappa'),sl_scale=pend.get('sl_scale'))

        if self.position is None and self.pending is None and action in ('BUY','SELL'):
            if delay<=0:
                self._open_position(
                    action,mid=mid,spread=sp,slip_bps=slip,bar_time=bar_time,
                    stop_distance=stop_distance,profit_distance=profit_distance,
                    tick=tick,min_distance=min_distance,
                    model_version=model_version,
                    shadow_transition=shadow_transition,
                    kappa=kappa,sl_scale=sl_scale)
            else:
                self.pending={
                    'direction':action,
                    'open_on_bar':self.bars_seen+delay,
                    'stop_distance':stop_distance,'profit_distance':profit_distance,
                    'tick':tick,'min_distance':min_distance,
                    'model_version':model_version,
                    'shadow_transition':shadow_transition,
                    'kappa':kappa,'sl_scale':sl_scale,
                    'signal_bar':bar_time,
                }

        self._persist()
        return closed

    def paper_cash_pnls(self):
        """Only cash-denominated rows — comparable to broker £."""
        return [float(r['paper_pnl']) for r in self.history
                if r.get('unit')=='account_currency'
                and r.get('paper_pnl') is not None
                and math.isfinite(float(r['paper_pnl']))]

    def paper_rows(self):
        """Cash rows with metadata (model_version, times) for fair compare."""
        return [r for r in self.history
                if r.get('unit')=='account_currency'
                and r.get('paper_pnl') is not None
                and math.isfinite(float(r['paper_pnl']))]

    def paper_pnls(self):
        """Backward-compatible alias → cash-only (never mix price units)."""
        return self.paper_cash_pnls()

    def summary(self):
        pnls=self.paper_cash_pnls()
        return {
            'trades':len(self.history),
            'cash_trades':len(pnls),
            'open':self.position is not None,
            'position':self.position,
            'pending':self.pending,
            'cash_scale':self.cash_scale,
            'paper_net':float(sum(pnls)) if pnls else 0.,
            'wins':sum(1 for x in pnls if x>0),
            'losses':sum(1 for x in pnls if x<0),
            'unit':'account_currency',
            'exit_mode':'sl_tp_trail_only',
        }
