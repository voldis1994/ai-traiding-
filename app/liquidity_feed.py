"""Public multi-source volume/DOM context for ANY Capital.com instrument.

Capital.com has no true volume/DOM — these feeds are EXTERNAL evidence only.
Routes are resolved from epic + instrument_profile (forex, metals, energy,
indices, crypto, …). Missing/stale sources are omitted; features return None
unless enough real observations exist for the target completed M1.

Fill / SL / £ remain on Capital. This module only builds bounded features.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote

import httpx

from .market_context import candle_time

YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
COINBASE_CANDLES = 'https://api.exchange.coinbase.com/products/{product}/candles'
COINBASE_BOOK = 'https://api.exchange.coinbase.com/products/{product}/book'
OKX_CANDLES = 'https://www.okx.com/api/v5/market/candles'

USER_AGENT = 'NeuralTradingAI/1.0 (+liquidity-context; not for Capital fill)'
MAX_AGE_SEC = 180
MIN_VOLUME_BARS = 20

LIQUIDITY_NAMES = (
    'liq_vol_z_primary',
    'liq_vol_z_confirm',
    'liq_vol_impulse',
    'liq_book_imbalance',
    'liq_book_micro_bps',
    'liq_source_coverage',
)

# CME FX futures (real volume) — spot Yahoo FX pairs usually report volume=0.
FX_FUTURES = {
    'EURUSD': '6E=F', 'GBPUSD': '6B=F', 'USDJPY': '6J=F', 'AUDUSD': '6A=F',
    'USDCAD': '6C=F', 'USDCHF': '6S=F', 'NZDUSD': '6N=F',
}
FX_ETF = {
    'EUR': 'FXE', 'GBP': 'EWU', 'JPY': 'EWJ', 'AUD': 'EWA',
    'CAD': 'EWC', 'CHF': 'FXF', 'NZD': 'ENZL', 'USD': 'UUP',
}


def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _norm_text(*parts):
    return ' '.join(str(p or '').upper() for p in parts)


def _has(text, *tokens):
    return any(re.search(r'(?<![A-Z0-9])'+re.escape(t)+r'(?![A-Z0-9])', text) for t in tokens)


def resolve_liquidity_routes(epic, profile=None):
    """Map a Capital instrument to public Yahoo / Coinbase / OKX liquidity routes."""
    profile = profile or {}
    kind = str(profile.get('kind') or 'OTHER').upper()
    name = str(profile.get('name') or '')
    text = _norm_text(epic, name, kind)
    currencies = [str(c).upper() for c in (profile.get('currencies') or []) if c]
    yahoo = []
    coinbase = None
    okx = None
    label = kind

    # --- Crypto (real spot volume + L2 book) ---
    if kind == 'CRYPTO' or _has(text, 'BITCOIN', 'BTC', 'ETHEREUM', 'ETH', 'SOLANA', 'SOL',
                                'XRP', 'RIPPLE', 'LITECOIN', 'LTC', 'PAXG'):
        if _has(text, 'BITCOIN', 'BTC'):
            yahoo, coinbase, okx = ['BTC-USD'], 'BTC-USD', 'BTC-USDT'
            label = 'CRYPTO_BTC'
        elif _has(text, 'ETHEREUM', 'ETH'):
            yahoo, coinbase, okx = ['ETH-USD'], 'ETH-USD', 'ETH-USDT'
            label = 'CRYPTO_ETH'
        elif _has(text, 'SOLANA', 'SOL'):
            yahoo, coinbase, okx = ['SOL-USD'], 'SOL-USD', 'SOL-USDT'
            label = 'CRYPTO_SOL'
        elif _has(text, 'XRP', 'RIPPLE'):
            yahoo, coinbase, okx = ['XRP-USD'], 'XRP-USD', 'XRP-USDT'
            label = 'CRYPTO_XRP'
        elif _has(text, 'LITECOIN', 'LTC'):
            yahoo, coinbase, okx = ['LTC-USD'], 'LTC-USD', 'LTC-USDT'
            label = 'CRYPTO_LTC'
        elif _has(text, 'PAXG', 'GOLD'):
            yahoo, coinbase, okx = ['PAXG-USD', 'GC=F'], 'PAXG-USD', 'PAXG-USDT'
            label = 'CRYPTO_PAXG'
        else:
            label = 'CRYPTO_UNMAPPED'

    # --- Metals ---
    elif kind == 'METALS' or _has(text, 'GOLD', 'XAU', 'SILVER', 'XAG', 'PLATINUM', 'XPT',
                                  'PALLADIUM', 'XPD'):
        if _has(text, 'SILVER', 'XAG'):
            yahoo, coinbase = ['SI=F', 'SLV'], None
            label = 'METAL_SILVER'
        elif _has(text, 'PLATINUM', 'XPT'):
            yahoo, coinbase = ['PL=F'], None
            label = 'METAL_PLATINUM'
        elif _has(text, 'PALLADIUM', 'XPD'):
            yahoo, coinbase = ['PA=F'], None
            label = 'METAL_PALLADIUM'
        else:
            yahoo, coinbase, okx = ['GC=F', 'GLD', 'MGC=F'], 'PAXG-USD', 'PAXG-USDT'
            label = 'METAL_GOLD'

    # --- Energy / softs / base metals ---
    elif kind == 'COMMODITIES' or _has(text, 'CRUDE', 'BRENT', 'WTI', 'OIL', 'NATGAS',
                                       'NATURAL GAS', 'NATURALGAS', 'COPPER', 'WHEAT',
                                       'CORN', 'COFFEE', 'COCOA'):
        if _has(text, 'BRENT'):
            yahoo = ['BZ=F', 'USO']; label = 'CMDTY_BRENT'
        elif _has(text, 'CRUDE', 'WTI', 'OIL'):
            yahoo = ['CL=F', 'USO']; label = 'CMDTY_WTI'
        elif _has(text, 'NATGAS', 'NATURAL GAS', 'NATURALGAS'):
            yahoo = ['NG=F', 'UNG']; label = 'CMDTY_NATGAS'
        elif _has(text, 'COPPER'):
            yahoo = ['HG=F', 'CPER']; label = 'CMDTY_COPPER'
        elif _has(text, 'WHEAT'):
            yahoo = ['ZW=F', 'WEAT']; label = 'CMDTY_WHEAT'
        elif _has(text, 'CORN'):
            yahoo = ['ZC=F', 'CORN']; label = 'CMDTY_CORN'
        elif _has(text, 'COFFEE'):
            yahoo = ['KC=F']; label = 'CMDTY_COFFEE'
        elif _has(text, 'COCOA'):
            yahoo = ['CC=F']; label = 'CMDTY_COCOA'
        else:
            label = 'CMDTY_UNMAPPED'

    # --- Indices ---
    elif kind == 'INDICES' or _has(text, 'NASDAQ', 'US100', 'US500', 'S&P', 'DOW', 'US30',
                                   'RUSSELL', 'DAX', 'GER40', 'FTSE', 'UK100', 'NIKKEI',
                                   'JP225', 'HANG SENG', 'HK50', 'EURO STOXX', 'CAC40'):
        if _has(text, 'NASDAQ', 'US100'):
            yahoo = ['NQ=F', 'QQQ']; label = 'IDX_US100'
        elif _has(text, 'US500', 'S&P'):
            yahoo = ['ES=F', 'SPY']; label = 'IDX_US500'
        elif _has(text, 'DOW', 'US30', 'WALL STREET'):
            yahoo = ['YM=F', 'DIA']; label = 'IDX_US30'
        elif _has(text, 'RUSSELL'):
            yahoo = ['RTY=F']; label = 'IDX_RUSSELL'
        elif _has(text, 'DAX', 'GER40', 'GERMANY 40'):
            yahoo = ['EWG', 'EZU']; label = 'IDX_DAX'
        elif _has(text, 'FTSE', 'UK100', 'UK 100'):
            yahoo = ['EWU']; label = 'IDX_FTSE'
        elif _has(text, 'NIKKEI', 'JP225', 'JAPAN 225'):
            yahoo = ['NIY=F', 'EWJ']; label = 'IDX_NIKKEI'
        elif _has(text, 'HANG SENG', 'HK50', 'HONG KONG'):
            yahoo = ['EWH']; label = 'IDX_HK'
        elif _has(text, 'EURO STOXX', 'EUROSTOXX', 'CAC40', 'FRANCE 40'):
            yahoo = ['EZU']; label = 'IDX_EU'
        else:
            yahoo = ['ES=F', 'SPY']; label = 'IDX_GENERIC'

    # --- FX ---
    elif kind == 'FOREX' or (len(currencies) == 2 and currencies[0] != currencies[1]):
        if len(currencies) == 2:
            a, b = currencies[0], currencies[1]
        else:
            a = b = None
            for match in re.finditer(r'(?<![A-Z])([A-Z]{3})\s*[/_-]?\s*([A-Z]{3})(?![A-Z])', text):
                x, y = match.groups()
                if x != y:
                    a, b = x, y
                    break
        if a and b:
            pair = a + b
            inv = b + a
            fut = FX_FUTURES.get(pair) or FX_FUTURES.get(inv)
            if fut:
                yahoo.append(fut)
            # Second tape: USD index or currency ETF — never invent volume.
            if 'USD' in (a, b):
                other = b if a == 'USD' else a
                etf = FX_ETF.get(other)
                if etf:
                    yahoo.append(etf)
                if fut != '6E=F' and pair not in ('EURUSD',) and 'UUP' not in yahoo:
                    yahoo.append('UUP')
            else:
                # Cross: both currency ETFs if known.
                for ccy in (a, b):
                    etf = FX_ETF.get(ccy)
                    if etf and etf not in yahoo:
                        yahoo.append(etf)
            label = 'FX_' + pair
        else:
            label = 'FX_UNMAPPED'

    # --- Equities / ETF: best-effort Yahoo ticker from short name ---
    elif kind == 'EQUITIES':
        ticker = None
        for token in re.findall(r'\b[A-Z]{1,5}\b', name.upper()):
            if token not in ('CFD', 'USD', 'GBP', 'EUR', 'THE', 'INC', 'PLC', 'ETF', 'STOCK'):
                ticker = token
                break
        if ticker:
            yahoo = [ticker]
            label = 'EQ_' + ticker
        else:
            label = 'EQ_UNMAPPED'

    # Deduplicate preserving order
    seen = set()
    yahoo_u = []
    for s in yahoo:
        if s and s not in seen:
            seen.add(s)
            yahoo_u.append(s)
    return {
        'epic': epic,
        'kind': kind,
        'label': label,
        'yahoo': yahoo_u[:4],
        'coinbase': coinbase,
        'okx': okx,
        'mapped': bool(yahoo_u or coinbase or okx),
    }


def _zscore(values, current):
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
    nonzero = sum(1 for b in bars if b['volume'] > 0)
    if len(bars) < MIN_VOLUME_BARS or nonzero < MIN_VOLUME_BARS // 2:
        raise ValueError(f'Yahoo {symbol} insufficient real volume')
    return {'symbol': symbol, 'source': 'yahoo_finance', 'bars': bars[-120:]}


def _parse_coinbase_candles(rows, *, product):
    if not isinstance(rows, list) or not rows:
        raise ValueError('Coinbase candles empty')
    bars = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts_i = int(row[0]); vol_f = float(row[5]); close = float(row[4])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vol_f) or vol_f < 0 or not math.isfinite(close):
            continue
        bars.append({
            't': datetime.fromtimestamp(ts_i, tz=timezone.utc).replace(second=0, microsecond=0).isoformat(),
            'ts': ts_i, 'volume': vol_f, 'close': close,
        })
    bars.sort(key=lambda b: b['ts'])
    if len(bars) < MIN_VOLUME_BARS:
        raise ValueError(f'Coinbase {product} too few volume bars')
    return {'symbol': product, 'source': 'coinbase', 'bars': bars[-120:]}


def _parse_okx_candles(payload, *, inst):
    if not isinstance(payload, dict) or payload.get('code') != '0':
        raise ValueError('OKX candles rejected')
    bars = []
    for row in payload.get('data') or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts_i = int(int(row[0]) // 1000)
            vol_f = float(row[5]); close = float(row[4])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vol_f) or vol_f < 0:
            continue
        bars.append({
            't': datetime.fromtimestamp(ts_i, tz=timezone.utc).replace(second=0, microsecond=0).isoformat(),
            'ts': ts_i, 'volume': vol_f, 'close': close,
        })
    bars.sort(key=lambda b: b['ts'])
    if len(bars) < MIN_VOLUME_BARS:
        raise ValueError(f'OKX {inst} too few volume bars')
    return {'symbol': inst, 'source': 'okx', 'bars': bars[-120:]}


def _parse_coinbase_book(payload, *, product, top_n=10):
    try:
        bids = payload['bids']; asks = payload['asks']
    except (KeyError, TypeError) as exc:
        raise ValueError('Coinbase book malformed') from exc
    if not bids or not asks:
        raise ValueError('Coinbase book empty')
    bid_sz = ask_sz = 0.0
    bid_px = ask_px = None
    for level in bids[:top_n]:
        px = _finite(level[0]); sz = _finite(level[1])
        if px is None or sz is None or sz < 0:
            continue
        bid_sz += sz
        if bid_px is None:
            bid_px = px
    for level in asks[:top_n]:
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
    return {
        'source': 'coinbase', 'symbol': product,
        'bid': bid_px, 'ask': ask_px,
        'imbalance': max(-1.0, min(1.0, imbalance)),
        'micro_bps': micro_bps,
        'spread_bps': max(0.0, (ask_px - bid_px) / mid * 10000.0),
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
    target = candle_time(bar_time_iso)
    if target is None or not bars:
        return None, None
    target_iso = target.replace(second=0, microsecond=0).isoformat()
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
        if not prior:
            return None, None
        return prior[:-1], prior[-1]
    return prior[:-1], exact


def build_liquidity_features(snapshot, bar_time, *, now=None, epic=None):
    """Six bounded EXTERNAL features or None — never pads missing sources with fake volume."""
    if not isinstance(snapshot, dict) or not snapshot.get('ok'):
        return None
    if epic is not None and snapshot.get('epic') not in (None, epic):
        # Snapshot belongs to another instrument mandate.
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
    for series in snapshot.get('series') or []:
        if not isinstance(series, dict) or not series.get('bars'):
            continue
        hist, cur = _series_at(series['bars'], bar_time)
        if hist is None or cur is None:
            continue
        # Skip long-stale cash ETF tapes outside session.
        last_ts = series['bars'][-1]['ts']
        if series.get('source') == 'yahoo_finance' and not str(series.get('symbol', '')).endswith('=F'):
            if abs(now.timestamp() - last_ts) > 6 * 3600:
                continue
        z = _zscore(hist, cur)
        imp = _impulse(hist, cur)
        if z is None:
            continue
        series_list.append({'z': z, 'impulse': imp, 'symbol': series.get('symbol')})

    if len(series_list) < 1:
        return None

    primary = series_list[0]
    if len(series_list) >= 2:
        confirm_z = float(series_list[1]['z'])
        confirm_present = True
    else:
        confirm_z = 0.0
        confirm_present = False

    impulse = primary['impulse']
    if impulse is None:
        impulse = 0.0

    book = snapshot.get('book')
    book_ok = isinstance(book, dict) and _finite(book.get('imbalance')) is not None
    if book_ok:
        imbalance = float(book['imbalance'])
        micro = float(book.get('micro_bps') or 0.0)
    else:
        # No free L2 for most FX/futures — coverage reflects absence; do not invent pressure.
        imbalance = 0.0
        micro = 0.0

    slots = 3  # primary vol, confirm vol, book
    filled = 1 + (1 if confirm_present else 0) + (1 if book_ok else 0)
    coverage = filled / slots

    out = [
        float(primary['z']), float(confirm_z), float(impulse),
        float(imbalance), float(micro), float(coverage),
    ]
    if not all(math.isfinite(x) for x in out):
        return None
    if not (-1.0 <= out[0] <= 1.0 and -1.0 <= out[1] <= 1.0 and -1.0 <= out[2] <= 1.0):
        return None
    if not (-1.0 <= out[3] <= 1.0 and -1.0 <= out[4] <= 1.0 and 0.0 <= out[5] <= 1.0):
        return None
    return out


async def refresh_liquidity(path, *, epic=None, profile=None, client=None, now=None):
    """Fetch public volume/DOM for the active Capital instrument into a local JSON file."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = Path(path)
    routes = resolve_liquidity_routes(epic, profile)
    own = client is None
    errors = []
    snapshot = {
        'ok': False,
        'epic': epic,
        'updated_at': now.astimezone(timezone.utc).isoformat(),
        'routes': routes,
        'series': [],
        'book': None,
        'sources': [],
        'errors': [],
    }
    if not routes.get('mapped'):
        snapshot['errors'] = ['unmapped_instrument']
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.download')
        temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
        return {'updated': True, 'ok': False, 'sources': [], 'errors': snapshot['errors'],
                'reason': f'No public liquidity route for {epic or "unknown"}',
                'routes': routes}
    try:
        if own:
            client = httpx.AsyncClient(timeout=12, follow_redirects=True)
        for sym in routes.get('yahoo') or []:
            try:
                url = YAHOO_CHART.format(symbol=quote(sym, safe='=^'))
                raw = await _get_json(client, url, params={'interval': '1m', 'range': '1d'})
                series = _parse_yahoo_chart(raw, symbol=sym)
                snapshot['series'].append(series)
                snapshot['sources'].append('yahoo:' + sym)
            except Exception as exc:
                errors.append('yahoo:' + sym + ':' + type(exc).__name__ + ':' + str(exc)[:60])
        product = routes.get('coinbase')
        if product:
            try:
                url = COINBASE_CANDLES.format(product=product)
                raw = await _get_json(client, url, params={'granularity': 60})
                snapshot['series'].append(_parse_coinbase_candles(raw, product=product))
                snapshot['sources'].append('coinbase:' + product)
            except Exception as exc:
                errors.append('coinbase_candles:' + type(exc).__name__ + ':' + str(exc)[:60])
            try:
                url = COINBASE_BOOK.format(product=product)
                raw = await _get_json(client, url, params={'level': 2})
                snapshot['book'] = _parse_coinbase_book(raw, product=product)
                snapshot['sources'].append('coinbase:book:' + product)
            except Exception as exc:
                errors.append('coinbase_book:' + type(exc).__name__ + ':' + str(exc)[:60])
        inst = routes.get('okx')
        if inst:
            try:
                raw = await _get_json(client, OKX_CANDLES,
                                      params={'instId': inst, 'bar': '1m', 'limit': '120'})
                snapshot['series'].append(_parse_okx_candles(raw, inst=inst))
                snapshot['sources'].append('okx:' + inst)
            except Exception as exc:
                errors.append('okx:' + type(exc).__name__ + ':' + str(exc)[:60])

        snapshot['errors'] = errors
        snapshot['ok'] = len(snapshot['series']) >= 1
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.download')
        temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
        return {
            'updated': True,
            'ok': snapshot['ok'],
            'sources': list(snapshot['sources']),
            'errors': errors,
            'routes': routes,
            'reason': ('Liquidity snapshot refreshed for ' + str(routes.get('label'))
                       if snapshot['ok'] else 'Liquidity unavailable: ' + '; '.join(errors[:3])),
        }
    except Exception as exc:
        return {'updated': False, 'ok': False, 'sources': [], 'errors': [str(exc)[:120]],
                'routes': routes,
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


def liquidity_features_from_path(path, bar_time, *, now=None, epic=None):
    snap = load_liquidity_snapshot(path)
    return build_liquidity_features(snap, bar_time, now=now, epic=epic)
