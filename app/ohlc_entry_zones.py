"""OHLC structural entry — unified intent + trigger for Admin chart and LIVE OPEN.

Intent comes from neural + MTF. Trigger is either:
  • zone  — pullback into discount (BUY) / premium (SELL) band, or
  • break — structure / range break in the neural side (do not wait for pullback).

Bands still drive SL/TP geometry. MTF hard conflict (weak ADX / strong opposite)
holds OPEN. Never invent mid ± κ.
"""
from __future__ import annotations

from . import structure_memory


def _finite(v):
    try:
        x=float(v)
    except (TypeError,ValueError):
        return None
    return x if x==x and abs(x)!=float('inf') else None


def _mtf_atr(mtf):
    if not isinstance(mtf,dict) or not mtf.get('ready'):
        return None
    atr=_finite(mtf.get('stop_hint_atr14'))
    if atr is not None and atr>0:
        return atr
    tf=(mtf.get('tf') or {}).get('m5') or {}
    atr=_finite(tf.get('atr14'))
    return atr if atr is not None and atr>0 else None


def _mtf_kama(mtf):
    if not isinstance(mtf,dict) or not mtf.get('ready'):
        return None
    tf=mtf.get('tf') or {}
    for key in ('m5','m1','m60'):
        k=_finite((tf.get(key) or {}).get('kama'))
        if k is not None:
            return k
    return None


def compute_entry_zones(candles,*,mid=None,tick=0.01,spread=0.0,mtf=None):
    """Return structural BUY/SELL entry bands from OHLC swings / BOS + MTF.

    Same geometry Admin draws and LIVE waits for. ATR widens/narrows the band
    from real volatility; KAMA can tighten the wait edge when inside the range.
    """
    out={
        'ok':False,'reason':'no_candles',
        'support':None,'resistance':None,
        'buy_entry':None,'sell_entry':None,
        'range_lo':None,'range_hi':None,'band':None,
        'bias':None,'event':None,
        'atr14':None,'kama':None,'mtf_mode':None,'mtf_preferred':None,
        'info_stack':'ohlc_swing',
    }
    if not candles or len(candles)<20:
        return out
    bos=structure_memory.bos_choch(candles,left=2,right=2) or {}
    live=structure_memory.snapshot_live(candles=candles,mid=mid) or {}
    lo=_finite(bos.get('range_lo'))
    hi=_finite(bos.get('range_hi'))
    out['bias']=bos.get('bias') or live.get('live_bias')
    out['event']=bos.get('event') or live.get('live_event')
    out['range_lo']=lo
    out['range_hi']=hi
    atr=_mtf_atr(mtf)
    kama=_mtf_kama(mtf)
    out['atr14']=atr
    out['kama']=kama
    if isinstance(mtf,dict) and mtf.get('ready'):
        out['mtf_mode']=mtf.get('mode')
        out['mtf_preferred']=mtf.get('preferred')
        out['info_stack']='ohlc_swing+kama_adx_atr'
    if lo is None or hi is None or not (hi>lo):
        out['reason']='no_swing_range'
        return out
    try:
        tick_f=max(float(tick or 0.01),1e-9)
    except (TypeError,ValueError):
        tick_f=0.01
    try:
        spread_f=max(float(spread or 0),0.)
    except (TypeError,ValueError):
        spread_f=0.
    span=hi-lo
    # ATR from the same MTF stack LIVE uses — not an invented mid ± κ.
    band=max(span*0.12,tick_f*8,spread_f*2)
    if atr is not None:
        band=max(band,0.25*atr)
        band=min(band,span*0.45)  # never swallow the whole range
    out['band']=band
    out['support']={'lo':lo,'hi':lo,'label':'Support / swing low'}
    out['resistance']={'lo':hi,'hi':hi,'label':'Resistance / swing high'}
    buy_hi=lo+band
    sell_lo=hi-band
    # KAMA inside range tightens the wait edge toward adaptive trend.
    if kama is not None and lo<kama<hi:
        buy_hi=min(buy_hi,max(lo+tick_f,kama))
        sell_lo=max(sell_lo,min(hi-tick_f,kama))
    if buy_hi>lo:
        out['buy_entry']={
            'lo':lo,'hi':buy_hi,'label':'BUY entry zona','side':'BUY',
            'source':out['info_stack']}
    if sell_lo<hi:
        out['sell_entry']={
            'lo':sell_lo,'hi':hi,'label':'SELL entry zona','side':'SELL',
            'source':out['info_stack']}
    out['ok']=bool(out['buy_entry'] or out['sell_entry'])
    out['reason']='swing_levels' if out['ok'] else 'empty_bands'
    return out


