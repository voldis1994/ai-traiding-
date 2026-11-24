"""OHLC structural entry — unified intent + trigger for Admin chart and LIVE OPEN.

1:1 zone reading (1m Supply/Demand):
  • Zone box = base (consolidation) High→Low immediately before impulse
  • Impulse UP → Demand (BUY); impulse DOWN → Supply (SELL)
  • Structure filter: HH+HL → Demand; LH+LL → Supply
  • Zone trigger needs price in zone + rejection OR micro structure change (V5 Z04/T06)
  • Invalidated zones (close through) are never selected (V3 Z03)

Also: break trigger (structure/range break + M1 tape) when not waiting pullback.

Gate knobs learn from closed broker £ via ``observe_close``.
Never invent mid ± κ.
"""
from __future__ import annotations

from . import structure_memory

STORE_KEY = 'ohlc_entry_gates_v1'

DEFAULTS = {
    'enabled': 1,
    # Band geometry (BUY discount / SELL premium width).
    # Cap stays tight so mid-range is NEVER both a BUY and SELL pullback —
    # inflated M5 ATR used to push band≈45% of span and erase structure.
    'band_span_frac': 0.12,
    'band_atr_frac': 0.35,
    'band_span_cap': 0.28,
    # Break tape / MTF agreement.
    'tape_need': 2,
    'break_mtf_conf': 0.55,
    'mtf_opposite_conf': 0.75,
    'zone_wide_conf': 0.50,
    'shift_block_conf': 0.60,
    # Rails.
    'band_span_frac_min': 0.08,
    'band_span_frac_max': 0.22,
    'band_atr_frac_min': 0.20,
    'band_atr_frac_max': 0.50,
    'band_span_cap_min': 0.18,
    'band_span_cap_max': 0.35,
    'tape_need_min': 1,
    'tape_need_max': 3,
    'break_mtf_conf_min': 0.45,
    'break_mtf_conf_max': 0.75,
    'mtf_opposite_conf_min': 0.60,
    'mtf_opposite_conf_max': 0.90,
    'zone_wide_conf_min': 0.40,
    'zone_wide_conf_max': 0.70,
    'shift_block_conf_min': 0.50,
    'shift_block_conf_max': 0.75,
}


def _clip(key, value):
    lo = DEFAULTS.get(f'{key}_min')
    hi = DEFAULTS.get(f'{key}_max')
    n = _finite(value)
    if n is None:
        return float(DEFAULTS[key])
    if lo is not None:
        n = max(float(lo), n)
    if hi is not None:
        n = min(float(hi), n)
    return float(n)


def load_params(store=None) -> dict:
    out = {k: DEFAULTS[k] for k in DEFAULTS if not k.endswith('_min') and not k.endswith('_max')}
    if store is None:
        return out
    try:
        from .self_params import get as sp_get
        out['enabled'] = int(sp_get(store, 'ohlc_entry_gates_enabled', 1))
        for k in ('band_span_frac', 'band_atr_frac', 'band_span_cap',
                  'break_mtf_conf', 'mtf_opposite_conf', 'zone_wide_conf',
                  'shift_block_conf'):
            raw = sp_get(store, f'ohlc_gate_{k}', None)
            if raw is not None:
                out[k] = float(raw)
        tn = sp_get(store, 'ohlc_gate_tape_need', None)
        if tn is not None:
            out['tape_need'] = int(tn)
    except Exception:
        pass
    bag = store.get(STORE_KEY) if store is not None else None
    if isinstance(bag, dict):
        for k in out:
            if bag.get(k) is not None:
                if k == 'tape_need' or k == 'enabled':
                    out[k] = int(bag[k])
                else:
                    out[k] = float(bag[k])
    # Clamp to rails.
    for k in ('band_span_frac', 'band_atr_frac', 'band_span_cap',
              'break_mtf_conf', 'mtf_opposite_conf', 'zone_wide_conf',
              'shift_block_conf'):
        out[k] = _clip(k, out[k])
    out['tape_need'] = int(round(_clip('tape_need', out['tape_need'])))
    out['enabled'] = int(out.get('enabled') or 0)
    return out


def save_params(store, params: dict):
    if store is None or not isinstance(params, dict):
        return
    clean = load_params(None)
    for k in clean:
        if params.get(k) is not None:
            clean[k] = params[k]
    for k in ('band_span_frac', 'band_atr_frac', 'band_span_cap',
              'break_mtf_conf', 'mtf_opposite_conf', 'zone_wide_conf',
              'shift_block_conf'):
        clean[k] = _clip(k, clean[k])
    clean['tape_need'] = int(round(_clip('tape_need', clean['tape_need'])))
    clean['enabled'] = int(clean.get('enabled') or 0)
    store.set(STORE_KEY, clean)


