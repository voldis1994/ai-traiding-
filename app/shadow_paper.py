"""Independent shadow paper trading — own position + cash-comparable PnL.

Never remaps LIVE broker £ onto the shadow vote. Each closed M1 bar the shadow
neural core votes BUY/SELL/HOLD; this journal opens/closes a virtual position
using V3 B05–B08 / H03–H04 rules: bid/ask-side SL/TP triggers (BUY closes on
bid, SELL on ask), optional slip, fill delay, fixed SL/TP (trail hard OFF),
no invented high/low from mid, and conservative SL when both hit the same bar
without tick sequence. Opposite-signal flip exits are forbidden.

Optional zone/structure/entry gate kwargs veto paper OPEN when ``allowed`` is
False. Paper cash stays separate from LIVE Funds.

Price-unit moves are converted to account-currency cash via a scale estimated
from recent LIVE broker trades (broker_£ / signed price move). Without that
scale, cash fields stay None — autonomy must not compare price units to £.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .trade_costs import (
    adverse_slip as _adverse_slip,
    fill_price as _shared_fill_price,
    half_spread,
)

STORE_KEY = 'shadow_paper_journal_v1'
STATE_KEY = 'shadow_paper_state_v1'
_SEED_MAX_ROWS = 400

# V3 B08 / H04: when SL and TP both touch the same bar and tick order is
# unknown, never take the optimistic TP — always close as stop-loss.
SAME_BAR_BOTH_HIT_POLICY = 'conservative_sl'

def _store_exec_defaults(store):
    """Operating friction comes from self_params Store — never frozen module constants."""
    from .self_params import DEFAULTS, load
    if store is not None:
        sp=load(store)
        return float(sp['exec_slip_bps']), int(sp['exec_fill_delay_bars'])
    return float(DEFAULTS['exec_slip_bps']), int(DEFAULTS['exec_fill_delay_bars'])


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


def cash_per_price_unit(live_rows, *, min_samples=3):
    """Median broker-£ per unit price move from LIVE closed trades.

    Uses actual_pnl (or pnl) / signed(exit-entry). Captures size + FX into
    account currency without inventing tick_value. Returns None if under-powered.
    """
    ratios=[]
    for r in live_rows or []:
        if not isinstance(r,dict):continue
        pnl=_finite(r.get('actual_pnl'))
        if pnl is None:pnl=_finite(r.get('pnl'))
        entry=_finite(r.get('entry'));exit_px=_finite(r.get('exit'))
        direction=r.get('direction')
        if pnl is None or entry is None or exit_px is None:continue
        if direction not in ('BUY','SELL'):continue
        # Skip zero-poison broker rows — they destroy the £/price scale.
        if abs(pnl)<1e-12:continue
        sign=1 if direction=='BUY' else -1
        move=sign*(exit_px-entry)
        if abs(move)<1e-12:continue
        ratios.append(pnl/move)
    if len(ratios)<int(min_samples):return None
    return float(np.median(ratios))


def _bar_mid_hl(bar, *, default_spread=0.):
    """Normalize a path bar dict/tuple to (mid, high, low, spread).

    H03: never invent high/low from mid — missing extremes stay None so
    callers can mark an incomplete path instead of fabricating range.
    """
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
    return mid,high,low,float(sp)


def simulate_action_on_path(action, bars, *, spread=0., stop_distance=None,
                            profit_distance=None, tick=None, min_distance=None,
                            slip_bps=0., trail=False):
    """Replay BUY/SELL/HOLD on a full OHLC path with LIVE SL/TP costs.

    Same execution rules as ShadowPaperJournal / LIVE: adverse fills, SL before
    TP on the same bar, optional trail (off by default — matches live fixed
    OHLC SL/TP), never flip-exit on opposite signal.
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
    # Refuse invented 2×tick fantasy brackets — caller must pass LIVE OHLC SL/TP.
    if sd is None or sd<=0 or pd is None or pd<=0:
        return None
    sign=1 if action=='BUY' else -1
    stop=entry-sign*float(sd)
    profit=entry+sign*float(pd)
    original_risk=float(sd)
    trailed=False
    for i,bar in enumerate(bars):
        mid,hi,lo,sp_i=_bar_mid_hl(bar,default_spread=sp)
        if mid is None:continue
        # H03: incomplete OHLC → skip SL/TP this bar (do not invent H/L from mid).
        if hi is None or lo is None:
            continue
        sp_bar=float(sp_i if sp_i is not None else sp)
        if trail:
            # Mirror LIVE adaptive arm: BE lock once MFE covers trail_arm fraction.
            arm=None
            try:
                from . import structure_edge as _se
                mfe=sign*(float(hi if action=='BUY' else lo)-float(entry))
                arm=_se.trail_arm_decision(
                    None,epic=None,structure='CHOP',direction=action,
                    mfe=mfe,original_risk=original_risk,
                    original_profit=abs(float(profit)-float(entry)) if profit is not None else None,
                    be_locked=sign*(stop-entry)>=-1e-12)
            except Exception:
                arm=None
            new_stop=paper_trail_stop(
                action,entry,stop,profit,high=hi,low=lo,mid=mid,spread=sp_bar,
                original_risk=original_risk,tick=tick_v,min_distance=min_d,
                arm_decision=arm,
                original_profit=(abs(float(profit)-float(entry))
                                 if profit is not None else None))
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
                    'stop':float(stop),'profit':float(profit),
                    'same_bar_both_hit_policy':SAME_BAR_BOTH_HIT_POLICY}
    mid_l,_,_,sp_l=_bar_mid_hl(bars[-1],default_spread=sp)
    if mid_l is None:return None
    close_dir='SELL' if action=='BUY' else 'BUY'
    exit_px=_fill_price(close_dir,mid_l,sp_l if sp_l is not None else sp,slip_bps=slip)
    pnl=_close_price_pnl(action,entry,exit_px)
    return {'pnl':float(pnl),'reason':'path_end','entry':float(entry),
            'exit':float(exit_px),'bars_held':len(bars),'path':True,
            'stop':float(stop),'profit':float(profit)}


