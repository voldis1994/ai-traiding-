"""Multi-timeframe Momentum + Market Structure + ADX + ATR.

Starting stack for GOLD testing (operator-chosen — not yet self-tuned):

  M1  EMA 5/13 + 3-candle structure break   → react first
  M5  EMA 9/21 + ATR 14                     → confirm direction
  M15 ADX 14 + DI+/DI-                      → strength
  M30 ATR-normalized momentum               → medium tendency
  M60 EMA 20 slope + price structure       → context

Modes (UI / soft evidence — NeuralCore stays BUY/SELL/HOLD):
  BUY   — M5 up + M15 strength, AI prefers BUY
  SHIFT — M1 structure break against bias; evaluate CLOSE without waiting M60
  SELL  — M5 down impulse + move > costs
  HOLD  — conflict / weak trend / spread too wide

Does not invent RSI/MACD. Soft-nudges scores like structure_edge; never a hard veto.
"""
from __future__ import annotations

import math
from typing import Any

from .bracket_learn import resample_m1_ohlc

# Enough M1 history to pack M15/M30/M60 indicators with Warmup headroom.
# Capital price history is capped elsewhere; this is the MTF readiness floor.
DEFAULT_MIN_M1_BARS = 480
ADX_MIN = 18.0
STORE_KEY = 'mtf_momentum_v1'