def open_meta_from_gate(gate: dict, *, action) -> dict:
    """Persist on entry_brain so close can teach OHLC gate knobs."""
    if not isinstance(gate, dict):
        return {}
    from . import ohlc_scenarios
    sc = gate.get('scenario') if isinstance(gate.get('scenario'), dict) else {}
    return {
        'action': action,
        'trigger': gate.get('trigger'),
        'reason': gate.get('reason'),
        'allowed': bool(gate.get('allowed')),
        'params': gate.get('params'),
        'break_reason': (gate.get('break') or {}).get('reason')
                        if isinstance(gate.get('break'), dict) else None,
        'zone': gate.get('zone'),
        'scenario': ohlc_scenarios.open_meta_from_scenario(sc),
    }


def observe_close(store, *, ohlc_meta=None, broker_pnl=None, spread=0.0):
    """Adapt band / break knobs from break|zone OPEN outcomes (after costs)."""
    meta = ohlc_meta if isinstance(ohlc_meta, dict) else {}
    if not meta:
        return {'adapted': False, 'reason': 'no_ohlc_meta'}
    trigger = str(meta.get('trigger') or '').lower()
    if trigger not in ('break', 'zone'):
        return {'adapted': False, 'reason': 'no_trigger'}
    pnl = _finite(broker_pnl)
    if pnl is None:
        return {'adapted': False, 'reason': 'no_pnl'}
    cost = abs(_finite(spread) or 0.0)
    useful = pnl > max(0.02, 0.25 * cost)
    hurt = pnl < -max(0.02, 0.25 * cost)
    if not useful and not hurt:
        return {'adapted': False, 'reason': 'scratch_band'}
    params = load_params(store)
    if not params.get('enabled'):
        return {'adapted': False, 'reason': 'disabled'}
    notes = []
    if trigger == 'break':
        if hurt:
            params['tape_need'] = int(round(_clip(
                'tape_need', int(params['tape_need']) + 1)))
            params['break_mtf_conf'] = _clip(
                'break_mtf_conf', float(params['break_mtf_conf']) + 0.02)
            params['band_span_frac'] = _clip(
                'band_span_frac', float(params['band_span_frac']) - 0.01)
            notes.append('tighten_break_after_loss')
        elif useful:
            params['break_mtf_conf'] = _clip(
                'break_mtf_conf', float(params['break_mtf_conf']) - 0.01)
            if int(params['tape_need']) > 1:
                params['tape_need'] = int(round(_clip(
                    'tape_need', int(params['tape_need']) - 1)))
            notes.append('ease_break_after_win')
    elif trigger == 'zone':
        if hurt:
            # Narrower band = more selective pullback touch.
            params['band_span_frac'] = _clip(
                'band_span_frac', float(params['band_span_frac']) - 0.01)
            params['band_atr_frac'] = _clip(
                'band_atr_frac', float(params['band_atr_frac']) - 0.02)
            params['mtf_opposite_conf'] = _clip(
                'mtf_opposite_conf', float(params['mtf_opposite_conf']) - 0.02)
            notes.append('narrow_zone_after_loss')
        elif useful:
            params['band_span_frac'] = _clip(
                'band_span_frac', float(params['band_span_frac']) + 0.005)
            params['zone_wide_conf'] = _clip(
                'zone_wide_conf', float(params['zone_wide_conf']) - 0.01)
            notes.append('widen_zone_after_win')
    if not notes:
        return {'adapted': False, 'reason': 'no_rule_hit'}
    save_params(store, params)
    hist = list((store.get(STORE_KEY + ':hist') or [])) if store is not None else []
    hist.append({
        'pnl': pnl, 'useful': useful, 'hurt': hurt, 'trigger': trigger,
        'notes': notes, 'params': {
            'band_span_frac': params['band_span_frac'],
            'band_atr_frac': params['band_atr_frac'],
            'tape_need': params['tape_need'],
            'break_mtf_conf': params['break_mtf_conf'],
            'mtf_opposite_conf': params['mtf_opposite_conf'],
        },
    })
    if store is not None:
        store.set(STORE_KEY + ':hist', hist[-40:])
        store.event('ohlc_gates', 'Adapted OHLC entry gates from outcome', {
            'notes': notes, 'trigger': trigger, 'pnl': pnl,
            'band_span_frac': params['band_span_frac'],
            'tape_need': params['tape_need'],
            'break_mtf_conf': params['break_mtf_conf'],
        })
    return {'adapted': True, 'notes': notes, 'params': params}



def _finite(v):
    try:
        x=float(v)
    except (TypeError,ValueError):
        return None
    return x if x==x and abs(x)!=float('inf') else None


