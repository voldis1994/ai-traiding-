"""Adaptive KAMA + ADX/DI + ATR + Market Pressure + Multi-TF AI.

Operator-chosen start stack for GOLD testing (not free self-mutation every trade):

  KAMA        ER10 / Fast2 / Slow30     → adaptive trend (fast in move, slow in noise)
  ADX + DI    14 on M5/M15              → strength + directional dominance
  ATR         7 / 14 / 21               → volatility, stop hint, move size
  Pressure    3 / 5 / 10 M1 candles     → early direction change from OHLC
  Structure   M1 / M5                   → breaks, breakouts, corrections
  Semivariance up/down realized vol     → is volatility more up or down?
  AI weights  M1–M60                    → period importance (pre-tested modes)

Direction change (soft evidence — NeuralCore stays BUY/SELL/HOLD):
  1. BUY     — KAMA up, DI+ dominates, ADX confirms
  2. SHIFT   — M1 structure + normalized pressure flip (CLOSE without waiting M60)
  3. CLOSE   — confirmed opposite impulse (exit streak lowered in SHIFT)
  4. SELL/HOLD — SELL only if new direction confirmed and edge covers spread+slip
"""
from __future__ import annotations

import math

from .bracket_learn import resample_m1_ohlc

# ≥720 M1 → ≥12 calendar M60 bars so KAMA ER10 can adapt on H1 (was 480→8).
DEFAULT_MIN_M1_BARS = 720
ADX_MIN = 18.0
STORE_KEY = 'mtf_momentum_v1'

# Pre-tested mode presets — robot picks among these; does not invent free knobs mid-trade.
MODE_PRESETS = {
    'trend_follow': {'nudge': 0.10, 'adx_min': 18.0, 'spread_atr_mult': 0.35},
    'fast_shift': {'nudge': 0.12, 'adx_min': 16.0, 'spread_atr_mult': 0.40},
    'noise_hold': {'nudge': 0.08, 'adx_min': 22.0, 'spread_atr_mult': 0.50},
}


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
        'kama_er': 10,
        'kama_fast': 2,
        'kama_slow': 30,
        'pressure_windows': (3, 5, 10),
        'semivar_bars': 30,
        'mode_preset': 'trend_follow',
        'exec_slip_bps': 2.0,
    }
    if store is None:
        return _apply_preset(out)
    try:
        from .self_params import get as sp_get
        out['enabled'] = int(sp_get(store, 'mtf_momentum_enabled', 1))
        out['nudge'] = float(sp_get(store, 'mtf_momentum_nudge', 0.10))
        out['min_m1_bars'] = int(sp_get(store, 'mtf_min_m1_bars', DEFAULT_MIN_M1_BARS))
        out['adx_min'] = float(sp_get(store, 'mtf_adx_min', ADX_MIN))
        out['spread_atr_mult'] = float(sp_get(store, 'mtf_spread_atr_mult', 0.35))
        out['shift_exit_streak'] = int(sp_get(store, 'mtf_shift_exit_streak', 1))
        out['kama_er'] = int(sp_get(store, 'mtf_kama_er', 10))
        out['kama_fast'] = int(sp_get(store, 'mtf_kama_fast', 2))
        out['kama_slow'] = int(sp_get(store, 'mtf_kama_slow', 30))
        out['semivar_bars'] = int(sp_get(store, 'mtf_semivar_bars', 30))
        out['mode_preset'] = str(sp_get(store, 'mtf_mode_preset', 'trend_follow') or 'trend_follow')
        out['exec_slip_bps'] = float(sp_get(store, 'exec_slip_bps', 2.0))
    except Exception:
        pass
    return _apply_preset(out)


def _apply_preset(cfg: dict) -> dict:
    """Clamp live knobs to a pre-tested mode — no free per-trade mutation."""
    preset = MODE_PRESETS.get(str(cfg.get('mode_preset') or ''), MODE_PRESETS['trend_follow'])
    # Soft blend: stored nudge stays inside preset ±20% rails.
    for k, v in preset.items():
        cur = _finite(cfg.get(k))
        if cur is None:
            cfg[k] = v
            continue
        lo, hi = float(v) * 0.8, float(v) * 1.2
        if k == 'adx_min':
            lo, hi = float(v) - 4.0, float(v) + 4.0
        cfg[k] = min(hi, max(lo, cur))
    return cfg