def price_in_zone(zone,price,*,buffer=0.0):
    """True when price sits inside [lo−buf, hi+buf]."""
    if not isinstance(zone,dict):
        return False
    lo=_finite(zone.get('lo'))
    hi=_finite(zone.get('hi'))
    p=_finite(price)
    if lo is None or hi is None or p is None:
        return False
    if hi<lo:
        lo,hi=hi,lo
    try:
        buf=max(float(buffer or 0),0.)
    except (TypeError,ValueError):
        buf=0.
    return (lo-buf)<=p<=(hi+buf)


def _mtf_allows_side(action,mtf):
    """Hard MTF filter: do not OPEN against a *strongly* opposing KAMA/ADX stack.

    Soft HOLD / mild cross no longer freezes entry when OHLC preferred agrees
    with the neural side — only hard ADX/spread blocks and high-confidence
    opposite preferred (≥0.75) keep the wait.
    """
    if not isinstance(mtf,dict) or not mtf.get('ready'):
        return True,'mtf_not_ready'
    if mtf.get('enabled') is False:
        return True,'mtf_disabled'
    mode=str(mtf.get('mode') or 'HOLD').upper()
    preferred=str(mtf.get('preferred') or 'HOLD').upper()
    reason=str(mtf.get('reason') or '')
    try:
        conf=float(mtf.get('confidence') or 0)
    except (TypeError,ValueError):
        conf=0.
    # Hard cost/ADX blocks only — soft HOLD reasons stay soft (neural+nudge).
    if mode=='HOLD' and reason in ('spread_slip_or_weak_adx','weak_adx'):
        return False,f'wait_mtf_{reason}'
    if mtf.get('move_ok') is False:
        return False,'wait_mtf_spread_atr'
    # OHLC/MTF already prefers our side — never treat as conflict.
    if preferred==action or mode==action:
        return True,'mtf_aligned'
    # Only hard-block a *strong* opposite preferred (was 0.55 — froze on mild cross).
    if (conf>=0.75 and preferred in ('BUY','SELL') and preferred!=action
            and mode!='SHIFT'):
        return False,f'wait_mtf_conflict ({mode}/{preferred})'
    # Mild cross / soft HOLD — allow zone check (neural+OHLC decide).
    if mode=='HOLD':
        return True,'mtf_hold_soft'
    # SHIFT preferring the other side = early reverse — do not chase old side
    # unless confidence is soft (<0.60).
    if mode=='SHIFT' and preferred in ('BUY','SELL') and preferred!=action:
        if conf>=0.60:
            return False,f'wait_mtf_shift ({preferred})'
        return True,'mtf_shift_soft'
    return True,'mtf_aligned'

def drop_forming_minute(candles, *, now_utc=None):
    """Exclude the unfinished UTC minute — same cutoff as market_features."""
    from .forecast_targets import completed_candles
    return completed_candles(candles, now_utc=now_utc)


def _structure_break_side(mtf,zones):
    """UP/DOWN break from MTF stack or swing event on the zone card."""
    if isinstance(mtf,dict) and mtf.get('ready'):
        br=mtf.get('structure_break')
        if br in ('UP','DOWN'):
            return br
        m1=((mtf.get('tf') or {}).get('m1') or {})
        br=m1.get('structure_break')
        if br in ('UP','DOWN'):
            return br
        event=str(mtf.get('bos_event') or '').upper()
        if event in ('BOS_UP','CHOCH_UP','BREAKOUT_UP'):
            return 'UP'
        if event in ('BOS_DOWN','CHOCH_DOWN','BREAKOUT_DOWN'):
            return 'DOWN'
    if isinstance(zones,dict):
        # Only confirmed swing events — live bias alone is not a break trigger
        # (would OPEN every climb above the discount band).
        event=str(zones.get('event') or '').upper()
        if event in ('BOS_UP','CHOCH_UP','BREAKOUT_UP'):
            return 'UP'
        if event in ('BOS_DOWN','CHOCH_DOWN','BREAKOUT_DOWN'):
            return 'DOWN'
    return None