def _m1_atr_from_candles(candles, period=14):
    """Wilder ATR on the same completed M1 bars that build swing zones."""
    rows=list(candles or [])
    if len(rows)<max(3,int(period)+1):
        return None
    try:
        from .mtf_momentum import atr_wilder
        atr=atr_wilder(rows,int(period))
    except Exception:
        return None
    return atr if atr is not None and atr>0 else None


def _entry_band_atr(mtf,candles):
    """ATR for *entry band width* — M1 scale, never M5 stop_hint.

    ``stop_hint_atr14`` is M5 noise for SL distance. Using it here made Gold
    bands swallow the swing (price always \"in zone\" → looks like OHLC ignored).
    """
    if isinstance(mtf,dict) and mtf.get('ready'):
        tf=(mtf.get('tf') or {}).get('m1') or {}
        atr=_finite(tf.get('atr14'))
        if atr is not None and atr>0:
            return atr
    return _m1_atr_from_candles(candles)


def _mtf_kama(mtf):
    if not isinstance(mtf,dict) or not mtf.get('ready'):
        return None
    tf=mtf.get('tf') or {}
    for key in ('m5','m1','m60'):
        k=_finite((tf.get(key) or {}).get('kama'))
        if k is not None:
            return k
    return None


def zone_invalidation_status(zone, candles, *, tick=0.01):
    """V3 Z03: Demand close < lo−tick / Supply close > hi+tick → INVALIDATED.

    Wick sweep alone is not close-invalidation. Invalidated zones keep their
    quality score but must never be selected for entry (Z03/Z04).
    """
    out = {
        'invalidated': False,
        'state': 'ACTIVE',
        'invalidated_at': None,
        'invalidation_reason': None,
    }
    if not isinstance(zone, dict):
        out['state'] = 'INVALIDATED'
        out['invalidated'] = True
        out['invalidation_reason'] = 'bad_zone'
        return out
    lo = _finite(zone.get('lo'))
    hi = _finite(zone.get('hi'))
    if lo is None or hi is None:
        out['state'] = 'INVALIDATED'
        out['invalidated'] = True
        out['invalidation_reason'] = 'bad_bounds'
        return out
    if hi < lo:
        lo, hi = hi, lo
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    kind = str(zone.get('kind') or '').lower()
    side = str(zone.get('side') or '').upper()
    if not kind:
        kind = 'demand' if side == 'BUY' else ('supply' if side == 'SELL' else '')
    # Only bars after the impulse (zone exists) can invalidate.
    rows = list(candles or [])
    start_i = 0
    impulse_end = zone.get('impulse_i1')
    try:
        if impulse_end is not None:
            start_i = max(0, int(impulse_end) + 1)
    except (TypeError, ValueError):
        start_i = 0
    for bar in rows[start_i:]:
        if not isinstance(bar, dict):
            continue
        c = _finite(bar.get('close'))
        if c is None:
            continue
        if kind == 'demand' and c < (lo - tick_f):
            out['invalidated'] = True
            out['state'] = 'INVALIDATED'
            out['invalidated_at'] = bar.get('at')
            out['invalidation_reason'] = 'demand_close_below'
            return out
        if kind == 'supply' and c > (hi + tick_f):
            out['invalidated'] = True
            out['state'] = 'INVALIDATED'
            out['invalidated_at'] = bar.get('at')
            out['invalidation_reason'] = 'supply_close_above'
            return out
    # Touch / confirm lifecycle hints (observability only).
    touches = int(zone.get('touches') or 0)
    if touches > 0:
        out['state'] = 'TOUCHED'
    return out


def _annotate_zone_lifecycle(zone, candles, *, tick=0.01):
    """Copy zone + Z03 identity/state fields (never mutates caller dict)."""
    if not isinstance(zone, dict):
        return None
    z = dict(zone)
    inv = zone_invalidation_status(z, candles, tick=tick)
    z['invalidated'] = bool(inv.get('invalidated'))
    z['state'] = inv.get('state') or 'ACTIVE'
    z['invalidated_at'] = inv.get('invalidated_at')
    z['invalidation_reason'] = inv.get('invalidation_reason')
    if not z.get('zone_id'):
        kind = str(z.get('kind') or z.get('side') or 'zone')
        at = z.get('at') or z.get('impulse_i1') or z.get('base_i0') or ''
        z['zone_id'] = f"{kind}:{z.get('lo')}:{z.get('hi')}:{at}"
    return z