def ema_series(closes, span: int) -> list[float]:
    if not closes:
        return []
    k = 2.0 / (max(1, int(span)) + 1)
    out = [float(closes[0])]
    for c in closes[1:]:
        out.append(out[-1] + k * (float(c) - out[-1]))
    return out


def kama_series(closes, er_period: int = 10, fast: int = 2, slow: int = 30) -> list[float]:
    """Kaufman Adaptive Moving Average — fast in clear trends, slow in noise."""
    n = len(closes)
    if n == 0:
        return []
    er_n = max(1, int(er_period))
    fast_sc = 2.0 / (max(1, int(fast)) + 1)
    slow_sc = 2.0 / (max(1, int(slow)) + 1)
    out = [float(closes[0])] * n
    for i in range(1, n):
        if i < er_n:
            # Warmup: simple EMA toward close with slow SC.
            out[i] = out[i - 1] + slow_sc * (float(closes[i]) - out[i - 1])
            continue
        change = abs(float(closes[i]) - float(closes[i - er_n]))
        volatility = 0.0
        for j in range(i - er_n + 1, i + 1):
            volatility += abs(float(closes[j]) - float(closes[j - 1]))
        er = 0.0 if volatility <= 1e-12 else change / volatility
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        out[i] = out[i - 1] + sc * (float(closes[i]) - out[i - 1])
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


def atr_bundle(bars) -> dict:
    return {
        'atr7': atr_wilder(bars, 7),
        'atr14': atr_wilder(bars, 14),
        'atr21': atr_wilder(bars, 21),
    }


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


def market_pressure(bars, window: int) -> float | None:
    """Normalized OHLC pressure over last N completed candles ∈ [-1, 1].

    Positive = buyers dominating candle bodies; negative = sellers.
    """
    w = max(1, int(window))
    if len(bars) < w:
        return None
    chunk = bars[-w:]
    up = down = 0.0
    for b in chunk:
        o, h, l, c = float(b['open']), float(b['high']), float(b['low']), float(b['close'])
        span = max(h - l, 1e-12)
        body = c - o
        # Body share of range, signed.
        up += max(body, 0.0) / span
        down += max(-body, 0.0) / span
        # Close location in range (buying/selling pressure).
        loc = (c - l) / span
        up += loc
        down += (1.0 - loc)
    tot = up + down
    if tot <= 1e-12:
        return 0.0
    return float((up - down) / tot)


def pressure_bundle(bars, windows=(3, 5, 10)) -> dict:
    out = {}
    vals = []
    for w in windows:
        p = market_pressure(bars, w)
        out[f'p{w}'] = p
        if p is not None:
            vals.append(p)
    out['mean'] = float(sum(vals) / len(vals)) if vals else None
    # Flip signal: short window disagrees with longer (early turn).
    p3, p10 = out.get('p3'), out.get('p10')
    out['flip'] = None
    if p3 is not None and p10 is not None:
        if p3 < -0.15 and p10 > 0.05:
            out['flip'] = 'SELL'
        elif p3 > 0.15 and p10 < -0.05:
            out['flip'] = 'BUY'
    return out


def realized_semivariance(closes, bars: int = 30) -> dict:
    """Upside / downside realized semivariance from log returns.

    Lets the AI see whether volatility is more up-moves or down-moves
    (intraday momentum / reversal literature — test on GOLD M1, not assumed).
    """
    n = max(2, int(bars))
    if len(closes) < n + 1:
        return {'rs_up': None, 'rs_down': None, 'rs_share_up': None, 'n': 0}
    use = [float(c) for c in closes[-(n + 1):]]
    rs_up = rs_down = 0.0
    count = 0
    for i in range(1, len(use)):
        if use[i - 1] <= 0 or use[i] <= 0:
            continue
        r = math.log(use[i] / use[i - 1])
        if not math.isfinite(r):
            continue
        count += 1
        if r > 0:
            rs_up += r * r
        elif r < 0:
            rs_down += r * r
    tot = rs_up + rs_down
    share = None if tot <= 1e-18 else rs_up / tot
    return {
        'rs_up': rs_up if count else None,
        'rs_down': rs_down if count else None,
        'rs_share_up': share,
        'n': count,
    }