def paper_trail_stop(direction, entry, stop, profit, *, high, low, mid, spread,
                     original_risk, tick, min_distance, store=None,
                     arm_decision=None, original_profit=None, atr=None):
    """Mirror LIVE broker trail using bar extremes (bid/ask ≈ mid ± half-spread).

    Same BE / post-BE / early-arm gap idea as `propose_broker_trail` — never
    flips on signal. Returns new stop or None when no improvement.
    """
    direction=direction if direction in ('BUY','SELL') else None
    if direction is None:return None
    entry=_finite(entry);stop=_finite(stop);tick=_finite(tick)
    high=_finite(high);low=_finite(low);mid=_finite(mid)
    minimum=_finite(min_distance);gap=_finite(original_risk)
    sp=_finite(spread) or 0.
    if None in (entry,stop,tick,high,low,mid) or tick<=0:return None
    if minimum is None or minimum<0:minimum=tick
    # Match LIVE: refuse tip±min glue when opening risk is missing.
    if gap is None or gap<=0:
        return None
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
    early_arm=bool(isinstance(arm_decision,dict) and arm_decision.get('arm'))
    orig_tp=_finite(original_profit)
    if orig_tp is None and profit is not None and entry is not None:
        cand=sign*(float(profit)-float(entry))
        if cand is not None and cand>0:orig_tp=cand
    if be_locked:
        from .protection_ai import _post_be_trail_gap
        trail_gap=_post_be_trail_gap(
            open_gap,minimum,None,store=store,
            original_profit=orig_tp,atr=atr)
    else:
        trail_gap=open_gap
    tip_fav=sign*(tip-entry)
    cushion=0.0
    if not be_locked and early_arm and tip_fav>0:
        try:
            from .self_params import load as _sp_load, DEFAULTS as _SP_DEF
            sp=_sp_load(store) or {}
            arm_frac=float(sp.get('trail_arm_mfe_frac',_SP_DEF['trail_arm_mfe_frac']))
            tp_frac=float(sp.get('trail_arm_tp_frac',
                                 _SP_DEF.get('trail_arm_tp_frac',0.45)))
            cush_frac=float(sp.get('trail_be_cushion_frac',
                                   _SP_DEF.get('trail_be_cushion_frac',0.15)))
        except Exception:
            arm_frac=1.0;tp_frac=0.45;cush_frac=0.15
        arm_frac=max(0.70,min(1.0,arm_frac))
        tp_frac=max(0.25,min(0.70,tp_frac))
        cush_frac=max(0.05,min(0.35,cush_frac))
        floor_need=arm_frac*open_gap
        if orig_tp is not None and orig_tp>0:
            floor_need=max(floor_need,tp_frac*float(orig_tp))
        if tip_fav<floor_need:
            return None
        cushion=min(tip_fav*0.5,cush_frac*open_gap)
        trail_gap=min(open_gap,max(minimum,tip_fav-cushion))
        need=floor_need
    else:
        need=open_gap if not be_locked else trail_gap
    if tip_fav<need:return None
    profit=_finite(profit)
    if direction=='BUY':
        tip=max(tip,bid)
        if tip<=entry:return None
        desired=min(tip-trail_gap,bid-minimum)
        if early_arm and not be_locked:
            desired=max(desired,entry+cushion)
        new_stop=math.floor(desired/tick+1e-12)*tick
        if early_arm and not be_locked and new_stop<entry-1e-12:
            return None
        if new_stop<=stop or new_stop>=tip:return None
        if profit is not None and profit<=new_stop:return None
        if new_stop<stop+tick-1e-12:return None
        return float(new_stop)
    tip=min(tip,ask)
    if tip>=entry:return None
    desired=max(tip+trail_gap,ask+minimum)
    if early_arm and not be_locked:
        desired=min(desired,entry-cushion)
    new_stop=math.ceil(desired/tick-1e-12)*tick
    if early_arm and not be_locked and new_stop>entry+1e-12:
        return None
    if new_stop>=stop or new_stop<=tip:return None
    if profit is not None and profit>=new_stop:return None
    if new_stop>stop-tick+1e-12:return None
    return float(new_stop)