def _pick_best_zone(cands, *, mid=None, candles=None, tick=0.01):
    """Prefer high-quality, fresh base; never select INVALIDATED (V3 Z03)."""
    rows = []
    for raw in (cands or []):
        if not isinstance(raw, dict):
            continue
        z = _annotate_zone_lifecycle(raw, candles, tick=tick)
        if not z or z.get('invalidated') or z.get('state') == 'INVALIDATED':
            continue
        rows.append(z)
    if not rows:
        return None
    m = _finite(mid)

    def _key(z):
        q = float(z.get('quality') or 0)
        if m is None:
            return (-q, int(z.get('touches') or 0))
        mid_z = 0.5 * (float(z['lo']) + float(z['hi']))
        return (-q, abs(mid_z - m), int(z.get('touches') or 0))

    rows.sort(key=_key)
    return rows[0]


def compute_entry_zones(candles,*,mid=None,tick=0.01,spread=0.0,mtf=None,
                        store=None,params=None):
    """Return Demand/Supply entry boxes from base→impulse OHLC (1:1 guide).

    Same geometry Admin draws and LIVE waits for. Structure HH/HL/LH/LL picks
    which side is active. Legacy discount/premium swing bands are gone.
    """
    cfg=params if isinstance(params,dict) else load_params(store)
    out={
        'ok':False,'reason':'no_candles',
        'support':None,'resistance':None,
        'buy_entry':None,'sell_entry':None,
        'range_lo':None,'range_hi':None,'band':None,
        'bias':None,'event':None,
        'atr14':None,'kama':None,'mtf_mode':None,'mtf_preferred':None,
        'info_stack':'ohlc_base_impulse',
        'structure':None,
        'demand_zones':[],'supply_zones':[],
        'params':{
            'band_span_frac':cfg.get('band_span_frac'),
            'band_atr_frac':cfg.get('band_atr_frac'),
            'band_span_cap':cfg.get('band_span_cap'),
        },
    }
    if not candles or len(candles)<20:
        return out
    bos=structure_memory.bos_choch(candles,left=2,right=2) or {}
    struct=structure_memory.label_swing_structure(candles,left=2,right=2) or {}
    live=structure_memory.snapshot_live(candles=candles,mid=mid) or {}
    atr=_entry_band_atr(mtf,candles)
    kama=_mtf_kama(mtf)
    out['atr14']=atr
    out['kama']=kama
    out['structure']={
        'bias':struct.get('bias'),
        'last_high_tag':struct.get('last_high_tag'),
        'last_low_tag':struct.get('last_low_tag'),
        'hh_count':struct.get('hh_count'),
        'hl_count':struct.get('hl_count'),
        'lh_count':struct.get('lh_count'),
        'll_count':struct.get('ll_count'),
    }
    out['bias']=struct.get('bias') or bos.get('bias') or live.get('live_bias')
    out['event']=bos.get('event') or live.get('live_event')
    if isinstance(mtf,dict) and mtf.get('ready'):
        out['mtf_mode']=mtf.get('mode')
        out['mtf_preferred']=mtf.get('preferred')
        out['info_stack']='ohlc_base_impulse+kama_adx_atr'
    det=structure_memory.detect_base_impulse_zones(candles,atr=atr,limit=80)
    demand=[_annotate_zone_lifecycle(z,candles,tick=tick)
            for z in (det.get('demand') or []) if isinstance(z,dict)]
    supply=[_annotate_zone_lifecycle(z,candles,tick=tick)
            for z in (det.get('supply') or []) if isinstance(z,dict)]
    demand=[z for z in demand if z]
    supply=[z for z in supply if z]
    out['demand_zones']=demand
    out['supply_zones']=supply
    out['invalidated_zones']=[
        z for z in (demand+supply)
        if z.get('invalidated') or z.get('state')=='INVALIDATED'
    ]
    bias=str(out.get('bias') or 'RANGE').upper()
    # Structure filter — UP trades Demand; DOWN trades Supply; RANGE keeps both.
    allow_buy = bias in ('UP', 'RANGE') or str(out.get('event') or '').endswith('_UP')
    allow_sell = bias in ('DOWN', 'RANGE') or str(out.get('event') or '').endswith('_DOWN')
    # CHOCH flips character: allow the new side even if last tags lag.
    event=str(out.get('event') or '').upper()
    if event in ('CHOCH_UP', 'BOS_UP'):
        allow_buy = True
    if event in ('CHOCH_DOWN', 'BOS_DOWN'):
        allow_sell = True
    buy_raw=_pick_best_zone(demand,mid=mid,candles=candles,tick=tick) if allow_buy else None
    sell_raw=_pick_best_zone(supply,mid=mid,candles=candles,tick=tick) if allow_sell else None
    src=out['info_stack']
    if buy_raw:
        out['buy_entry']={
            'lo':float(buy_raw['lo']),'hi':float(buy_raw['hi']),
            'label':'Demand zona (base→impulse UP)','side':'BUY',
            'source':src,'kind':'demand',
            'quality':buy_raw.get('quality'),
            'touches':buy_raw.get('touches'),
            'base_len':buy_raw.get('base_len'),
            'impulse_len':buy_raw.get('impulse_len'),
            'impulse_i1':buy_raw.get('impulse_i1'),
            'sl_anchor':float(buy_raw['lo']),
            'zone_id':buy_raw.get('zone_id'),
            'state':buy_raw.get('state') or 'ACTIVE',
            'invalidated':False,
            'invalidated_at':None,
            'invalidation_reason':None,
        }
        out['support']={
            'lo':float(buy_raw['lo']),'hi':float(buy_raw['hi']),
            'label':'Demand / base support'}
    if sell_raw:
        out['sell_entry']={
            'lo':float(sell_raw['lo']),'hi':float(sell_raw['hi']),
            'label':'Supply zona (base→impulse DOWN)','side':'SELL',
            'source':src,'kind':'supply',
            'quality':sell_raw.get('quality'),
            'touches':sell_raw.get('touches'),
            'base_len':sell_raw.get('base_len'),
            'impulse_len':sell_raw.get('impulse_len'),
            'impulse_i1':sell_raw.get('impulse_i1'),
            'sl_anchor':float(sell_raw['hi']),
            'zone_id':sell_raw.get('zone_id'),
            'state':sell_raw.get('state') or 'ACTIVE',
            'invalidated':False,
            'invalidated_at':None,
            'invalidation_reason':None,
        }
        out['resistance']={
            'lo':float(sell_raw['lo']),'hi':float(sell_raw['hi']),
            'label':'Supply / base resistance'}
    # range_lo/hi = active structural box (for break + chart context).
    if out['buy_entry'] and out['sell_entry']:
        out['range_lo']=float(out['buy_entry']['lo'])
        out['range_hi']=float(out['sell_entry']['hi'])
    elif out['buy_entry']:
        out['range_lo']=float(out['buy_entry']['lo'])
        out['range_hi']=float(out['buy_entry']['hi'])
    elif out['sell_entry']:
        out['range_lo']=float(out['sell_entry']['lo'])
        out['range_hi']=float(out['sell_entry']['hi'])
    else:
        # Fallback swing range so break path still has levels.
        lo=_finite(bos.get('range_lo'));hi=_finite(bos.get('range_hi'))
        out['range_lo']=lo;out['range_hi']=hi
    if out['buy_entry']:
        out['band']=float(out['buy_entry']['hi'])-float(out['buy_entry']['lo'])
    elif out['sell_entry']:
        out['band']=float(out['sell_entry']['hi'])-float(out['sell_entry']['lo'])
    # Dead mid between demand top and supply bottom when both exist.
    if out['buy_entry'] and out['sell_entry']:
        out['mid_dead']={
            'lo':float(out['buy_entry']['hi']),
            'hi':float(out['sell_entry']['lo']),
            'mid':0.5*(float(out['range_lo'] or 0)+float(out['range_hi'] or 0)),
        }
    out['ok']=bool(out['buy_entry'] or out['sell_entry'])
    if out['ok']:
        out['reason']='base_impulse'
    elif not det.get('ready'):
        out['reason']=det.get('reason') or 'no_base_impulse'
    else:
        out['reason']='structure_filtered'
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