def three_candle_break(bars_m1: list) -> dict:
    """M1 structure break — prefer swing BOS/CHoCH, fall back to 3-candle H/L."""
    from . import structure_memory
    bos = structure_memory.bos_choch(bars_m1, left=2, right=2)
    event = bos.get('event')
    br = None
    if event in ('BOS_UP', 'CHOCH_UP'):
        br = 'UP'
    elif event in ('BOS_DOWN', 'CHOCH_DOWN'):
        br = 'DOWN'
    prior_high = bos.get('range_hi')
    prior_low = bos.get('range_lo')
    close = bos.get('last_close')
    if br is None and len(bars_m1) >= 4:
        # Legacy micro-break when swings are not ready yet.
        prior = bars_m1[-4:-1]
        last = bars_m1[-1]
        prior_high = max(float(b['high']) for b in prior)
        prior_low = min(float(b['low']) for b in prior)
        close = float(last['close'])
        if close > prior_high:
            br = 'UP'
        elif close < prior_low:
            br = 'DOWN'
    return {
        'break': br, 'prior_high': prior_high, 'prior_low': prior_low,
        'close': close, 'bos_event': event, 'bos_bias': bos.get('bias'),
        'retest': bos.get('retest'), 'false_break': bool(bos.get('false_break')),
    }


def price_structure_bias(bars: list) -> str | None:
    """Swing bias (BOS/CHoCH) on higher-TF bars; fallback to sequential HH/HL."""
    from . import structure_memory
    bos = structure_memory.bos_choch(bars, left=1, right=1)
    bias = bos.get('bias')
    if bias in ('UP', 'DOWN'):
        return bias
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


def period_weights(*, adx_v, pressure_flip, structure_break, kama_slope, strong) -> dict:
    """Diagnostic heuristic only — NOT the decision source.

    True M1→M60 importance is learned inside NeuralCore.period_logits (same
    train chain as BUY/SELL/HOLD). This formula is UI/fallback evidence only.
    """
    w = {'m1': 0.20, 'm5': 0.25, 'm15': 0.25, 'm30': 0.15, 'm60': 0.15}
    if pressure_flip or structure_break:
        w = {'m1': 0.40, 'm5': 0.25, 'm15': 0.15, 'm30': 0.10, 'm60': 0.10}
    elif strong and adx_v is not None and adx_v >= 25:
        w = {'m1': 0.10, 'm5': 0.20, 'm15': 0.30, 'm30': 0.15, 'm60': 0.25}
    elif kama_slope is not None and abs(kama_slope) < 1e-6:
        w = {'m1': 0.15, 'm5': 0.20, 'm15': 0.20, 'm30': 0.20, 'm60': 0.25}
    s = sum(w.values()) or 1.0
    return {k: round(v / s, 4) for k, v in w.items()}