def hit_sl_tp(direction, *, high, low, stop, profit, mid, spread, slip_bps=0.):
    """Return (exit_px, reason) if SL/TP is touched on the executable close side.

    V3 B05/B08: BUY closes on bid, SELL on ask. With mid bar extremes and full
    spread ``s``, bid=mid−s/2 and ask=mid+s/2 — never test TP on the opposite
    side of the spread (the mid-bar bug that treated BUY TP as ask / SELL TP as
    bid).

    Same-bar both-hit without tick sequence → ``SAME_BAR_BOTH_HIT_POLICY``
    (``conservative_sl``): close as SL with gap-aware adverse fill, never
    optimistic TP. Callers must pass real high/low; missing extremes → None
    (H03: do not invent H/L from mid).

    SL/TP fills apply gap-through extremes and adverse ``slip_bps``.
    """
    high=_finite(high);low=_finite(low)
    stop=_finite(stop);profit=_finite(profit)
    if high is None or low is None:return None
    half=half_spread(spread)
    mid_m=_finite(mid)
    slip_ref=mid_m if mid_m is not None else (high+low)/2.
    slip=_adverse_slip(slip_ref,slip_bps)
    if direction=='BUY':
        # Executable bid extremes this bar (close side for BUY).
        bid_hi=high-half
        bid_lo=low-half
        sl_hit=stop is not None and bid_lo<=stop
        tp_hit=profit is not None and bid_hi>=profit
        if sl_hit:
            # Gap through SL → worse of stop and lowest bid; then adverse slip.
            fill=min(float(stop),bid_lo)-slip
            return float(fill),'sl'
        if tp_hit:
            # Conservative TP fill at level − adverse exit slip (no optimistic gap).
            return float(profit)-slip,'tp'
    elif direction=='SELL':
        # Executable ask extremes this bar (close side for SELL).
        ask_hi=high+half
        ask_lo=low+half
        sl_hit=stop is not None and ask_hi>=stop
        tp_hit=profit is not None and ask_lo<=profit
        if sl_hit:
            fill=max(float(stop),ask_hi)+slip
            return float(fill),'sl'
        if tp_hit:
            return float(profit)+slip,'tp'
    return None