def _candle_interacted_zone(bar, zone, *, buffer=0.0):
    if not isinstance(bar, dict) or not isinstance(zone, dict):
        return False
    lo = _finite(zone.get('lo')); hi = _finite(zone.get('hi'))
    bh = _finite(bar.get('high')); bl = _finite(bar.get('low'))
    if None in (lo, hi, bh, bl):
        return False
    try:
        buf = max(float(buffer or 0), 0.)
    except (TypeError, ValueError):
        buf = 0.
    return bl <= (hi + buf) and bh >= (lo - buf)


def rejection_candle(action, candles, zone, *, tick=0.01):
    """1m confirmation: pin / engulf rejecting the zone (guide step 3)."""
    action = (action or '').upper()
    rows = list(candles or [])
    out = {'ok': False, 'reason': 'no_bar', 'kind': None}
    if action not in ('BUY', 'SELL') or len(rows) < 1 or not zone:
        return out
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    last = rows[-1]
    o = _finite(last.get('open')); h = _finite(last.get('high'))
    l = _finite(last.get('low')); c = _finite(last.get('close'))
    if None in (o, h, l, c):
        return out
    if not _candle_interacted_zone(last, zone, buffer=tick_f):
        out['reason'] = 'no_zone_touch_on_bar'
        return out
    span = max(h - l, tick_f)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if action == 'BUY':
        # Bullish pin: long lower wick, close in upper half.
        pin = lower >= 0.45 * span and c >= (l + 0.55 * span) and lower >= 1.2 * max(body, tick_f)
        engulf = False
        if len(rows) >= 2:
            prev = rows[-2]
            po = _finite(prev.get('open')); pc = _finite(prev.get('close'))
            if po is not None and pc is not None and pc < po:
                engulf = c > o and c >= po and o <= pc
        if pin or engulf:
            out['ok'] = True
            out['kind'] = 'pin_bull' if pin else 'engulf_bull'
            out['reason'] = out['kind']
            return out
        out['reason'] = 'no_buy_rejection'
        return out
    # SELL — bearish pin / engulf
    pin = upper >= 0.45 * span and c <= (h - 0.55 * span) and upper >= 1.2 * max(body, tick_f)
    engulf = False
    if len(rows) >= 2:
        prev = rows[-2]
        po = _finite(prev.get('open')); pc = _finite(prev.get('close'))
        if po is not None and pc is not None and pc > po:
            engulf = c < o and c <= po and o >= pc
    if pin or engulf:
        out['ok'] = True
        out['kind'] = 'pin_bear' if pin else 'engulf_bear'
        out['reason'] = out['kind']
        return out
    out['reason'] = 'no_sell_rejection'
    return out