def period_evidence(analysis: dict | None) -> list[float]:
    """Per-TF directional evidence ∈ [-1, 1] for NeuralCore period gate.

    Order: m1, m5, m15, m30, m60. Positive = bullish lean. The *weights* that
    mix these channels are learned by NeuralCore — not chosen here.
    """
    from .core import PERIOD_N
    out = [0.0] * PERIOD_N
    if not isinstance(analysis, dict) or not analysis.get('ready'):
        return out
    tf = analysis.get('tf') or {}
    m1, m5, m15, m30, m60 = (tf.get(k) or {} for k in ('m1', 'm5', 'm15', 'm30', 'm60'))

    # M1: structure break + pressure mean
    br = m1.get('structure_break')
    pmean = (m1.get('pressure') or {}).get('mean')
    e1 = 0.0
    if br == 'UP':
        e1 += 0.6
    elif br == 'DOWN':
        e1 -= 0.6
    if pmean is not None:
        e1 += 0.5 * float(np_clip(pmean, -1, 1))
    out[0] = float(np_clip(e1, -1, 1))

    # M5: KAMA slope / EMA
    e5 = 0.0
    if m5.get('kama_up'):
        e5 += 0.7
    elif m5.get('kama_down'):
        e5 -= 0.7
    elif m5.get('ema_up'):
        e5 += 0.4
    elif m5.get('ema_down'):
        e5 -= 0.4
    slope = _finite(m5.get('kama_slope'))
    if slope is not None:
        e5 += float(np_clip(slope * 50.0, -0.3, 0.3))
    out[1] = float(np_clip(e5, -1, 1))

    # M15: DI dominance scaled by ADX strength
    e15 = 0.0
    if m15.get('di_buy'):
        e15 += 0.6
    elif m15.get('di_sell'):
        e15 -= 0.6
    adx = _finite(m15.get('adx'))
    if adx is not None:
        e15 *= float(np_clip(adx / 40.0, 0.25, 1.25))
    out[2] = float(np_clip(e15, -1, 1))

    # M30: ATR-normalized momentum
    mom = _finite(m30.get('mom_atr'))
    out[3] = float(np_clip(mom if mom is not None else 0.0, -1, 1))

    # M60: KAMA / structure bias
    e60 = 0.0
    if m60.get('bias_up'):
        e60 += 0.7
    elif m60.get('bias_down'):
        e60 -= 0.7
    s60 = _finite(m60.get('kama_slope') if m60.get('kama_slope') is not None else m60.get('ema20_slope'))
    if s60 is not None:
        e60 += float(np_clip(s60 * 20.0, -0.3, 0.3))
    out[4] = float(np_clip(e60, -1, 1))
    return out