def m1_tape_agrees(action,candles,*,need=2):
    """Last ``need`` *completed* M1 bodies agree with action (BUY=green, SELL=red)."""
    action=(action or '').upper()
    need=max(1,int(need))
    rows=list(candles or [])
    if len(rows)<need:
        return False,0
    streak=0
    for row in reversed(rows[-12:]):
        o=_finite(row.get('open'));c=_finite(row.get('close'))
        if o is None or c is None:
            break
        if action=='BUY' and c>o:
            streak+=1
        elif action=='SELL' and c<o:
            streak+=1
        else:
            break
        if streak>=need:
            return True,streak
    return streak>=need,streak


def _last_completed_close(candles):
    rows=list(candles or [])
    if not rows:
        return None
    return _finite(rows[-1].get('close'))


def break_trigger_allows(action,*,price,zones,mtf=None,tick=0.01,candles=None):
    """True when structure/range break aligns with neural side AND M1 tape.

    Unified path: intent + break → OPEN without pullback zone. Micro BOS alone
    must not BUY into a red dump (or SELL into a green spike) — that was the
    illogical OHLC chase. Zones remain for SL/TP geometry.
    """
    action=(action or '').upper()
    out={'ok':False,'reason':'no_break','side':None,'trigger':'break','tape_streak':0}
    if action not in ('BUY','SELL'):
        out['reason']='bad_action'
        return out
    p=_finite(price)
    if p is None:
        out['reason']='no_price'
        return out
    tape_ok,tape_streak=m1_tape_agrees(action,candles,need=2)
    opp='SELL' if action=='BUY' else 'BUY'
    opp_ok,_opp_streak=m1_tape_agrees(opp,candles,need=2)
    out['tape_streak']=tape_streak
    pref=str((mtf or {}).get('preferred') or '').upper() if isinstance(mtf,dict) else ''
    mode=str((mtf or {}).get('mode') or '').upper() if isinstance(mtf,dict) else ''
    try:
        conf=float((mtf or {}).get('confidence') or 0) if isinstance(mtf,dict) else 0.
    except (TypeError,ValueError):
        conf=0.
    mtf_aligned=(pref==action) or (mode==action) or (mode=='SHIFT' and pref==action and conf>=0.55)
    # Against a clear opposite M1 dump/spike — never break-chase the bounce.
    if opp_ok and not tape_ok:
        out['reason']='break_against_m1_tape'
        return out
    side=_structure_break_side(mtf,zones)
    out['side']=side
    want='UP' if action=='BUY' else 'DOWN'
    if side==want:
        # Micro structure break needs M1 pressure and/or strong MTF same side.
        if tape_ok or (mtf_aligned and conf>=0.55):
            out['ok']=True
            out['reason']='ohlc_break_structure'
            return out
        out['reason']='break_needs_m1_or_mtf'
        return out
    # Range break: completed close must already clear the swing — live mid alone
    # is forming-bar chase against finished OHLC.
    if isinstance(zones,dict) and isinstance(mtf,dict) and mtf.get('ready'):
        hi=_finite(zones.get('range_hi'))
        lo=_finite(zones.get('range_lo'))
        last_c=_last_completed_close(candles)
        try:
            buf=max(float(tick or 0.01),0.)
        except (TypeError,ValueError):
            buf=0.01
        aligned=mtf_aligned or (mode=='SHIFT' and conf>=0.55)
        if (action=='BUY' and aligned and hi is not None and last_c is not None
                and last_c>=(hi-buf) and p>=(hi-buf) and (tape_ok or conf>=0.55)):
            out['ok']=True
            out['reason']='ohlc_break_range_high'
            return out
        if (action=='SELL' and aligned and lo is not None and last_c is not None
                and last_c<=(lo+buf) and p<=(lo+buf) and (tape_ok or conf>=0.55)):
            out['ok']=True
            out['reason']='ohlc_break_range_low'
            return out
    return out