def micro_structure_confirm(action, candles, zone, *, tick=0.01):
    """Structure change on 1m while reacting in/near the zone (guide step 3)."""
    action = (action or '').upper()
    rows = list(candles or [])
    out = {'ok': False, 'reason': 'no_structure', 'event': None}
    if action not in ('BUY', 'SELL') or len(rows) < 12 or not zone:
        return out
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    # Last few bars must still be interacting with the zone context.
    near = False
    for b in rows[-4:]:
        if _candle_interacted_zone(b, zone, buffer=tick_f * 3):
            near = True
            break
    if not near:
        out['reason'] = 'structure_away_from_zone'
        return out
    bos = structure_memory.bos_choch(rows[-24:], left=2, right=2) or {}
    event = str(bos.get('event') or '').upper()
    struct = structure_memory.label_swing_structure(rows[-24:], left=2, right=2) or {}
    out['event'] = event or None
    out['bias'] = struct.get('bias')
    if action == 'BUY' and event in ('CHOCH_UP', 'BOS_UP'):
        out['ok'] = True
        out['reason'] = event.lower()
        return out
    if action == 'SELL' and event in ('CHOCH_DOWN', 'BOS_DOWN'):
        out['ok'] = True
        out['reason'] = event.lower()
        return out
    # Soft: last tags flipped into our side (HL after LL for BUY, LH after HH for SELL).
    if action == 'BUY' and struct.get('last_low_tag') == 'HL' and struct.get('bias') in ('UP', 'RANGE'):
        out['ok'] = True
        out['reason'] = 'hl_printed'
        return out
    if action == 'SELL' and struct.get('last_high_tag') == 'LH' and struct.get('bias') in ('DOWN', 'RANGE'):
        out['ok'] = True
        out['reason'] = 'lh_printed'
        return out
    out['reason'] = 'wait_structure_change'
    return out


def zone_confirm_allows(action, *, candles, zone, tick=0.01):
    """Zone OPEN needs rejection candle OR micro structure change (V5 Z04/T06).

    Rejection and microstructure stay as separate evidence; no universal AND
    filter. Invalidated zones are never confirmed (Z03).
    """
    # Never confirm an already-invalidated zone (Z03).
    if isinstance(zone, dict) and (
            zone.get('invalidated') or str(zone.get('state') or '').upper() == 'INVALIDATED'):
        return {
            'ok': False,
            'rejection': {'ok': False, 'reason': 'zone_invalidated', 'kind': None},
            'structure': {'ok': False, 'reason': 'zone_invalidated', 'event': None},
            'reason': 'zone_invalidated',
        }
    inv = zone_invalidation_status(zone, candles, tick=tick)
    if inv.get('invalidated'):
        return {
            'ok': False,
            'rejection': {'ok': False, 'reason': 'zone_invalidated', 'kind': None},
            'structure': {'ok': False, 'reason': 'zone_invalidated', 'event': None},
            'reason': 'zone_invalidated',
            'invalidation': inv,
        }
    rej = rejection_candle(action, candles, zone, tick=tick)
    st = micro_structure_confirm(action, candles, zone, tick=tick)
    ok = bool(rej.get('ok') or st.get('ok'))
    return {
        'ok': ok,
        'rejection': rej,
        'structure': st,
        'reason': (
            'rejection+' + str(rej.get('kind')) if rej.get('ok')
            else (st.get('reason') if st.get('ok') else 'wait_zone_confirm')
        ),
    }


