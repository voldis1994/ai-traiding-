"""OANDA v20 LIVE REST broker adapter.

Presents the same async surface the engine historically used with Capital.com
(positions/accounts/market/prices/open/close/update/confirm/transactions),
while talking only to OANDA fxTrade / fxPractice. Never automatically retry
non-idempotent writes. Credentials stay in RAM.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import quote
import httpx

LIVE_REST = 'https://api-fxtrade.oanda.com'
LIVE_STREAM = 'https://stream-fxtrade.oanda.com'
DEMO_REST = 'https://api-fxpractice.oanda.com'
DEMO_STREAM = 'https://stream-fxpractice.oanda.com'


def capital_api_time(value):
    """Format timestamps for broker history ``from``/``to`` (UTC, no micros).

    Kept under the historical name so engine lookback helpers stay stable.
    OANDA accepts RFC3339; bare ``YYYY-MM-DDTHH:MM:SS`` is also fine.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace(' ', 'T')
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            if s.endswith('Z'):
                return s[:-1]
            if s.endswith('+00:00'):
                return s[:-6]
            return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%S')


broker_api_time = capital_api_time


def instrument_name(s: str) -> str:
    name = str(s or '').strip().upper().replace('/', '_').replace('-', '_')
    if not re.fullmatch(r'[A-Z0-9]+_[A-Z0-9]+', name):
        # Allow legacy Capital-style epics through unchanged for search/adopt.
        if re.fullmatch(r'[A-Z0-9._]+', name) and name:
            return name
        raise ValueError('Invalid OANDA instrument')
    return name