def np_clip(v, lo, hi):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(n):
        return 0.0
    return max(lo, min(hi, n))


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
    """Compute KAMA/ADX/ATR/Pressure/Structure/Semivar stack + mode."""
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
        'stack': 'kama_adx_atr_pressure',
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
    if len(m1) < 40 or len(m5) < 35 or len(m15) < 30:
        return dict(base, reason='higher_tf_warmup_incomplete',
                    bars_m1=len(rows), tf={
                        'm1': len(m1), 'm5': len(m5), 'm15': len(m15),
                        'm30': len(m30), 'm60': len(m60),
                    })

    # --- KAMA (adaptive trend on M5; M1 for early slope) ---
    c1 = [float(b['close']) for b in m1]
    c5 = [float(b['close']) for b in m5]
    kama_m5 = kama_series(c5, cfg['kama_er'], cfg['kama_fast'], cfg['kama_slow'])
    kama_m1 = kama_series(c1, cfg['kama_er'], cfg['kama_fast'], cfg['kama_slow'])
    kama_slope = None
    if len(kama_m5) >= 4:
        kama_slope = kama_m5[-1] - kama_m5[-4]
    kama_up = kama_slope is not None and kama_slope > 0
    kama_down = kama_slope is not None and kama_slope < 0
    kama_rising = len(kama_m5) >= 2 and kama_m5[-1] > kama_m5[-2]

    # --- Structure M1 / M5 ---
    br = three_candle_break(m1)
    struct_m5 = price_structure_bias(m5)
    # Legacy EMA retained as secondary (compat with older tests/UI).
    e5 = ema_series(c1, 5)
    e13 = ema_series(c1, 13)
    e9 = ema_series(c5, 9)
    e21 = ema_series(c5, 21)
    m1_up = e5[-1] > e13[-1]
    m5_up = e9[-1] > e21[-1]
    m5_down = e9[-1] < e21[-1]

    # --- Pressure 3/5/10 ---
    pressure = pressure_bundle(m1, cfg['pressure_windows'])

    # --- ATR 7/14/21 (M5 primary for stops / move size) ---
    atrs = atr_bundle(m5)
    atr14 = atrs.get('atr14')

    # --- ADX + DI on M5 and M15 ---
    adx5 = adx_di(m5, 14) or {}
    adx15 = adx_di(m15, 14) or {}
    adx_v = _finite(adx15.get('adx'))
    if adx_v is None:
        adx_v = _finite(adx5.get('adx'))
    di_p = _finite(adx15.get('di_plus'))
    if di_p is None:
        di_p = _finite(adx5.get('di_plus'))
    di_m = _finite(adx15.get('di_minus'))
    if di_m is None:
        di_m = _finite(adx5.get('di_minus'))
    adx_min = float(cfg['adx_min'])
    strong = adx_v is not None and adx_v >= adx_min
    di_buy = di_p is not None and di_m is not None and di_p > di_m
    di_sell = di_p is not None and di_m is not None and di_m > di_p

    # --- Semivariance (M1) ---
    semivar = realized_semivariance(c1, cfg['semivar_bars'])

    # --- M30 / M60 context ---
    atr30 = atr_wilder(m30, 14) if len(m30) >= 16 else None
    mom30 = None
    if len(m30) >= 2 and atr30 and atr30 > 0:
        mom30 = (float(m30[-1]['close']) - float(m30[-2]['close'])) / atr30
    c60 = [float(b['close']) for b in m60] if m60 else []
    kama60 = kama_series(c60, cfg['kama_er'], cfg['kama_fast'], cfg['kama_slow']) if len(c60) >= 5 else []
    slope60 = (kama60[-1] - kama60[-3]) if len(kama60) >= 3 else None
    struct60 = price_structure_bias(m60) if m60 else None
    m60_up = (slope60 is not None and slope60 > 0) or struct60 == 'UP'
    m60_down = (slope60 is not None and slope60 < 0) or struct60 == 'DOWN'

    close = float(m1[-1]['close'])
    spr = _finite(spread) or 0.0
    slip = max(0.0, float(cfg.get('exec_slip_bps') or 0)) * close / 10000.0
    cost = spr + slip
    # Expected move (ATR14) must cover spread + slippage * mult.
    move_ok = True
    if atr14 is not None and atr14 > 0 and close > 0:
        move_ok = atr14 >= cost * float(cfg['spread_atr_mult'])
    spread_blocked = not move_ok and cost > 0

    weights = period_weights(
        adx_v=adx_v,
        pressure_flip=pressure.get('flip'),
        structure_break=br.get('break'),
        kama_slope=kama_slope,
        strong=strong,
    )

    # --- Mode: BUY → SHIFT(pressure) → CLOSE path → SELL/HOLD ---
    mode = 'HOLD'
    preferred = 'HOLD'
    conf = 0.35
    reason = 'signals_conflict_or_weak'
    p_flip = pressure.get('flip')
    p_mean = pressure.get('mean')

    # 2) Pressure / structure turn — SHIFT (CLOSE without waiting M60).
    if (br.get('break') == 'DOWN' or p_flip == 'SELL') and (kama_up or m5_up or m60_up):
        mode, preferred, conf = 'SHIFT', 'SELL', 0.74
        reason = 'pressure_or_structure_flip_sell'
    elif (br.get('break') == 'UP' or p_flip == 'BUY') and (kama_down or m5_down or m60_down):
        mode, preferred, conf = 'SHIFT', 'BUY', 0.74
        reason = 'pressure_or_structure_flip_buy'
    elif spread_blocked or (adx_v is not None and adx_v < adx_min * 0.75):
        mode, preferred, conf = 'HOLD', 'HOLD', 0.55
        reason = 'spread_slip_or_weak_adx' if spread_blocked else 'weak_adx'
    # 1) BUY trend: KAMA up + DI+ + ADX
    elif (kama_up or kama_rising) and strong and di_buy and move_ok:
        mode, preferred = 'BUY', 'BUY'
        conf = 0.55 + min(0.35, (adx_v - adx_min) / 100.0)
        if m60_up:
            conf = min(0.95, conf + 0.06)
        if p_mean is not None and p_mean > 0:
            conf = min(0.95, conf + 0.04)
        if semivar.get('rs_share_up') is not None and semivar['rs_share_up'] > 0.55:
            conf = min(0.95, conf + 0.04)
        reason = 'kama_up_di_plus_adx_confirms'
    # 4) SELL only if confirmed + edge covers costs
    elif (kama_down or (not kama_rising and m5_down)) and strong and di_sell and move_ok:
        mode, preferred = 'SELL', 'SELL'
        conf = 0.55 + min(0.35, (adx_v - adx_min) / 100.0)
        if m60_down:
            conf = min(0.95, conf + 0.06)
        if p_mean is not None and p_mean < 0:
            conf = min(0.95, conf + 0.04)
        if semivar.get('rs_share_up') is not None and semivar['rs_share_up'] < 0.45:
            conf = min(0.95, conf + 0.04)
        reason = 'kama_down_confirmed_edge_covers_costs'
    elif not strong or (di_buy and kama_down) or (di_sell and kama_up):
        mode, preferred, conf = 'HOLD', 'HOLD', 0.5
        reason = 'tf_conflict_or_weak_trend'

    stop_hint = None
    if atr14 is not None and close > 0:
        # Soft stop distance suggestion (ATR14) — brackets still own LIVE SL.
        stop_hint = round(float(atr14), 6)

    atr_m1 = atr_wilder(m1, 14) if len(m1) >= 16 else None
    tf_m1 = {
        'kama': kama_m1[-1] if kama_m1 else None,
        'ema5': e5[-1], 'ema13': e13[-1], 'ema_up': m1_up,
        'atr14': atr_m1,
        'structure_break': br.get('break'),
        'prior_high': br.get('prior_high'), 'prior_low': br.get('prior_low'),
        'bos_event': br.get('bos_event'),
        'bos_bias': br.get('bos_bias'),
        'retest': br.get('retest'),
        'false_break': br.get('false_break'),
        'pressure': pressure,
        'semivariance': semivar,
    }
    tf_m5 = {
        'kama': kama_m5[-1] if kama_m5 else None,
        'kama_slope': kama_slope,
        'kama_up': kama_up, 'kama_down': kama_down,
        'ema9': e9[-1], 'ema21': e21[-1], 'ema_up': m5_up, 'ema_down': m5_down,
        'structure': struct_m5,
        'atr': atrs,
        'atr14': atr14,
        'adx': _finite(adx5.get('adx')),
        'di_plus': _finite(adx5.get('di_plus')),
        'di_minus': _finite(adx5.get('di_minus')),
    }
    tf_m15 = {
        'adx': _finite(adx15.get('adx')),
        'di_plus': _finite(adx15.get('di_plus')),
        'di_minus': _finite(adx15.get('di_minus')),
        'strong': strong, 'di_buy': di_buy, 'di_sell': di_sell,
    }
    tf_m30 = {'atr14': atr30, 'mom_atr': mom30}
    tf_m60 = {
        'kama': kama60[-1] if kama60 else None,
        'kama_slope': slope60,
        'ema20_slope': slope60,  # compat key for older persist/UI
        'structure': struct60,
        'bias_up': bool(m60_up and not m60_down),
        'bias_down': bool(m60_down and not m60_up),
    }

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
        'stack': 'kama_adx_atr_pressure',
        'spread': spr,
        'slip_price': round(slip, 6),
        'cost': round(cost, 6),
        'move_ok': move_ok,
        'stop_hint_atr14': stop_hint,
        # Heuristic diagnostic only — NeuralCore.period_weights() is the learned source.
        'period_weights_heuristic': weights,
        'period_weights': weights,
        'mode_preset': cfg.get('mode_preset'),
        'tf': {
            'm1': tf_m1, 'm5': tf_m5, 'm15': tf_m15,
            'm30': tf_m30, 'm60': tf_m60,
            'counts': {
                'm1': len(m1), 'm5': len(m5), 'm15': len(m15),
                'm30': len(m30), 'm60': len(m60),
            },
        },
        'hierarchy': 'KAMA=trend ADX=strength ATR=noise Pressure=reversal; NeuralCore learns TF weights',
    }
    out['period_evidence'] = period_evidence(out)
    return out