def _mtf_allows_side(action,mtf,params=None):
    """Hard MTF filter: do not OPEN against a *strongly* opposing KAMA/ADX stack.

    Soft HOLD / mild cross no longer freezes entry when OHLC preferred agrees
    with the neural side — only hard ADX/spread blocks and high-confidence
    opposite preferred keep the wait. Opposite conf threshold is learned.
    """
    cfg=params if isinstance(params,dict) else DEFAULTS
    opp_conf=float(cfg.get('mtf_opposite_conf') or 0.75)
    shift_conf=float(cfg.get('shift_block_conf') or 0.60)
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
    # Only hard-block a *strong* opposite preferred.
    if (conf>=opp_conf and preferred in ('BUY','SELL') and preferred!=action
            and mode!='SHIFT'):
        return False,f'wait_mtf_conflict ({mode}/{preferred})'
    # Mild cross / soft HOLD — allow zone check (neural+OHLC decide).
    if mode=='HOLD':
        return True,'mtf_hold_soft'
    # SHIFT preferring the other side = early reverse — do not chase old side.
    if mode=='SHIFT' and preferred in ('BUY','SELL') and preferred!=action:
        if conf>=shift_conf:
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


def break_trigger_allows(action,*,price,zones,mtf=None,tick=0.01,candles=None,
                         params=None):
    """True when structure/range break aligns with neural side AND M1 tape.

    Unified path: intent + break → OPEN without pullback zone. Micro BOS alone
    must not BUY into a red dump (or SELL into a green spike). ``tape_need`` and
    ``break_mtf_conf`` learn from closed £.
    """
    cfg=params if isinstance(params,dict) else DEFAULTS
    tape_need=max(1,int(cfg.get('tape_need') or 2))
    break_conf=float(cfg.get('break_mtf_conf') or 0.55)
    action=(action or '').upper()
    out={'ok':False,'reason':'no_break','side':None,'trigger':'break','tape_streak':0,
         'tape_need':tape_need}
    if action not in ('BUY','SELL'):
        out['reason']='bad_action'
        return out
    p=_finite(price)
    if p is None:
        out['reason']='no_price'
        return out
    tape_ok,tape_streak=m1_tape_agrees(action,candles,need=tape_need)
    opp='SELL' if action=='BUY' else 'BUY'
    opp_ok,_opp_streak=m1_tape_agrees(opp,candles,need=tape_need)
    out['tape_streak']=tape_streak
    pref=str((mtf or {}).get('preferred') or '').upper() if isinstance(mtf,dict) else ''
    mode=str((mtf or {}).get('mode') or '').upper() if isinstance(mtf,dict) else ''
    try:
        conf=float((mtf or {}).get('confidence') or 0) if isinstance(mtf,dict) else 0.
    except (TypeError,ValueError):
        conf=0.
    mtf_aligned=((pref==action) or (mode==action)
                 or (mode=='SHIFT' and pref==action and conf>=break_conf))
    # Against a clear opposite M1 dump/spike — never break-chase the bounce.
    if opp_ok and not tape_ok:
        out['reason']='break_against_m1_tape'
        return out
    side=_structure_break_side(mtf,zones)
    out['side']=side
    want='UP' if action=='BUY' else 'DOWN'
    if side==want:
        # Micro structure break needs M1 pressure and/or strong MTF same side.
        if tape_ok or (mtf_aligned and conf>=break_conf):
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
        aligned=mtf_aligned or (mode=='SHIFT' and conf>=break_conf)
        if (action=='BUY' and aligned and hi is not None and last_c is not None
                and last_c>=(hi-buf) and p>=(hi-buf)
                and (tape_ok or conf>=break_conf)):
            out['ok']=True
            out['reason']='ohlc_break_range_high'
            return out
        if (action=='SELL' and aligned and lo is not None and last_c is not None
                and last_c<=(lo+buf) and p<=(lo+buf)
                and (tape_ok or conf>=break_conf)):
            out['ok']=True
            out['reason']='ohlc_break_range_low'
            return out
    return out


