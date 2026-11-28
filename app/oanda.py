"""OANDA v20 LIVE endpoints only. Execution requires explicit arming."""
import json, os, re
from datetime import datetime, timezone
import requests
from .market_clock import Quote, utc

REST = 'https://api-fxtrade.oanda.com'
STREAM = 'https://stream-fxtrade.oanda.com'


def instrument_name(s):
    if not re.fullmatch(r'[A-Z0-9]+_[A-Z0-9]+', s): raise ValueError('Invalid OANDA instrument')
    return s


def decode_price(raw, received_at):
    if not isinstance(raw,dict):raise ValueError("Non-object price message")
    if raw.get('type') == 'HEARTBEAT': return None
    if raw.get('type') != 'PRICE': raise ValueError('Unexpected message')
    if raw.get('tradeable') is False or (raw.get('status') is not None and raw['status'] != 'tradeable') or (raw.get('status') is None and raw.get('tradeable') is not True):
        return None
    bids = [float(x['price']) for x in raw['bids'] if float(x['liquidity']) > 0]
    asks = [float(x['price']) for x in raw['asks'] if float(x['liquidity']) > 0]
    if not bids or not asks: raise ValueError('Missing liquid quote side')
    return Quote(instrument_name(raw['instrument']), utc(raw['time']), utc(received_at), max(bids), min(asks))


class OandaLive:
    def __init__(self, account_id=None, token=None, *, session=None, armed=False):
        self.account_id = account_id or os.getenv('OANDA_ACCOUNT_ID', '')
        token = token or os.getenv('OANDA_API_TOKEN', '')
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+)+', self.account_id) or not token:
            raise ValueError('Set OANDA_ACCOUNT_ID and OANDA_API_TOKEN locally')
        self.armed = armed is True
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': 'Bearer '+token, 'Accept-Datetime-Format': 'RFC3339'})

    def _get(self, path, params=None):
        with self.session.get(REST+path, params=params, timeout=(5, 15), allow_redirects=False) as r:
            if 300 <= getattr(r, "status_code", 200) < 400: raise RuntimeError("Unexpected redirect")
            r.raise_for_status()
            return r.json()

    def instruments(self):
        data = self._get(f'/v3/accounts/{self.account_id}/instruments')
        return [x['name'] for x in data['instruments']]

    def account_summary(self):
        return self._get(f'/v3/accounts/{self.account_id}/summary')

    def open_trades(self):
        return self._get(f'/v3/accounts/{self.account_id}/openTrades')

    def transactions_since(self, transaction_id):
        if not re.fullmatch(r'[0-9]+', str(transaction_id)):raise ValueError('Invalid transaction ID')
        return self._get(f'/v3/accounts/{self.account_id}/transactions/sinceid', {'id':str(transaction_id)})

    def candles(self, instrument, count=100):
        if not 1 <= count <= 5000: raise ValueError('count outside OANDA range')
        name = instrument_name(instrument)
        data = self._get(f'/v3/instruments/{name}/candles', {'price':'MBA','granularity':'M1','count':count})
        return [x for x in data['candles'] if x.get('complete') is True]

    def messages(self, instrument, *, session=None):
        name = instrument_name(instrument)
        with (session or self.session).get(f'{STREAM}/v3/accounts/{self.account_id}/pricing/stream',
                              params={'instruments':name}, stream=True, timeout=(5, 15), allow_redirects=False) as r:
            if 300 <= getattr(r, "status_code", 200) < 400: raise RuntimeError("Unexpected redirect")
            r.raise_for_status()
            for line in r.iter_lines(chunk_size=1):
                if line:
                    received = datetime.now(timezone.utc).isoformat()
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw,dict): raise ValueError('Non-object')
                    except (ValueError,UnicodeError,TypeError):
                        yield {'received_at':received,'message':{'type':'INVALID'}}
                        continue
                    if raw.get('type') == 'PRICE' and raw.get('instrument') != name:
                        raise ValueError('Unexpected instrument')
                    yield {'received_at':received,'message':raw}

    def instrument_details(self, instrument):
        name = instrument_name(instrument)
        rows = self._get(f'/v3/accounts/{self.account_id}/instruments', {'instruments':name})['instruments']
        if len(rows) != 1 or rows[0]['name'] != name: raise ValueError('Instrument unavailable')
        return rows[0]

    def pending_orders(self):
        return self._get(f'/v3/accounts/{self.account_id}/pendingOrders')

    def trade(self, trade_id):
        if not re.fullmatch(r'[0-9]+', str(trade_id)): raise ValueError('Invalid trade ID')
        return self._get(f'/v3/accounts/{self.account_id}/trades/{trade_id}')['trade']

    def _write(self, method, path, payload):
        if not self.armed: raise RuntimeError('LIVE order submission is not armed')
        # Never automatically retry writes: a timeout can occur AFTER a fill.
        with self.session.request(method, REST+path, json=payload, timeout=(5,15), allow_redirects=False) as r:
            if not 200 <= r.status_code < 300: raise RuntimeError(f'Unconfirmed broker write HTTP {r.status_code}')
            return r.json()

    def create_order(self, order):
        return self._write('POST', f'/v3/accounts/{self.account_id}/orders', {'order':order})

    def close(self): self.session.close()