def _finite(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _cfg(store=None) -> dict:
    out = {
        'enabled': 1,
        'nudge': 0.10,
        'min_m1_bars': DEFAULT_MIN_M1_BARS,
        'adx_min': ADX_MIN,
        'spread_atr_mult': 0.35,
        'shift_exit_streak': 1,
    }
    if store is None:
        return out
    try:
        from .self_params import get as sp_get
        out['enabled'] = int(sp_get(store, 'mtf_momentum_enabled', 1))
        out['nudge'] = float(sp_get(store, 'mtf_momentum_nudge', 0.10))
        out['min_m1_bars'] = int(sp_get(store, 'mtf_min_m1_bars', DEFAULT_MIN_M1_BARS))
        out['adx_min'] = float(sp_get(store, 'mtf_adx_min', ADX_MIN))
        out['spread_atr_mult'] = float(sp_get(store, 'mtf_spread_atr_mult', 0.35))
        out['shift_exit_streak'] = int(sp_get(store, 'mtf_shift_exit_streak', 1))
    except Exception:
        pass
    return out


def ema_series(closes, span: int) -> list[float]:
    if not closes:
        return []
    k = 2.0 / (max(1, int(span)) + 1)
    out = [float(closes[0])]
    for c in closes[1:]:
        out.append(out[-1] + k * (float(c) - out[-1]))
    return out


def atr_wilder(bars, period: int = 14) -> float | None:
    """Wilder ATR on OHLC dict bars. Needs period+1 bars."""
    n = max(1, int(period))
    if len(bars) < n + 1:
        return None
    trs = []
    prev_c = float(bars[0]['close'])
    for b in bars[1:]:
        h, l, c = float(b['high']), float(b['low']), float(b['close'])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr if math.isfinite(atr) and atr > 0 else None


def adx_di(bars, period: int = 14) -> dict | None:
    """Wilder ADX + DI+/DI- on OHLC dict bars."""
    n = max(1, int(period))
    if len(bars) < n * 2:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        h, l = float(bars[i]['high']), float(bars[i]['low'])
        ph, pl = float(bars[i - 1]['high']), float(bars[i - 1]['low'])
        pc = float(bars[i - 1]['close'])
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None

    def wilder_smooth(vals):
        s = sum(vals[:n])
        out = [s]
        for v in vals[n:]:
            s = s - s / n + v
            out.append(s)
        return out

    atr_s = wilder_smooth(trs)
    plus_s = wilder_smooth(plus_dm)
    minus_s = wilder_smooth(minus_dm)
    dx = []
    for a, p, m in zip(atr_s, plus_s, minus_s):
        if a <= 1e-12:
            dx.append(0.0)
            continue
        pdi = 100.0 * p / a
        mdi = 100.0 * m / a
        denom = pdi + mdi
        dx.append(0.0 if denom <= 1e-12 else 100.0 * abs(pdi - mdi) / denom)
    if len(dx) < n:
        return None
    adx = sum(dx[:n]) / n
    for v in dx[n:]:
        adx = (adx * (n - 1) + v) / n
    a_last = atr_s[-1]
    if a_last <= 1e-12:
        return None
    di_plus = 100.0 * plus_s[-1] / a_last
    di_minus = 100.0 * minus_s[-1] / a_last
    return {
        'adx': float(adx),
        'di_plus': float(di_plus),
        'di_minus': float(di_minus),
    }


def three_candle_break(bars_m1: list) -> dict:
    """M1: last close vs prior 3-candle high/low structure."""
    if len(bars_m1) < 4:
        return {'break': None, 'prior_high': None, 'prior_low': None}
    prior = bars_m1[-4:-1]
    last = bars_m1[-1]
    hi = max(float(b['high']) for b in prior)
    lo = min(float(b['low']) for b in prior)
    c = float(last['close'])
    br = None
    if c > hi:
        br = 'UP'
    elif c < lo:
        br = 'DOWN'
    return {'break': br, 'prior_high': hi, 'prior_low': lo, 'close': c}


def price_structure_bias(bars: list) -> str | None:
    """HH/HL vs LH/LL on recent higher-TF bars."""
    if len(bars) < 4:
        return None
    recent = bars[-4:]
    ups = downs = 0
    for i in range(1, len(recent)):
        if float(recent[i]['high']) > float(recent[i - 1]['high']) and float(recent[i]['low']) >= float(recent[i - 1]['low']):
            ups += 1
        elif float(recent[i]['low']) < float(recent[i - 1]['low']) and float(recent[i]['high']) <= float(recent[i - 1]['high']):
            downs += 1
    if ups > downs:
        return 'UP'
    if downs > ups:
        return 'DOWN'
    return 'MIXED'


def _ohlc_tuples_to_dicts(bars_tuples) -> list:
    """Accept list of (o,h,l,c) or dict OHLC."""
    out = []
    for i, b in enumerate(bars_tuples or []):
        if isinstance(b, dict):
            try:
                out.append({
                    'at': b.get('at') or str(i),
                    'open': float(b['open']), 'high': float(b['high']),
                    'low': float(b['low']), 'close': float(b['close']),
                    'spread': b.get('spread'),
                })
            except (TypeError, ValueError, KeyError):
                continue
        elif isinstance(b, (list, tuple)) and len(b) >= 4:
            try:
                o, h, l, c = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            except (TypeError, ValueError):
                continue
            out.append({'at': str(i), 'open': o, 'high': h, 'low': l, 'close': c})
    return out


def analyze(candles, *, spread: float = 0.0, store=None, epic: str | None = None) -> dict:
    """Compute MTF stack + mode from completed M1 OHLC rows."""
    cfg = _cfg(store)
    rows = _ohlc_tuples_to_dicts(candles)
    need = max(61, int(cfg['min_m1_bars']))
    base = {
        'ready': False,
        'mode': 'HOLD',
        'preferred': 'HOLD',
        'confidence': 0.0,
        'reason': 'insufficient_m1_history',
        'epic': epic,
        'bars_m1': len(rows),
        'need_m1': need,
        'enabled': bool(cfg['enabled']),
        'tf': {},
    }
    if not cfg['enabled']:
        return dict(base, reason='mtf_disabled', ready=True, mode='HOLD')
    if len(rows) < need:
        return dict(base, reason=f'need>={need} M1 bars ({len(rows)} available)')

    m1 = resample_m1_ohlc(rows, 1)
    m5 = resample_m1_ohlc(rows, 5)
    m15 = resample_m1_ohlc(rows, 15)
    m30 = resample_m1_ohlc(rows, 30)
    m60 = resample_m1_ohlc(rows, 60)
    if len(m1) < 20 or len(m5) < 25 or len(m15) < 30:
        return dict(base, reason='higher_tf_warmup_incomplete',
                    bars_m1=len(rows), tf={
                        'm1': len(m1), 'm5': len(m5), 'm15': len(m15),
                        'm30': len(m30), 'm60': len(m60),
                    })

    # --- M1 ---
    c1 = [float(b['close']) for b in m1]
    e5 = ema_series(c1, 5)
    e13 = ema_series(c1, 13)
    br = three_candle_break(m1)
    m1_up = e5[-1] > e13[-1]
    tf_m1 = {
        'ema5': e5[-1], 'ema13': e13[-1], 'ema_up': m1_up,
        'structure_break': br.get('break'),
        'prior_high': br.get('prior_high'), 'prior_low': br.get('prior_low'),
    }

    # --- M5 ---
    c5 = [float(b['close']) for b in m5]
    e9 = ema_series(c5, 9)
    e21 = ema_series(c5, 21)
    atr5 = atr_wilder(m5, 14)
    m5_up = e9[-1] > e21[-1]
    m5_down = e9[-1] < e21[-1]
    tf_m5 = {
        'ema9': e9[-1], 'ema21': e21[-1], 'ema_up': m5_up, 'ema_down': m5_down,
        'atr14': atr5,
    }

    # --- M15 ---
    adx = adx_di(m15, 14) or {}
    adx_v = _finite(adx.get('adx'))
    di_p = _finite(adx.get('di_plus'))
    di_m = _finite(adx.get('di_minus'))
    adx_min = float(cfg['adx_min'])
    strong = adx_v is not None and adx_v >= adx_min
    di_buy = di_p is not None and di_m is not None and di_p > di_m
    di_sell = di_p is not None and di_m is not None and di_m > di_p
    tf_m15 = {
        'adx': adx_v, 'di_plus': di_p, 'di_minus': di_m,
        'strong': strong, 'di_buy': di_buy, 'di_sell': di_sell,
    }

    # --- M30 ---
    atr30 = atr_wilder(m30, 14) if len(m30) >= 16 else None
    mom30 = None
    if len(m30) >= 2 and atr30 and atr30 > 0:
        mom30 = (float(m30[-1]['close']) - float(m30[-2]['close'])) / atr30
    tf_m30 = {'atr14': atr30, 'mom_atr': mom30}

    # --- M60 ---
    c60 = [float(b['close']) for b in m60] if m60 else []
    e20 = ema_series(c60, 20) if len(c60) >= 5 else []
    slope = None
    if len(e20) >= 3:
        slope = e20[-1] - e20[-3]
    struct60 = price_structure_bias(m60) if m60 else None
    m60_up = (slope is not None and slope > 0) or struct60 == 'UP'
    m60_down = (slope is not None and slope < 0) or struct60 == 'DOWN'
    tf_m60 = {
        'ema20': e20[-1] if e20 else None,
        'ema20_slope': slope,
        'structure': struct60,
        'bias_up': bool(m60_up and not m60_down),
        'bias_down': bool(m60_down and not m60_up),
    }

    close = float(m1[-1]['close'])
    spr = _finite(spread) or 0.0
    # Expected move proxy: M5 ATR; costs = spread. Require move > cost * mult.
    move_ok = True
    if atr5 is not None and atr5 > 0 and close > 0:
        move_ok = atr5 >= max(spr, 0.0) * float(cfg['spread_atr_mult'])
    spread_blocked = not move_ok and spr > 0

    # --- Mode selection (hierarchy: M1 reacts, M5 confirms, M15 strength, M60 context) ---
    mode = 'HOLD'
    preferred = 'HOLD'
    conf = 0.35
    reason = 'signals_conflict_or_weak'

    # SHIFT: M1 structure break against M5/M60 bias — react before M60 flips.
    if br.get('break') == 'DOWN' and (m5_up or tf_m60.get('bias_up')):
        mode, preferred, conf = 'SHIFT', 'SELL', 0.72
        reason = 'm1_structure_break_down_against_bias'
    elif br.get('break') == 'UP' and (m5_down or tf_m60.get('bias_down')):
        mode, preferred, conf = 'SHIFT', 'BUY', 0.72
        reason = 'm1_structure_break_up_against_bias'
    elif spread_blocked or (adx_v is not None and adx_v < adx_min * 0.75):
        mode, preferred, conf = 'HOLD', 'HOLD', 0.55
        reason = 'spread_or_weak_adx' if spread_blocked else 'weak_adx'
    elif m5_up and strong and di_buy and move_ok:
        mode, preferred = 'BUY', 'BUY'
        conf = 0.55 + min(0.35, (adx_v - adx_min) / 100.0)
        if tf_m60.get('bias_up'):
            conf = min(0.95, conf + 0.08)
        if mom30 is not None and mom30 > 0:
            conf = min(0.95, conf + 0.05)
        reason = 'm5_ema_up_m15_adx_confirms'
    elif m5_down and strong and di_sell and move_ok:
        mode, preferred = 'SELL', 'SELL'
        conf = 0.55 + min(0.35, (adx_v - adx_min) / 100.0)
        if tf_m60.get('bias_down'):
            conf = min(0.95, conf + 0.08)
        if mom30 is not None and mom30 < 0:
            conf = min(0.95, conf + 0.05)
        reason = 'm5_ema_down_impulse_covers_costs'
    elif not strong or (di_buy and m5_down) or (di_sell and m5_up):
        mode, preferred, conf = 'HOLD', 'HOLD', 0.5
        reason = 'tf_conflict_or_weak_trend'

    out = {
        'ready': True,
        'mode': mode,
        'preferred': preferred,
        'confidence': round(float(conf), 4),
        'reason': reason,
        'epic': epic,
        'bars_m1': len(rows),
        'need_m1': need,
        'enabled': True,
        'spread': spr,
        'move_ok': move_ok,
        'tf': {
            'm1': tf_m1, 'm5': tf_m5, 'm15': tf_m15,
            'm30': tf_m30, 'm60': tf_m60,
            'counts': {
                'm1': len(m1), 'm5': len(m5), 'm15': len(m15),
                'm30': len(m30), 'm60': len(m60),
            },
        },
        'hierarchy': 'M60=context M15=strength M5=direction M1=react',
    }
    return out


def soft_nudge_scores(scores, analysis, *, store=None):
    """Tilt BUY/SELL/HOLD toward MTF preferred mode. SHIFT boosts opposite side + HOLD damp."""
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    if not isinstance(analysis, dict) or not analysis.get('ready'):
        return dict(scores), {'nudged': False, 'reason': 'mtf_not_ready'}
    cfg = _cfg(store)
    if not cfg['enabled'] or float(cfg['nudge']) <= 0:
        return dict(scores), {'nudged': False, 'reason': 'mtf_disabled'}
    mode = str(analysis.get('mode') or 'HOLD').upper()
    preferred = str(analysis.get('preferred') or 'HOLD').upper()
    conf = float(analysis.get('confidence') or 0)
    if conf < 0.4:
        return dict(scores), {'nudged': False, 'reason': 'weak_confidence', 'mode': mode}
    delta = float(cfg['nudge']) * conf
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    if mode == 'SHIFT' and preferred in ('BUY', 'SELL'):
        # Turning: damp current conviction HOLD, lift reverse side for CLOSE/re-entry.
        out[preferred] = out.get(preferred, 0) + delta
        out['HOLD'] = out.get('HOLD', 0) - 0.25 * delta
        other = 'SELL' if preferred == 'BUY' else 'BUY'
        out[other] = out.get(other, 0) - 0.35 * delta
    elif preferred in ('BUY', 'SELL'):
        out[preferred] = out.get(preferred, 0) + delta
        other = 'SELL' if preferred == 'BUY' else 'BUY'
        out[other] = out.get(other, 0) - 0.45 * delta
        out['HOLD'] = out.get('HOLD', 0) - 0.25 * delta
    elif preferred == 'HOLD' or mode == 'HOLD':
        out['HOLD'] = out.get('HOLD', 0) + delta
        out['BUY'] = out.get('BUY', 0) - 0.35 * delta
        out['SELL'] = out.get('SELL', 0) - 0.35 * delta
    else:
        return dict(scores), {'nudged': False, 'reason': 'no_target', 'mode': mode}
    for k in out:
        out[k] = max(0.0, float(out[k]))
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate'}
    out = {k: v / total for k, v in out.items()}
    return out, {
        'nudged': True, 'mode': mode, 'target': preferred,
        'delta': delta, 'confidence': conf, 'reason': analysis.get('reason'),
        'renormalized': True,
    }


def exit_need_streak(analysis, *, store=None, default: int = 2) -> int:
    """SHIFT: evaluate CLOSE sooner — do not wait for M60 EMA flip."""
    cfg = _cfg(store)
    if (isinstance(analysis, dict) and analysis.get('ready')
            and str(analysis.get('mode') or '').upper() == 'SHIFT'):
        return max(1, int(cfg.get('shift_exit_streak') or 1))
    return max(1, int(default))


def persist(store, analysis: dict) -> None:
    if store is None or not isinstance(analysis, dict):
        return
    try:
        slim = {
            'mode': analysis.get('mode'),
            'preferred': analysis.get('preferred'),
            'confidence': analysis.get('confidence'),
            'reason': analysis.get('reason'),
            'ready': analysis.get('ready'),
            'bars_m1': analysis.get('bars_m1'),
            'tf': {
                'm5_up': ((analysis.get('tf') or {}).get('m5') or {}).get('ema_up'),
                'm15_adx': ((analysis.get('tf') or {}).get('m15') or {}).get('adx'),
                'm1_break': ((analysis.get('tf') or {}).get('m1') or {}).get('structure_break'),
                'm60_slope': ((analysis.get('tf') or {}).get('m60') or {}).get('ema20_slope'),
            },
        }
        store.set(STORE_KEY, slim)
    except Exception:
        pass


def load(store) -> dict | None:
    if store is None:
        return None
    try:
        raw = store.get(STORE_KEY)
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def min_history_bars(store=None) -> int:
    """Floor for LIVE OHLC fetch so M30/M60 indicators can warm up."""
    return max(61, int(_cfg(store)['min_m1_bars']))