def entry_allows_open(action,*,candles,mid=None,ask=None,bid=None,
                      tick=0.01,spread=0.0,mtf=None,store=None,params=None):
    """Gate LIVE OPEN: zone touch OR structure/range break + MTF ok.

    BUY uses ask (or mid); SELL uses bid (or mid). Buffer = 1 tick.
    Learned knobs from ``store`` / ``params`` (band, tape, MTF conf).
    """
    cfg=params if isinstance(params,dict) else load_params(store)
    action=(action or '').upper()
    meta={
        'allowed':False,'action':action,'reason':'bad_action',
        'zones':None,'price':None,'zone':None,'source':'ohlc_entry_zones',
        'mtf_ok':None,'trigger':None,'intent':action if action in ('BUY','SELL') else None,
        'params':{
            'band_span_frac':cfg.get('band_span_frac'),
            'tape_need':cfg.get('tape_need'),
            'break_mtf_conf':cfg.get('break_mtf_conf'),
            'mtf_opposite_conf':cfg.get('mtf_opposite_conf'),
        }}
    if action not in ('BUY','SELL'):
        return meta
    mtf_ok,mtf_reason=_mtf_allows_side(action,mtf,params=cfg)
    meta['mtf_ok']=mtf_ok
    from . import ohlc_scenarios
    if not mtf_ok:
        meta['reason']=mtf_reason
        meta['zones']=compute_entry_zones(
            candles,mid=mid,tick=tick,spread=spread,mtf=mtf,store=store,params=cfg)
        meta['scenario']=ohlc_scenarios.classify(
            candles,mid=mid,zones=meta['zones'],tick=tick,store=store)
        return meta
    zones=compute_entry_zones(
        candles,mid=mid,tick=tick,spread=spread,mtf=mtf,store=store,params=cfg)
    meta['zones']=zones
    scenario=ohlc_scenarios.classify(
        candles,mid=mid,zones=zones,tick=tick,store=store)
    meta['scenario']=scenario
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
    # --- Trigger A: break (structure) — scenario may veto blind range break ---
    br=break_trigger_allows(
        action,price=price,zones=zones,mtf=mtf,tick=tick,candles=candles,
        params=cfg)
    meta['break']=br
    if br.get('ok'):
        # MTF/structure break already confirmed → treat as BREAKOUT tag so
        # RANGE "prefer retest" does not veto a real BOS.
        sc_for_break=dict(scenario or {})
        mtf_br=None
        if isinstance(mtf,dict):
            mtf_br=mtf.get('structure_break')
        if mtf_br in ('UP','DOWN') or str(br.get('reason') or '').startswith('ohlc_break'):
            tags=list(sc_for_break.get('scenarios') or [])
            tag=('BREAKOUT_UP' if action=='BUY' else 'BREAKOUT_DOWN')
            if tag not in tags:
                tags.append(tag)
            sc_for_break['scenarios']=tags
            if str(sc_for_break.get('scenario') or '') in ('RANGE','COMPRESSION','UNKNOWN'):
                sc_for_break['scenario']=tag
                sc_for_break['entry_hint']='BUY' if action=='BUY' else 'SELL'
        sc_ok=ohlc_scenarios.scenario_allows_trigger(
            action,sc_for_break,'break',store=store)
        meta['scenario_gate']=sc_ok
        if sc_ok.get('ok'):
            meta['allowed']=True
            meta['trigger']='break'
            meta['reason']=br.get('reason') or 'ohlc_break_trigger'
            return meta
        # Fall through to zone/retest wait instead of chasing.
        meta['break_blocked_by_scenario']=sc_ok.get('reason')
    # Guide: never open zone-path in the middle of a range / compression box.
    mid_block,mid_reason=ohlc_scenarios.mid_range_blocked(
        action,scenario,price,tick=tick)
    if mid_block:
        meta['reason']=mid_reason or 'wait_mid_range'
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
    wide_conf=float(cfg.get('zone_wide_conf') or 0.50)
    # Mild widen only — 3× used to erase the wait and look like OHLC ignored.
    if pref==action and conf>=wide_conf:
        buf=max(buf,float(tick or 0.01)*1.5)
        meta['wide_ohlc_touch']=True
    # --- Trigger B: pullback into Demand/Supply base + 1m confirm ---
    if not price_in_zone(zone,price,buffer=buf):
        lo=_finite(zone['lo']);hi=_finite(zone['hi'])
        if action=='BUY' and price>hi:
            meta['reason']=f'wait_buy_entry_zone (price {price:.5g} > zona {hi:.5g})'
        elif action=='SELL' and price<lo:
            meta['reason']=f'wait_sell_entry_zone (price {price:.5g} < zona {lo:.5g})'
        else:
            meta['reason']=f'wait_entry_zone (price {price:.5g} outside [{lo:.5g},{hi:.5g}])'
        return meta
    confirm=zone_confirm_allows(action,candles=candles,zone=zone,tick=tick)
    meta['confirm']=confirm
    if confirm.get('ok'):
        sc_ok=ohlc_scenarios.scenario_allows_trigger(
            action,scenario,'zone',store=store)
        meta['scenario_gate']=sc_ok
        if not sc_ok.get('ok'):
            meta['reason']=sc_ok.get('reason') or 'wait_scenario'
            return meta
        meta['allowed']=True
        meta['trigger']='zone'
        meta['reason']='ohlc_entry_zone_confirm'
        return meta
    meta['reason']=f"wait_zone_confirm ({confirm.get('reason') or 'rejection_or_structure'})"
    return meta