def entry_allows_open(action,*,candles,mid=None,ask=None,bid=None,
                      tick=0.01,spread=0.0,mtf=None):
    """Gate LIVE OPEN: zone touch OR structure/range break + MTF ok.

    BUY uses ask (or mid); SELL uses bid (or mid). Buffer = 1 tick.
    """
    action=(action or '').upper()
    meta={
        'allowed':False,'action':action,'reason':'bad_action',
        'zones':None,'price':None,'zone':None,'source':'ohlc_entry_zones',
        'mtf_ok':None,'trigger':None,'intent':action if action in ('BUY','SELL') else None}
    if action not in ('BUY','SELL'):
        return meta
    mtf_ok,mtf_reason=_mtf_allows_side(action,mtf)
    meta['mtf_ok']=mtf_ok
    if not mtf_ok:
        meta['reason']=mtf_reason
        meta['zones']=compute_entry_zones(
            candles,mid=mid,tick=tick,spread=spread,mtf=mtf)
        return meta
    zones=compute_entry_zones(
        candles,mid=mid,tick=tick,spread=spread,mtf=mtf)
    meta['zones']=zones
    if not zones.get('ok'):
        meta['reason']=f"wait_entry_zone ({zones.get('reason') or 'unready'})"
        return meta
    zone=zones.get('buy_entry') if action=='BUY' else zones.get('sell_entry')
    meta['zone']=zone
    if action=='BUY':
        price=_finite(ask)
        if price is None:
            price=_finite(mid)
    else:
        price=_finite(bid)
        if price is None:
            price=_finite(mid)
    meta['price']=price
    if price is None:
        meta['reason']='wait_entry_zone (no_price)'
        return meta
    # --- Trigger A: break (catch the move — do not wait for pullback) ---
    # Pass completed candles so break cannot fight the M1 tape / forming mid.
    br=break_trigger_allows(
        action,price=price,zones=zones,mtf=mtf,tick=tick,candles=candles)
    meta['break']=br
    if br.get('ok'):
        meta['allowed']=True
        meta['trigger']='break'
        meta['reason']=br.get('reason') or 'ohlc_break_trigger'
        return meta
    if not zone:
        meta['reason']=f'wait_entry_zone (no_{action.lower()}_band)'
        return meta
    try:
        buf=max(float(tick or 0.01),0.)
    except (TypeError,ValueError):
        buf=0.01
    # Strong OHLC/MTF agreement → slightly wider touch so mid-tape "cross" into
    # the band still fires instead of waiting another closed M1.
    try:
        conf=float((mtf or {}).get('confidence') or 0) if isinstance(mtf,dict) else 0.
    except (TypeError,ValueError):
        conf=0.
    pref=str((mtf or {}).get('preferred') or '').upper() if isinstance(mtf,dict) else ''
    if pref==action and conf>=0.50:
        buf=max(buf,buf*3.0,float(tick or 0.01)*3)
        meta['wide_ohlc_touch']=True
    # --- Trigger B: pullback zone (cheaper entry into structure) ---
    # Zone BUY at discount during a dump is intentional OHLC (support); break
    # chase into the dump was the illogical path (blocked above).
    if price_in_zone(zone,price,buffer=buf):
        meta['allowed']=True
        meta['trigger']='zone'
        meta['reason']='ohlc_entry_zone_touch'
        return meta
    lo=_finite(zone['lo']);hi=_finite(zone['hi'])
    if action=='BUY' and price>hi:
        meta['reason']=f'wait_buy_entry_zone (price {price:.5g} > zona {hi:.5g})'
    elif action=='SELL' and price<lo:
        meta['reason']=f'wait_sell_entry_zone (price {price:.5g} < zona {lo:.5g})'
    else:
        meta['reason']=f'wait_entry_zone (price {price:.5g} outside [{lo:.5g},{hi:.5g}])'
    return meta