def soft_nudge_scores(scores, analysis, *, store=None, live_trend=None):
    """Tilt BUY/SELL/HOLD toward MTF preferred mode. SHIFT boosts reverse for CLOSE.

    When live multi-TF trend already flipped but KAMA lag still prefers the old
    side (and mode is not SHIFT), skip the reinforce — do not glue sticky SELL
    into an UP tape. SHIFT keeps authority (pressure/structure early flip).
    """
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
    trend = (live_trend or '').upper() if isinstance(live_trend, str) else None
    if (mode != 'SHIFT' and preferred in ('BUY', 'SELL') and trend in ('UP', 'DOWN')
            and ((preferred == 'SELL' and trend == 'UP')
                 or (preferred == 'BUY' and trend == 'DOWN'))):
        return dict(scores), {
            'nudged': False, 'reason': 'live_trend_disagrees',
            'mode': mode, 'target': preferred, 'live_trend': trend,
        }
    # Mode tilt only — period importance is learned inside NeuralCore, not here.
    delta = float(cfg['nudge']) * conf
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    if mode == 'SHIFT' and preferred in ('BUY', 'SELL'):
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
        # Do not glue perpetual HOLD when neural already leans BUY/SELL —
        # Admin was stuck HOLD·HOLD while scores showed a clear side.
        buy = float(scores.get('BUY') or 0)
        sell = float(scores.get('SELL') or 0)
        hold = float(scores.get('HOLD') or 0)
        neural_side = 'BUY' if buy >= sell else 'SELL'
        neural_edge = float(scores.get(neural_side) or 0) - hold
        if neural_edge >= 0.05:
            return dict(scores), {
                'nudged': False,
                'reason': 'skip_hold_nudge_clear_neural',
                'neural_side': neural_side,
                'mode': mode,
                'preferred': preferred,
                'neural_edge': neural_edge,
            }
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
    """SHIFT/pressure flip: evaluate CLOSE sooner — do not wait for M60 trend flip."""
    cfg = _cfg(store)
    if (isinstance(analysis, dict) and analysis.get('ready')
            and str(analysis.get('mode') or '').upper() == 'SHIFT'):
        return max(1, int(cfg.get('shift_exit_streak') or 1))
    return max(1, int(default))


