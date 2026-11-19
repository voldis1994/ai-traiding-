"""OHLC structural entry zones — shared by Admin chart and LIVE OPEN.

BUY waits in the discount band above swing/BOS low; SELL waits in the premium
band below swing/BOS high. Band width uses ATR when MTF is ready; KAMA can
tighten the wait band when it sits inside the swing range. MTF conflict
(ADX/KAMA opposite / weak ADX / spread block) holds OPEN. Never invent mid ± κ.
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
    """Hard MTF filter: do not OPEN against ready KAMA/ADX/ATR stack."""
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
    # Weak trend / cost block — same signals that keep MTF in HOLD.
    if mode=='HOLD' and reason in (
            'spread_slip_or_weak_adx','weak_adx','signals_conflict_or_weak',
            'tf_conflict_or_weak_trend'):
        return False,f'wait_mtf_{reason}'
    if mtf.get('move_ok') is False:
        return False,'wait_mtf_spread_atr'
    # Strong opposite side (not SHIFT-into-us).
    if (conf>=0.55 and preferred in ('BUY','SELL') and preferred!=action
            and mode!='SHIFT'):
        return False,f'wait_mtf_conflict ({mode}/{preferred})'
    # SHIFT preferring the other side = early reverse — do not chase old side.
    if mode=='SHIFT' and preferred in ('BUY','SELL') and preferred!=action:
        return False,f'wait_mtf_shift ({preferred})'
    return True,'mtf_aligned'


def entry_allows_open(action,*,candles,mid=None,ask=None,bid=None,
                      tick=0.01,spread=0.0,mtf=None):
    """Gate LIVE OPEN: neural side only when price is in OHLC entry zone + MTF ok.

    BUY uses ask (or mid); SELL uses bid (or mid). Buffer = 1 tick.
    """
    action=(action or '').upper()
    meta={
        'allowed':False,'action':action,'reason':'bad_action',
        'zones':None,'price':None,'zone':None,'source':'ohlc_entry_zones',
        'mtf_ok':None}
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
    if not zone:
        meta['reason']=f'wait_entry_zone (no_{action.lower()}_band)'
        return meta
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
    try:
        buf=max(float(tick or 0.01),0.)
    except (TypeError,ValueError):
        buf=0.01
    if price_in_zone(zone,price,buffer=buf):
        meta['allowed']=True
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
