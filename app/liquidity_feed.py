"""Public multi-source gold liquidity context (volume + shallow DOM).

Capital.com has no true volume/DOM — these feeds are EXTERNAL evidence only.
Never invent bars or books. Missing/stale sources are omitted; features return
None unless enough real observations exist for the target completed M1.

Sources (HTTPS, no API keys):
  1. Yahoo Finance GC=F — COMEX gold futures M1 volume (primary futures tape)
  2. Yahoo Finance GLD  — gold ETF M1 volume (US cash session confirm)
  3. Coinbase PAXG-USD  — spot gold token M1 volume + L2 book imbalance (24/7)

Fill / SL / £ remain on Capital. This module only builds bounded features.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .market_context import candle_time

YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
COINBASE_CANDLES = 'https://api.exchange.coinbase.com/products/PAXG-USD/candles'
COINBASE_BOOK = 'https://api.exchange.coinbase.com/products/PAXG-USD/book'
OKX_CANDLES = 'https://www.okx.com/api/v5/market/candles'

USER_AGENT = 'NeuralTradingAI/1.0 (+liquidity-context; not for Capital fill)'
MAX_AGE_SEC = 180  # snapshot older than this is not used for LIVE features
MIN_VOLUME_BARS = 20


LIQUIDITY_NAMES = (
    'liq_vol_z_primary',       # strongest available volume z (prefer GC=F)
    'liq_vol_z_confirm',       # second independent volume z
    'liq_vol_impulse',         # primary vol vs its median (−1..1)
    'liq_book_imbalance',      # Coinbase PAXG L2 size imbalance
    'liq_book_micro_bps',      # microprice vs mid, scaled
    'liq_source_coverage',     # fraction of volume+book slots that contributed
)


def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _zscore(values, current):
    """Bounded z for one completed bar vs recent history (no look-ahead)."""
    hist = [v for v in values if v is not None and math.isfinite(v) and v >= 0]
    cur = _finite(current)
    if cur is None or cur < 0 or len(hist) < MIN_VOLUME_BARS:
        return None
    mu = sum(hist) / len(hist)
    var = sum((x - mu) ** 2 for x in hist) / len(hist)
    sd = math.sqrt(var)
    if sd < 1e-12:
        return 0.0 if abs(cur - mu) < 1e-12 else None
    return max(-3.0, min(3.0, (cur - mu) / sd)) / 3.0


def _impulse(values, current):
    hist = [v for v in values if v is not None and math.isfinite(v) and v >= 0]
    cur = _finite(current)
    if cur is None or cur < 0 or len(hist) < MIN_VOLUME_BARS:
        return None
    ordered = sorted(hist)
    med = ordered[len(ordered) // 2]
    if med <= 0:
        return 0.0 if cur <= 0 else None
    raw = math.log1p(cur) / math.log1p(med) - 1.0
    return max(-1.0, min(1.0, raw))


def _parse_yahoo_chart(payload, *, symbol):
    try:
        result = payload['chart']['result'][0]
        stamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        volumes = quote['volume']
        closes = quote.get('close') or []
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError(f'Yahoo {symbol} chart malformed') from exc
    if not isinstance(stamps, list) or not stamps or len(stamps) != len(volumes):
        raise ValueError(f'Yahoo {symbol} length mismatch')
    bars = []
    for i, ts in enumerate(stamps):
        vol = volumes[i]
        if vol is None:
            continue
        try:
            ts_i = int(ts)
            vol_f = float(vol)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vol_f) or vol_f < 0:
            continue
        close = None
        if i < len(closes) and closes[i] is not None:
            close = _finite(closes[i])
        bars.append({
            't': datetime.fromtimestamp(ts_i, tz=timezone.utc).replace(second=0, microsecond=0).isoformat(),
            'ts': ts_i,
            'volume': vol_f,
            'close': close,
        })
    if len(bars) < MIN_VOLUME_BARS:
        raise ValueError(f'Yahoo {symbol} too few volume bars')
    meta = result.get('meta') or {}
    return {
        'symbol': symbol,
        'source': 'yahoo_finance',
        'bars': bars[-120:],
        'regular_market_time': int(meta['regularMarketTime']) if meta.get('regularMarketTime') else None,
    }


def _parse_coinbase_candles(rows):
    # Coinbase: [time, low, high, open, close, volume], newest first.
    if not isinstance(rows, list) or not rows:
        raise ValueError('Coinbase candles empty')
    bars = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts_i = int(row[0])
            vol_f = float(row[5])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vol_f) or vol_f < 0 or not math.isfinite(close):
            continue
        bars.append({
            't': datetime.fromtimestamp(ts_i, tz=timezone.utc).replace(second=0, microsecond=0).isoformat(),
            'ts': ts_i,
            'volume': vol_f,
            'close': close,
        })
    bars.sort(key=lambda b: b['ts'])
    if len(bars) < MIN_VOLUME_BARS:
        raise ValueError('Coinbase PAXG too few volume bars')
    return {'symbol': 'PAXG-USD', 'source': 'coinbase', 'bars': bars[-120:]}


def _parse_okx_candles(payload):
    if not isinstance(payload, dict) or payload.get('code') != '0':
        raise ValueError('OKX candles rejected')
    rows = payload.get('data') or []
    bars = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            # OKX ms timestamp, vol at index 5
            ts_i = int(int(row[0]) // 1000)
            vol_f = float(row[5])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vol_f) or vol_f < 0:
            continue
        bars.append({
            't': datetime.fromtimestamp(ts_i, tz=timezone.utc).replace(second=0, microsecond=0).isoformat(),
            'ts': ts_i,
            'volume': vol_f,
            'close': close,
        })
    bars.sort(key=lambda b: b['ts'])
    if len(bars) < MIN_VOLUME_BARS:
        raise ValueError('OKX PAXG too few volume bars')
    return {'symbol': 'PAXG-USDT', 'source': 'okx', 'bars': bars[-120:]}


def _parse_coinbase_book(payload, *, top_n=10):
    try:
        bids = payload['bids']
        asks = payload['asks']
    except (KeyError, TypeError) as exc:
        raise ValueError('Coinbase book malformed') from exc
    if not bids or not asks:
        raise ValueError('Coinbase book empty')
    bid_sz = ask_sz = 0.0
    bid_px = ask_px = None
    for i, level in enumerate(bids[:top_n]):
        px = _finite(level[0]); sz = _finite(level[1])
        if px is None or sz is None or sz < 0:
            continue
        bid_sz += sz
        if bid_px is None:
            bid_px = px
    for i, level in enumerate(asks[:top_n]):
        px = _finite(level[0]); sz = _finite(level[1])
        if px is None or sz is None or sz < 0:
            continue
        ask_sz += sz
        if ask_px is None:
            ask_px = px
    if bid_px is None or ask_px is None or bid_sz + ask_sz <= 0:
        raise ValueError('Coinbase book unusable')
    mid = 0.5 * (bid_px + ask_px)
    if mid <= 0:
        raise ValueError('Coinbase mid invalid')
    imbalance = (bid_sz - ask_sz) / (bid_sz + ask_sz)
    micro = (bid_px * ask_sz + ask_px * bid_sz) / (bid_sz + ask_sz)
    micro_bps = max(-5.0, min(5.0, (micro - mid) / mid * 10000.0)) / 5.0
    spread_bps = max(0.0, (ask_px - bid_px) / mid * 10000.0)
    return {
        'source': 'coinbase',
        'symbol': 'PAXG-USD',
        'bid': bid_px,
        'ask': ask_px,
        'imbalance': max(-1.0, min(1.0, imbalance)),
        'micro_bps': micro_bps,
        'spread_bps': spread_bps,
        'levels': top_n,
    }


def _https_ok(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password


async def _get_json(client, url, *, params=None):
    if not _https_ok(url):
        raise ValueError('URL must be HTTPS without credentials')
    response = await client.get(url, params=params, headers={'User-Agent': USER_AGENT})
    response.raise_for_status()
    if len(response.content) > 4_000_000:
        raise ValueError('payload too large')
    return response.json()


def _series_at(bars, bar_time_iso):
    """Return (history_volumes_before_or_at_bar, volume_at_bar) for completed M1."""
    target = candle_time(bar_time_iso)
    if target is None or not bars:
        return None, None
    target_iso = target.replace(second=0, microsecond=0).isoformat()
    # Prefer exact minute match; else nearest completed bar not after target.
    exact = None
    prior = []
    for b in bars:
        bt = candle_time(b['t'])
        if bt is None:
            continue
        iso = bt.replace(second=0, microsecond=0).isoformat()
        if iso < target_iso:
            prior.append(b['volume'])
        elif iso == target_iso:
            exact = b['volume']
            prior.append(b['volume'])
    if exact is None:
        # Allow ≤90s provider lag: use last bar at or before target only.
        if not prior:
            return None, None
        return prior[:-1], prior[-1]
    return prior[:-1], exact


def _fresh_enough(series, *, now_ts, max_lag_sec=600):
    if not series or not series.get('bars'):
        return False
    last_ts = series['bars'][-1]['ts']
    return abs(int(now_ts) - int(last_ts)) <= max_lag_sec


def build_liquidity_features(snapshot, bar_time, *, now=None):
    """Six bounded EXTERNAL features or None — never pads missing sources with zeros."""
    if not isinstance(snapshot, dict) or not snapshot.get('ok'):
        return None
    target = candle_time(bar_time)
    if target is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return None
    updated = candle_time(snapshot.get('updated_at'))
    if updated is None:
        return None
    age = abs((now.astimezone(timezone.utc) - updated).total_seconds())
    if age > MAX_AGE_SEC:
        return None

    series_list = []
    for key, prefer in (('gc_f', 0), ('paxg_coinbase', 1), ('paxg_okx', 2), ('gld', 3)):
        series = snapshot.get(key)
        if not isinstance(series, dict) or not series.get('bars'):
            continue
        hist, cur = _series_at(series['bars'], bar_time)
        if hist is None or cur is None:
            continue
        # Skip long-stale ETF when cash session is closed (GLD).
        if key == 'gld':
            last_ts = series['bars'][-1]['ts']
            if abs(now.timestamp() - last_ts) > 6 * 3600:
                continue
        z = _zscore(hist, cur)
        imp = _impulse(hist, cur)
        if z is None:
            continue
        series_list.append({'key': key, 'prefer': prefer, 'z': z, 'impulse': imp, 'volume': cur})

    book = snapshot.get('paxg_book')
    book_ok = isinstance(book, dict) and _finite(book.get('imbalance')) is not None
    if len(series_list) < 1 or not book_ok:
        return None

    series_list.sort(key=lambda s: s['prefer'])
    primary = series_list[0]
    # Prefer two independent volume tapes. One volume + live book is allowed with
    # lower coverage — confirm_z stays 0 only when no second tape exists (coverage
    # tells the net it is unconfirmed; we do not clone primary into confirm).
    if len(series_list) >= 2:
        confirm_z = float(series_list[1]['z'])
        confirm_present = True
    else:
        confirm_z = 0.0
        confirm_present = False

    impulse = primary['impulse']
    if impulse is None:
        impulse = 0.0

    imbalance = float(book['imbalance'])
    micro = float(book.get('micro_bps') or 0.0)
    slots = 3  # primary vol, confirm vol, book
    filled = 1 + (1 if confirm_present else 0) + 1
    coverage = filled / slots

    out = [
        float(primary['z']),
        float(confirm_z),
        float(impulse),
        float(imbalance),
        float(micro),
        float(coverage),
    ]
    if not all(math.isfinite(x) for x in out):
        return None
    if not (-1.0 <= out[0] <= 1.0 and -1.0 <= out[1] <= 1.0 and -1.0 <= out[2] <= 1.0):
        return None
    if not (-1.0 <= out[3] <= 1.0 and -1.0 <= out[4] <= 1.0 and 0.0 <= out[5] <= 1.0):
        return None
    return out


async def refresh_liquidity(path, *, client=None, now=None):
    """Fetch public gold volume/DOM snapshots to a local JSON file."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = Path(path)
    own = client is None
    errors = []
    snapshot = {
        'ok': False,
        'updated_at': now.astimezone(timezone.utc).isoformat(),
        'sources': [],
        'errors': [],
    }
    try:
        if own:
            client = httpx.AsyncClient(timeout=12, follow_redirects=True)
        # Yahoo GC=F
        try:
            url = YAHOO_CHART.format(symbol='GC=F')
            raw = await _get_json(client, url, params={'interval': '1m', 'range': '1d'})
            snapshot['gc_f'] = _parse_yahoo_chart(raw, symbol='GC=F')
            snapshot['sources'].append('yahoo:GC=F')
        except Exception as exc:
            errors.append('GC=F:' + type(exc).__name__ + ':' + str(exc)[:80])
        # Yahoo GLD
        try:
            url = YAHOO_CHART.format(symbol='GLD')
            raw = await _get_json(client, url, params={'interval': '1m', 'range': '1d'})
            snapshot['gld'] = _parse_yahoo_chart(raw, symbol='GLD')
            snapshot['sources'].append('yahoo:GLD')
        except Exception as exc:
            errors.append('GLD:' + type(exc).__name__ + ':' + str(exc)[:80])
        # Coinbase PAXG candles
        try:
            raw = await _get_json(client, COINBASE_CANDLES, params={'granularity': 60})
            snapshot['paxg_coinbase'] = _parse_coinbase_candles(raw)
            snapshot['sources'].append('coinbase:PAXG-USD')
        except Exception as exc:
            errors.append('coinbase_candles:' + type(exc).__name__ + ':' + str(exc)[:80])
        # Coinbase book (DOM proxy)
        try:
            raw = await _get_json(client, COINBASE_BOOK, params={'level': 2})
            snapshot['paxg_book'] = _parse_coinbase_book(raw)
            snapshot['sources'].append('coinbase:book')
        except Exception as exc:
            errors.append('coinbase_book:' + type(exc).__name__ + ':' + str(exc)[:80])
        # OKX backup volume
        try:
            raw = await _get_json(client, OKX_CANDLES,
                                  params={'instId': 'PAXG-USDT', 'bar': '1m', 'limit': '120'})
            snapshot['paxg_okx'] = _parse_okx_candles(raw)
            snapshot['sources'].append('okx:PAXG-USDT')
        except Exception as exc:
            errors.append('okx:' + type(exc).__name__ + ':' + str(exc)[:80])

        snapshot['errors'] = errors
        has_vol = any(snapshot.get(k) for k in ('gc_f', 'paxg_coinbase', 'paxg_okx', 'gld'))
        has_book = isinstance(snapshot.get('paxg_book'), dict)
        snapshot['ok'] = bool(has_vol and has_book)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.download')
        temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
        return {
            'updated': True,
            'ok': snapshot['ok'],
            'sources': list(snapshot['sources']),
            'errors': errors,
            'reason': ('Liquidity snapshot refreshed'
                       if snapshot['ok'] else 'Liquidity partial/unavailable: ' + '; '.join(errors[:3])),
        }
    except Exception as exc:
        return {'updated': False, 'ok': False, 'sources': [], 'errors': [str(exc)[:120]],
                'reason': 'Liquidity refresh failed: ' + type(exc).__name__ + ': ' + str(exc)[:100]}
    finally:
        if own and client is not None:
            await client.aclose()


def load_liquidity_snapshot(path):
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def liquidity_features_from_path(path, bar_time, *, now=None):
    snap = load_liquidity_snapshot(path)
    return build_liquidity_features(snap, bar_time, now=now)