def persist(store, analysis: dict) -> None:
    if store is None or not isinstance(analysis, dict):
        return
    try:
        m5 = (analysis.get('tf') or {}).get('m5') or {}
        m1 = (analysis.get('tf') or {}).get('m1') or {}
        slim = {
            'mode': analysis.get('mode'),
            'preferred': analysis.get('preferred'),
            'confidence': analysis.get('confidence'),
            'reason': analysis.get('reason'),
            'ready': analysis.get('ready'),
            'bars_m1': analysis.get('bars_m1'),
            'stack': analysis.get('stack'),
            'stop_hint_atr14': analysis.get('stop_hint_atr14'),
            'period_weights': analysis.get('period_weights'),
            'tf': {
                'kama_up': m5.get('kama_up'),
                'm5_up': m5.get('ema_up'),
                'm15_adx': ((analysis.get('tf') or {}).get('m15') or {}).get('adx'),
                'm1_break': m1.get('structure_break'),
                'pressure_flip': (m1.get('pressure') or {}).get('flip'),
                'rs_share_up': (m1.get('semivariance') or {}).get('rs_share_up'),
                'm60_slope': ((analysis.get('tf') or {}).get('m60') or {}).get('kama_slope'),
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


def chart_overlays(candles, *, store=None, epic: str | None = None,
                   display_n: int = 180, spread: float = 0.0) -> dict:
    """Compact KAMA/EMA/ADX/ATR overlay for Admin OHLC — same stack as LIVE.

    Series are aligned to the last ``display_n`` M1 candles (one value per bar).
    Scalars come from ``analyze`` when history is warm enough.
    """
    rows = _ohlc_tuples_to_dicts(candles) if candles else []
    n = max(20, min(int(display_n or 180), 240))
    out = {
        'ok': False, 'reason': 'no_candles', 'n': 0,
        'kama': [], 'ema5': [], 'ema13': [],
        'kama_level': None, 'ema5_level': None, 'ema13_level': None,
        'atr14': None, 'adx': None, 'di_plus': None, 'di_minus': None,
        'mode': None, 'preferred': None, 'confidence': None, 'mtf_reason': None,
        'stop_hint_atr14': None, 'stack': 'kama_adx_atr_pressure',
    }
    if len(rows) < 20:
        return out
    cfg = _cfg(store)
    closes = [float(b['close']) for b in rows]
    kama = kama_series(closes, cfg['kama_er'], cfg['kama_fast'], cfg['kama_slow'])
    e5 = ema_series(closes, 5)
    e13 = ema_series(closes, 13)
    slice_n = min(n, len(rows))
    out['kama'] = [round(float(v), 6) for v in kama[-slice_n:]]
    out['ema5'] = [round(float(v), 6) for v in e5[-slice_n:]]
    out['ema13'] = [round(float(v), 6) for v in e13[-slice_n:]]
    out['n'] = slice_n
    out['kama_level'] = out['kama'][-1] if out['kama'] else None
    out['ema5_level'] = out['ema5'][-1] if out['ema5'] else None
    out['ema13_level'] = out['ema13'][-1] if out['ema13'] else None
    out['ok'] = True
    out['reason'] = 'series_only'
    analysis = analyze(rows, spread=spread, store=store, epic=epic)
    if isinstance(analysis, dict) and analysis.get('ready'):
        tf = analysis.get('tf') or {}
        m5 = tf.get('m5') or {}
        m15 = tf.get('m15') or {}
        out['atr14'] = _finite(m5.get('atr14')) or _finite(analysis.get('stop_hint_atr14'))
        out['adx'] = _finite(m15.get('adx')) or _finite(m5.get('adx'))
        out['di_plus'] = _finite(m15.get('di_plus')) or _finite(m5.get('di_plus'))
        out['di_minus'] = _finite(m15.get('di_minus')) or _finite(m5.get('di_minus'))
        out['mode'] = analysis.get('mode')
        out['preferred'] = analysis.get('preferred')
        out['confidence'] = analysis.get('confidence')
        out['mtf_reason'] = analysis.get('reason')
        out['stop_hint_atr14'] = analysis.get('stop_hint_atr14')
        out['reason'] = 'mtf_ready'
        out['analysis'] = {
            'ready': True,
            'mode': analysis.get('mode'),
            'preferred': analysis.get('preferred'),
            'confidence': analysis.get('confidence'),
            'reason': analysis.get('reason'),
            'move_ok': analysis.get('move_ok'),
            'enabled': analysis.get('enabled'),
            'stop_hint_atr14': analysis.get('stop_hint_atr14'),
            'tf': {
                'm1': {'kama': (tf.get('m1') or {}).get('kama')},
                'm5': {
                    'kama': m5.get('kama'), 'atr14': m5.get('atr14'),
                    'adx': m5.get('adx'), 'di_plus': m5.get('di_plus'),
                    'di_minus': m5.get('di_minus'),
                },
                'm15': {
                    'adx': m15.get('adx'), 'di_plus': m15.get('di_plus'),
                    'di_minus': m15.get('di_minus'),
                    'strong': m15.get('strong'),
                },
            },
        }
    else:
        out['analysis'] = analysis if isinstance(analysis, dict) else {
            'ready': False, 'reason': 'analyze_failed'}
        if isinstance(analysis, dict):
            out['mtf_reason'] = analysis.get('reason')
            out['mode'] = analysis.get('mode') or 'HOLD'
        # Partial ADX/ATR for Admin while full 720-bar MTF warms up — avoids
        # footer flashing "ADX 0 · ATR 0.00" when analyze is not ready yet.
        m5 = resample_m1_ohlc(rows, 5)
        m15 = resample_m1_ohlc(rows, 15)
        if len(m5) >= 30:
            atr5 = atr_wilder(m5, 14)
            a5 = adx_di(m5, 14)
            if atr5 is not None:
                out['atr14'] = float(atr5)
                out['stop_hint_atr14'] = float(atr5)
            if a5:
                out['adx'] = _finite(a5.get('adx'))
                out['di_plus'] = _finite(a5.get('di_plus'))
                out['di_minus'] = _finite(a5.get('di_minus'))
        if out.get('adx') is None and len(m15) >= 30:
            a15 = adx_di(m15, 14)
            if a15:
                out['adx'] = _finite(a15.get('adx'))
                out['di_plus'] = _finite(a15.get('di_plus'))
                out['di_minus'] = _finite(a15.get('di_minus'))
        if out.get('atr14') is not None or out.get('adx') is not None:
            out['reason'] = 'series_atr_adx_partial'
    return out