def _gate_blocks_paper_open(*, entry_gate=None, zone_gate=None,
                            structure_gate=None, entry_context=None):
    """True when a provided zone/structure/entry gate vetoes paper OPEN.

    Only blocks when a gate dict is supplied with ``allowed is False``.
    Omitted gates are ignored (engine may still stamp diagnostic context
    without wiring the veto — H02/H04 wiring is additive on this signature).
    """
    for g in (entry_gate,zone_gate,structure_gate):
        if isinstance(g,dict) and g.get('allowed') is False:
            return True
    if isinstance(entry_context,dict):
        for key in ('entry_gate','zone_gate','structure_gate','ohlc_entry'):
            g=entry_context.get(key)
            if isinstance(g,dict) and g.get('allowed') is False:
                return True
    return False


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

    def update_cash_scale(self, live_rows, *, min_samples=3):
        """Refresh £/price-unit from LIVE broker history when possible.

        Also backfills cash on price-only paper closes so PAPER BOOK no longer
        shows £0 while RECENT PAPER CLOSES already lists SL/TP rows.
        """
        scale=cash_per_price_unit(live_rows,min_samples=min_samples)
        if scale is None and self.store is not None:
            # Fallback: journal broker cash closes (profit_learning may lag).
            try:
                rows=self.store.trades(80)
            except Exception:
                rows=[]
            live=[]
            for r in rows or []:
                if not isinstance(r,dict) or not r.get('closed'):continue
                if str(r.get('pnl_source') or '')!='broker':continue
                live.append({
                    'actual_pnl':r.get('pnl'),'entry':r.get('entry'),
                    'exit':r.get('exit'),'direction':r.get('direction'),
                })
            scale=cash_per_price_unit(live,min_samples=min_samples)
        if scale is not None and math.isfinite(scale) and abs(float(scale))>1e-12:
            prev=self.cash_scale
            self.cash_scale=float(scale)
            n=self._revalue_price_rows()
            # Persist only when scale or row cash changed — desk polls often.
            if n or prev!=self.cash_scale:
                self._persist()
        return self.cash_scale

    def _revalue_price_rows(self):
        """Apply cash_scale to history rows stuck in price units."""
        scale=_finite(self.cash_scale)
        if scale is None or abs(scale)<1e-12:return 0
        n=0
        for r in self.history:
            if not isinstance(r,dict):continue
            if r.get('unit')=='account_currency' and r.get('paper_pnl') is not None:
                continue
            pp=_finite(r.get('price_pnl'))
            if pp is None:continue
            r['paper_pnl']=float(pp*scale)
            r['cash_scale']=float(scale)
            r['unit']='account_currency'
            n+=1
        return n

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
            'entry_context':pos.get('entry_context'),
        }
        self.history.append(closed)
        self.position=None
        return closed

    def _open_position(self, action, *, mid, spread, slip_bps, bar_time,
                       stop_distance, profit_distance, tick, min_distance,
                       model_version, shadow_transition, kappa, sl_scale,
                       entry_context=None):
        entry=_fill_price(action,mid,spread,slip_bps=slip_bps)
        tick=float(tick) if _finite(tick) and float(tick)>0 else max(abs(float(spread or 0))/2.,1e-4)
        sd=_finite(stop_distance)
        pd=_finite(profit_distance)
        # Same honesty as LIVE OPEN: no OHLC swing/regime distances → no paper open.
        # Never invent 2×tick fantasy SL/TP that would poison shadow→LIVE teach.
        if sd is None or sd<=0 or pd is None or pd<=0:
            self.pending=None
            return
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
            'entry_context':entry_context if isinstance(entry_context,dict) else None,
            'trail_extreme':None,
        }
        self.pending=None

    def on_bar(self, action, *, mid, spread=0., bar_time=None, cash_scale=None,
               high=None, low=None, tick=None, min_distance=None,
               stop_distance=None, profit_distance=None,
               kappa=None, sl_scale=None,
               model_version=None, slip_bps=None, fill_delay_bars=None,
               shadow_transition=None, entry_context=None,
               entry_gate=None, zone_gate=None, structure_gate=None):
        """Advance paper book one closed bar. Returns closed row or None.

        Execution: fixed SL/TP only (trail HARD OFF) — never exit on opposite
        signal. Paper cash stays independent of LIVE Funds.

        Optional ``entry_gate`` / ``zone_gate`` / ``structure_gate``: when a
        dict with ``allowed is False`` is provided, paper OPEN is vetoed
        (V3 H02/H04 zone parity). Omitted gates do not block.
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
        if slip_bps is None or fill_delay_bars is None:
            d_slip,d_delay=_store_exec_defaults(self.store)
            if slip_bps is None:slip_bps=d_slip
            if fill_delay_bars is None:fill_delay_bars=d_delay
        slip=float(slip_bps)
        delay=max(0,int(fill_delay_bars))
        # H03: never invent high/low from mid — incomplete path skips SL/TP.
        hi=_finite(high);lo=_finite(low)
        closed=None

        pos=self.position
        if pos is not None and hi is not None and lo is not None:
            direction=pos.get('direction')
            # Trail HARD OFF — fixed OHLC SL/TP only (no tip±gap).
            hit=hit_sl_tp(direction,high=hi,low=lo,stop=pos.get('stop'),
                          profit=pos.get('profit'),mid=mid,spread=sp,slip_bps=slip)
            if hit is not None:
                exit_px,reason=hit
                if reason=='sl' and pos.get('trailed'):
                    reason='trail'
                closed=self._close_row(pos,exit_px,bar_time=bar_time,spread=sp,reason=reason)

        gate_block=_gate_blocks_paper_open(
            entry_gate=entry_gate,zone_gate=zone_gate,
            structure_gate=structure_gate,entry_context=entry_context)

        if self.position is None and self.pending is not None:
            pend=self.pending
            # Re-check gate at fill time (H03 plan validity); cancel if vetoed.
            pend_ctx=pend.get('entry_context')
            pend_block=_gate_blocks_paper_open(
                entry_gate=entry_gate,zone_gate=zone_gate,
                structure_gate=structure_gate,entry_context=pend_ctx)
            if pend_block or gate_block:
                self.pending=None
            else:
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
                        kappa=pend.get('kappa'),sl_scale=pend.get('sl_scale'),
                        entry_context=pend.get('entry_context'))

        if self.position is None and self.pending is None and action in ('BUY','SELL'):
            # Zone/structure veto when provided — do not ignore (V3 H04).
            if gate_block:
                pass
            else:
                # No OHLC SL/TP → skip paper open (do not queue fantasy 2×tick brackets).
                sd_ok=_finite(stop_distance);pd_ok=_finite(profit_distance)
                if sd_ok is not None and sd_ok>0 and pd_ok is not None and pd_ok>0:
                    if delay<=0:
                        self._open_position(
                            action,mid=mid,spread=sp,slip_bps=slip,bar_time=bar_time,
                            stop_distance=stop_distance,profit_distance=profit_distance,
                            tick=tick,min_distance=min_distance,
                            model_version=model_version,
                            shadow_transition=shadow_transition,
                            kappa=kappa,sl_scale=sl_scale,
                            entry_context=entry_context)
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
                            'entry_context':entry_context if isinstance(entry_context,dict) else None,
                        }

        self._persist()
        return closed

    @staticmethod
    def _is_live_or_capital_row(r):
        """Capital / LIVE journal rows must never enter independent Paper £."""
        if not isinstance(r,dict):
            return False
        src=str(r.get('source') or '').lower()
        if src in ('capital_journal','live','live_backfill','live_funds_mirror',
                   'live_journal','broker','funds','capital'):
            return True
        if src.startswith('live_'):
            return True
        # Explicit Capital deal stamps (cap:… / deal UUID from Funds mirror).
        if r.get('pnl_source') in ('broker','funds','capital') and src in (
                'capital_journal','live_journal',''):
            return src=='capital_journal' or src=='live_journal'
        return False

    @staticmethod
    def _row_is_cash(r):
        """Independent SHADOW paper cash £ only — never Capital Funds rows."""
        if not isinstance(r,dict):return False
        if ShadowPaperJournal._is_live_or_capital_row(r):
            return False
        if _finite(r.get('paper_pnl')) is None:return False
        u=r.get('unit')
        if u=='price':return False
        if u=='account_currency':return True
        # Legacy: paper_pnl present, no unit, no price_pnl → treated as cash.
        if u is None and _finite(r.get('price_pnl')) is None:return True
        if u is None and _finite(r.get('cash_scale')) is not None:return True
        return False

    def paper_cash_pnls(self):
        """Only independent paper cash — never Capital Funds £."""
        return [float(r['paper_pnl']) for r in self.history
                if self._row_is_cash(r) and math.isfinite(float(r['paper_pnl']))]

    def paper_rows(self):
        """Cash rows with metadata (model_version, times) for fair compare."""
        return [r for r in self.history if self._row_is_cash(r)]

    def purge_capital_journal_rows(self):
        """Drop any Capital→Paper mix left by a bad seed (PR #240 regression)."""
        before=len(self.history or [])
        self.history=[r for r in (self.history or [])
                      if not (isinstance(r,dict)
                              and str(r.get('source') or '').lower()
                              =='capital_journal')]
        dropped=before-len(self.history)
        if dropped:
            self._persist()
        return dropped

    def paper_pnls(self):
        """Backward-compatible alias → cash-only (never mix price units)."""
        return self.paper_cash_pnls()

    def backfill_from_mistake_audits(self):
        """Restore empty paper journal from shadow mistake-audit hist.

        Instrument bag switches clear live paper keys while audit hist is not
        bag-scoped — Desk then shows Paper £0 with audits still listing -12.94.
        """
        if self.store is None:return 0
        if any(isinstance(r,dict) for r in (self.history or [])):
            return 0
        try:
            from .shadow_mistake_audit import HIST_KEY
            hist=list(self.store.get(HIST_KEY) or [])
        except Exception:
            return 0
        added=0
        for h in hist:
            if not isinstance(h,dict):continue
            src=str(h.get('source') or 'shadow').lower()
            # LIVE / Funds audits belong in MISTAKE AUDIT + CLIENT CLOSES — not Paper.
            if src=='live' or src.startswith('live_') or src in (
                    'capital_journal','broker','funds','capital'):
                continue
            pnl=_finite(h.get('pnl'))
            if pnl is None:continue
            # Prefer explicit unit / price_pnl — never guess £2+ LIVE losses as pts
            # and never invent GBP when unit is unknown → keep as price if no unit.
            explicit_unit=h.get('unit')
            has_price=_finite(h.get('price_pnl')) is not None
            if explicit_unit in ('price','account_currency'):
                unit=explicit_unit
            elif has_price and _finite(h.get('paper_pnl')) is None:
                unit='price'
            elif explicit_unit is None and not has_price:
                # Unknown unit — do NOT treat as GBP account cash.
                unit='price'
            else:
                unit='account_currency'
            row={
                'at':h.get('at'),
                'direction':h.get('direction'),
                'entry':h.get('entry'),
                'exit':h.get('exit'),
                'price_pnl':(_finite(h.get('price_pnl')) if unit=='price'
                             else None) or (float(pnl) if unit=='price' else None),
                'paper_pnl':float(pnl) if unit=='account_currency' else (
                    _finite(h.get('paper_pnl'))),
                'unit':unit,
                'exit_reason':h.get('exit_reason') or 'audit_backfill',
                'source':'shadow_paper_audit_backfill',
                'fingerprint':h.get('fingerprint'),
                'scenario':h.get('scenario'),
            }
            self.history.append(row)
            added+=1
        if added:
            self._persist()
        return added

    def summary(self):
        # Scrub any Capital→Paper poison before scoring.
        try:
            self.purge_capital_journal_rows()
        except Exception:
            pass
        pnls=self.paper_cash_pnls()
        price_pnls=[]
        for r in self.history:
            if not isinstance(r,dict):continue
            if self._is_live_or_capital_row(r):
                continue
            if self._row_is_cash(r):continue
            pp=_finite(r.get('price_pnl'))
            if pp is None and r.get('unit')=='price':
                pp=_finite(r.get('paper_pnl'))
            if pp is not None:price_pnls.append(float(pp))
        cash_net=float(sum(pnls)) if pnls else 0.
        price_net=float(sum(price_pnls)) if price_pnls else 0.
        # Prefer cash £ when available; otherwise show price-unit truth (not fake 0).
        if pnls:
            display_net,display_trades,display_unit=cash_net,len(pnls),'account_currency'
            wins=sum(1 for x in pnls if x>0)
            losses=sum(1 for x in pnls if x<0)
        elif price_pnls:
            display_net,display_trades,display_unit=price_net,len(price_pnls),'price'
            wins=sum(1 for x in price_pnls if x>0)
            losses=sum(1 for x in price_pnls if x<0)
        else:
            display_net,display_trades,display_unit=0.,0,'account_currency'
            wins=losses=0
        paper_only=sum(1 for r in (self.history or [])
                       if isinstance(r,dict) and not self._is_live_or_capital_row(r))
        return {
            'trades':paper_only,
            'cash_trades':len(pnls),
            'price_trades':len(price_pnls),
            'open':self.position is not None,
            'position':self.position,
            'pending':self.pending,
            'cash_scale':self.cash_scale,
            'paper_net':cash_net if pnls else (display_net if price_pnls else 0.),
            'price_net':price_net,
            'display_net':display_net,
            'display_trades':display_trades,
            'wins':wins,
            'losses':losses,
            'unit':display_unit,
            'exit_mode':'sl_tp_only',
            'audit_backfill':any(
                isinstance(r,dict) and r.get('source')=='shadow_paper_audit_backfill'
                for r in (self.history or [])),
            'capital_journal_seed':False,
            'independent_of_live':True,
        }