def _num(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_price(value, precision: int) -> str:
    q = Decimal('1').scaleb(-int(precision))
    return format(Decimal(str(value)).quantize(q), 'f')


class OandaBroker:
    """Async OANDA v20 client shaped like the former CapitalBroker surface."""

    def __init__(self, api_key, identifier, password='', live=True, client=None):
        # api_key = Bearer token; identifier = account id; password unused.
        self.api_key = (api_key or '').strip()
        self.identifier = (identifier or '').strip()
        self.password = password or ''
        self.live = bool(live)
        self.base = LIVE_REST if self.live else DEMO_REST
        self.stream_base = LIVE_STREAM if self.live else DEMO_STREAM
        self.account_id = self.identifier
        self.client = client or httpx.AsyncClient(
            base_url=self.base, timeout=15.0, follow_redirects=False)
        self.cst = None  # unused (Capital session leftover for stream checks)
        self.security = None
        self._confirms: dict[str, dict] = {}
        self._currency = None
        self._instrument_cache: dict[str, dict] = {}
        self._last_tx_id = None
        if not self.api_key or not re.fullmatch(r'[0-9]+(?:-[0-9]+)+', self.account_id):
            raise ValueError('OANDA requires API token and account id (e.g. 101-001-…)')

    def headers(self):
        return {
            'Authorization': 'Bearer ' + self.api_key,
            'Accept-Datetime-Format': 'RFC3339',
            'Content-Type': 'application/json',
        }

    async def connect(self):
        """Validate token + account (no session cookie — Bearer auth)."""
        data = await self._request('GET', f'/v3/accounts/{self.account_id}/summary')
        account = data.get('account') or {}
        if str(account.get('id') or '') != self.account_id:
            raise RuntimeError('OANDA account identity mismatch')
        self._currency = account.get('currency')
        self._last_tx_id = data.get('lastTransactionID')
        # Populate Capital-era token slots so stream readiness checks pass.
        self.cst = 'oanda'
        self.security = self.api_key[:12] if self.api_key else 'oanda'
        return data

    async def _request(self, method, path, *, params=None, json=None):
        response = await self.client.request(
            method, path, headers=self.headers(), params=params, json=json)
        # Relogin-equivalent only for reads (token is static; refresh summary).
        if response.status_code == 401 and method == 'GET':
            response = await self.client.request(
                method, path, headers=self.headers(), params=params, json=json)
        if 300 <= response.status_code < 400:
            raise RuntimeError('Unexpected OANDA redirect')
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    async def ping(self):
        return await self._request('GET', f'/v3/accounts/{self.account_id}/summary')

    async def accounts(self):
        data = await self._request('GET', f'/v3/accounts/{self.account_id}/summary')
        account = data.get('account') or {}
        self._currency = account.get('currency') or self._currency
        self._last_tx_id = data.get('lastTransactionID') or self._last_tx_id
        balance = _num(account.get('balance'))
        nav = _num(account.get('NAV'))
        available = _num(account.get('marginAvailable'))
        return {
            'accounts': [{
                'preferred': True,
                'accountId': str(account.get('id') or self.account_id),
                'accountName': account.get('alias') or self.account_id,
                'currency': account.get('currency'),
                'balance': {
                    'balance': balance,
                    'available': available,
                    'equity': nav if nav is not None else balance,
                    'profitLoss': _num(account.get('pl')),
                },
            }],
            'currentAccountId': str(account.get('id') or self.account_id),
        }

    def _map_trade(self, trade: dict) -> dict:
        units = _num(trade.get('currentUnits')) or 0.0
        direction = 'BUY' if units > 0 else 'SELL'
        size = abs(units)
        stop = None
        profit = None
        sl = trade.get('stopLossOrder') or {}
        tp = trade.get('takeProfitOrder') or {}
        if isinstance(sl, dict):
            stop = _num(sl.get('price'))
        if isinstance(tp, dict):
            profit = _num(tp.get('price'))
        instrument = trade.get('instrument')
        return {
            'position': {
                'dealId': str(trade.get('id')),
                'size': size,
                'direction': direction,
                'level': _num(trade.get('price')),
                'stopLevel': stop,
                'profitLevel': profit if profit is not None else 0,
                'upl': _num(trade.get('unrealizedPL')),
                'currency': self._currency,
                'contractSize': 1,
            },
            'market': {
                'epic': instrument,
                'instrumentName': instrument,
                'symbol': instrument,
            },
        }

    async def positions(self):
        data = await self._request('GET', f'/v3/accounts/{self.account_id}/openTrades')
        trades = data.get('trades') or []
        return [self._map_trade(t) for t in trades if isinstance(t, dict)]

    async def _instrument(self, epic: str) -> dict:
        name = instrument_name(epic)
        if name in self._instrument_cache:
            return self._instrument_cache[name]
        data = await self._request(
            'GET', f'/v3/accounts/{self.account_id}/instruments',
            params={'instruments': name})
        rows = data.get('instruments') or []
        if len(rows) != 1:
            raise RuntimeError(f'Instrument unavailable: {name}')
        self._instrument_cache[name] = rows[0]
        return rows[0]

    async def market(self, epic):
        name = instrument_name(epic)
        info = await self._instrument(name)
        pricing = await self._request(
            'GET', f'/v3/accounts/{self.account_id}/pricing',
            params={'instruments': name})
        prices = pricing.get('prices') or []
        row = prices[0] if prices else {}
        bids = row.get('bids') or []
        asks = row.get('asks') or []
        bid = _num(bids[0]['price']) if bids else None
        ask = _num(asks[0]['price']) if asks else None
        tradeable = row.get('tradeable') is not False and (
            row.get('status') in (None, 'tradeable'))
        precision = int(info.get('displayPrecision') or 5)
        pip_location = int(info.get('pipLocation') or -4)
        one_pip = 10 ** pip_location
        min_stop = _num((info.get('minimumTrailingStopDistance')
                         or info.get('minimumStopDistance') or {}).get('distance'))
        if min_stop is None:
            min_stop = one_pip
        return {
            'instrument': {
                'epic': name,
                'name': info.get('displayName') or name,
                'type': info.get('type'),
                'currency': (info.get('marginRate') and self._currency) or self._currency,
                'onePipMeans': one_pip,
                'valueOfOnePip': one_pip,
            },
            'snapshot': {
                'bid': bid,
                'offer': ask,
                'marketStatus': 'TRADEABLE' if tradeable else 'CLOSED',
                'decimalPlacesFactor': precision,
                'updateTime': row.get('time'),
            },
            'dealingRules': {
                'minStopOrProfitDistance': {
                    'unit': 'POINTS',
                    'value': str(min_stop),
                },
            },
        }

    async def search_markets(self, search_term):
        term = str(search_term or '').strip()
        data = await self._request('GET', f'/v3/accounts/{self.account_id}/instruments')
        needle = re.sub(r'[^A-Z0-9]', '', term.upper())
        markets = []
        for item in data.get('instruments') or []:
            name = item.get('name') or ''
            display = item.get('displayName') or ''
            blob = re.sub(r'[^A-Z0-9]', '', (name + display).upper())
            if not needle or needle in blob or needle in re.sub(r'[^A-Z0-9]', '', name.upper()):
                markets.append({
                    'epic': name,
                    'instrumentName': display or name,
                    'name': display or name,
                    'symbol': name,
                })
        return {'markets': markets}

    def _candle_to_capital(self, candle: dict) -> dict | None:
        if not candle.get('complete'):
            return None
        bid = candle.get('bid') or {}
        ask = candle.get('ask') or {}
        mid = candle.get('mid') or {}

        def side(bucket, key):
            if bucket.get(key) is not None:
                return _num(bucket.get(key))
            return _num(mid.get(key))

        def pair(key):
            b = side(bid, key)
            a = side(ask, key)
            if b is None and a is None:
                m = _num(mid.get(key))
                return {'bid': m, 'ask': m} if m is not None else {}
            if b is None:
                b = a
            if a is None:
                a = b
            return {'bid': b, 'ask': a}

        return {
            'snapshotTimeUTC': candle.get('time'),
            'snapshotTime': candle.get('time'),
            'openPrice': pair('o'),
            'highPrice': pair('h'),
            'lowPrice': pair('l'),
            'closePrice': pair('c'),
        }

    async def prices(self, epic, resolution='MINUTE', maximum=80,
                     from_time=None, to_time=None):
        name = instrument_name(epic)
        gran = {
            'MINUTE': 'M1', 'MINUTE_5': 'M5', 'MINUTE_15': 'M15',
            'MINUTE_30': 'M30', 'HOUR': 'H1', 'HOUR_4': 'H4', 'DAY': 'D',
        }.get(str(resolution).upper(), 'M1')
        count = max(1, min(int(maximum), 5000))
        params = {'price': 'MBA', 'granularity': gran, 'count': count}
        # OANDA from/to are RFC3339; prefer them when a window is requested.
        ft = capital_api_time(from_time)
        tt = capital_api_time(to_time)
        if ft:
            params['from'] = ft + 'Z'
            params.pop('count', None)
        if tt:
            params['to'] = tt + 'Z'
            params.pop('count', None)
        data = await self._request('GET', f'/v3/instruments/{quote(name, safe="")}/candles',
                                   params=params)
        rows = []
        for c in data.get('candles') or []:
            mapped = self._candle_to_capital(c)
            if mapped:
                rows.append(mapped)
        return {'prices': rows}

    async def confirm(self, deal_reference):
        ref = str(deal_reference or '')
        cached = self._confirms.get(ref)
        if cached:
            return dict(cached)
        # Fall back: scan recent transactions for clientExtensions / order id.
        try:
            data = await self._request(
                'GET', f'/v3/accounts/{self.account_id}/transactions',
                params={'count': 50})
        except Exception:
            return {'dealStatus': 'UNKNOWN', 'dealReference': ref}
        for tx in reversed(data.get('transactions') or []):
            cid = ((tx.get('clientExtensions') or {}).get('id')
                   or (tx.get('tradeOpened') or {}).get('tradeID')
                   or str(tx.get('id') or ''))
            if ref in (cid, str(tx.get('id') or ''),
                       str((tx.get('orderFillTransaction') or {}).get('id') or '')):
                trade = (tx.get('tradeOpened') or {})
                deal = str(trade.get('tradeID') or tx.get('id') or '')
                conf = {
                    'dealStatus': 'ACCEPTED',
                    'dealId': deal,
                    'dealReference': ref,
                    'level': _num(tx.get('price')),
                    'profit': _num(tx.get('pl')),
                }
                self._confirms[ref] = conf
                return dict(conf)
        return {'dealStatus': 'UNKNOWN', 'dealReference': ref}

    def _tx_window_params(self, *, last_period=600, from_time=None, to_time=None):
        params = {}
        ft = capital_api_time(from_time)
        tt = capital_api_time(to_time)
        if ft:
            params['from'] = ft + 'Z'
        if tt:
            params['to'] = tt + 'Z'
        if not ft and last_period is not None:
            start = datetime.now(timezone.utc) - timedelta(seconds=int(last_period))
            params['from'] = start.replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
        return params

    def _map_transaction(self, tx: dict) -> dict:
        pl = _num(tx.get('pl'))
        if pl is None:
            pl = _num(tx.get('realizedPL'))
        trade_ids = []
        for key in ('tradesClosed', 'tradeReduced', 'tradeOpened'):
            block = tx.get(key)
            if isinstance(block, list):
                for item in block:
                    tid = item.get('tradeID') or item.get('id')
                    if tid:
                        trade_ids.append(str(tid))
            elif isinstance(block, dict):
                tid = block.get('tradeID') or block.get('id')
                if tid:
                    trade_ids.append(str(tid))
        deal = trade_ids[0] if trade_ids else str(tx.get('id') or '')
        return {
            'transactionId': str(tx.get('id') or ''),
            'dealId': deal,
            'reference': deal,
            'date': tx.get('time'),
            'profitAndLoss': pl,
            'profit': pl,
            'pnl': pl,
            'currency': self._currency,
            'type': tx.get('type'),
            'note': ','.join(trade_ids),
        }

    async def transactions(self, *, last_period=600, from_time=None, to_time=None,
                           tx_type='TRADE'):
        params = self._tx_window_params(
            last_period=last_period, from_time=from_time, to_time=to_time)
        data = await self._request(
            'GET', f'/v3/accounts/{self.account_id}/transactions', params=params)
        rows = []
        for tx in data.get('transactions') or []:
            t = str(tx.get('type') or '')
            if tx_type == 'TRADE' and t not in (
                    'ORDER_FILL', 'TRADE_CLOSE', 'STOP_LOSS_ORDER_FILL',
                    'TAKE_PROFIT_ORDER_FILL', 'MARKET_ORDER_TRADE_CLOSE',
                    'GUARANTEED_STOP_LOSS_ORDER_FILL'):
                # Still keep fills that carry realized PL / closed trades.
                if not (tx.get('tradesClosed') or tx.get('pl') not in (None, '0', 0, '0.0')):
                    continue
            rows.append(self._map_transaction(tx))
        return {'transactions': rows}

    async def activity(self, *, last_period=600, from_time=None, to_time=None,
                       detailed=True, deal_id=None):
        """Synthesize Capital-style activity rows from OANDA transactions."""
        payload = await self.transactions(
            last_period=last_period, from_time=from_time, to_time=to_time)
        activities = []
        for tx in payload.get('transactions') or []:
            if deal_id and str(deal_id) not in (
                    str(tx.get('dealId') or ''), str(tx.get('reference') or ''),
                    str(tx.get('note') or '')):
                if str(deal_id) not in str(tx.get('note') or ''):
                    continue
            activities.append({
                'dealId': tx.get('dealId'),
                'date': tx.get('date'),
                'status': 'ACCEPTED',
                'type': tx.get('type'),
                'details': {
                    'dealId': tx.get('dealId'),
                    'profit': tx.get('profit'),
                    'profitAndLoss': tx.get('profitAndLoss'),
                    'currency': tx.get('currency'),
                },
                'currency': tx.get('currency'),
                'profit': tx.get('profit'),
                'profitAndLoss': tx.get('profitAndLoss'),
            })
        return {'activities': activities}

    async def open_position(self, epic, direction, size,
                            stop_distance=None, profit_distance=None,
                            limit_distance=None):
        if direction not in ('BUY', 'SELL') or Decimal(str(size)) <= 0:
            raise ValueError('Invalid trade')
        name = instrument_name(epic)
        info = await self._instrument(name)
        precision = int(info.get('displayPrecision') or 5)
        unit_precision = int(info.get('tradeUnitsPrecision') or 0)
        units_abs = Decimal(str(size)).quantize(Decimal('1').scaleb(-unit_precision))
        if units_abs <= 0:
            raise ValueError('Unit precision mismatch')
        units = units_abs if direction == 'BUY' else -units_abs
        market = await self.market(name)
        snap = market.get('snapshot') or {}
        bid = _num(snap.get('bid'))
        ask = _num(snap.get('offer'))
        if bid is None or ask is None:
            raise RuntimeError('No OANDA quote for entry')
        entry = ask if direction == 'BUY' else bid
        order = {
            'type': 'MARKET',
            'instrument': name,
            'units': format(units, 'f'),
            'timeInForce': 'FOK',
            'positionFill': 'DEFAULT',
        }
        profit_d = profit_distance if profit_distance is not None else limit_distance
        if stop_distance is not None:
            sl = entry - float(stop_distance) if direction == 'BUY' else entry + float(stop_distance)
            order['stopLossOnFill'] = {
                'price': _fmt_price(sl, precision),
                'timeInForce': 'GTC',
            }
        if profit_d is not None:
            tp = entry + float(profit_d) if direction == 'BUY' else entry - float(profit_d)
            order['takeProfitOnFill'] = {
                'price': _fmt_price(tp, precision),
                'timeInForce': 'GTC',
            }
        cid = 'nta-' + hashlib.sha256(
            f'{self.account_id}|{name}|{units}|{entry}'.encode()).hexdigest()[:40]
        order['clientExtensions'] = {'id': cid}
        order['tradeClientExtensions'] = {'id': cid}
        reply = await self._request(
            'POST', f'/v3/accounts/{self.account_id}/orders', json={'order': order})
        fill = reply.get('orderFillTransaction') or {}
        cancel = reply.get('orderCancelTransaction')
        if cancel and not fill:
            ref = str(cancel.get('id') or cid)
            self._confirms[ref] = {
                'dealStatus': 'REJECTED',
                'dealReference': ref,
                'reason': cancel.get('reason') or 'cancelled',
            }
            return {'dealReference': ref}
        if not fill:
            raise RuntimeError('Unconfirmed OANDA order fill')
        trade = fill.get('tradeOpened') or {}
        deal_id = str(trade.get('tradeID') or '')
        ref = str(fill.get('id') or cid)
        conf = {
            'dealStatus': 'ACCEPTED',
            'dealId': deal_id,
            'dealReference': ref,
            'level': _num(fill.get('price')),
            'profit': _num(fill.get('pl')),
        }
        self._confirms[ref] = conf
        self._confirms[cid] = conf
        return {'dealReference': ref}

    async def close_position(self, deal_id):
        tid = str(deal_id)
        if not re.fullmatch(r'[0-9]+', tid):
            raise ValueError('Invalid trade ID')
        reply = await self._request(
            'PUT', f'/v3/accounts/{self.account_id}/trades/{tid}/close', json={})
        fill = reply.get('orderFillTransaction') or reply.get('tradeCloseTransaction') or {}
        ref = str(fill.get('id') or f'close-{tid}')
        self._confirms[ref] = {
            'dealStatus': 'ACCEPTED',
            'dealId': tid,
            'dealReference': ref,
            'level': _num(fill.get('price')),
            'profit': _num(fill.get('pl')),
        }
        return {'dealReference': ref}

    async def update_position(self, deal_id, stop_level=None, profit_level=None,
                              limit_level=None):
        tid = str(deal_id)
        if not re.fullmatch(r'[0-9]+', tid):
            raise ValueError('Invalid trade ID')
        trade = (await self._request(
            'GET', f'/v3/accounts/{self.account_id}/trades/{tid}')).get('trade') or {}
        info = await self._instrument(trade.get('instrument') or 'XAU_USD')
        precision = int(info.get('displayPrecision') or 5)
        payload = {}
        if stop_level is not None:
            payload['stopLoss'] = {
                'timeInForce': 'GTC',
                'price': _fmt_price(stop_level, precision),
            }
        level = profit_level if profit_level is not None else limit_level
        if level is not None and float(level) != 0:
            payload['takeProfit'] = {
                'timeInForce': 'GTC',
                'price': _fmt_price(level, precision),
            }
        if not payload:
            raise ValueError('No changes supplied')
        reply = await self._request(
            'PUT', f'/v3/accounts/{self.account_id}/trades/{tid}/orders',
            json=payload)
        ref = str((reply.get('stopLossOrderTransaction')
                   or reply.get('takeProfitOrderTransaction')
                   or {}).get('id') or f'protect-{tid}')
        self._confirms[ref] = {
            'dealStatus': 'ACCEPTED',
            'dealId': tid,
            'dealReference': ref,
        }
        return {'dealReference': ref}

    async def close(self, *, end_session: bool = True):
        self.cst = None
        self.security = None
        await self.client.aclose()


# Back-compat alias so older imports/monkeypatches keep resolving during migration.
CapitalBroker = OandaBroker
