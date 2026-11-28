"""OANDA v20 pricing stream (chunked HTTPS), authenticated with Bearer token.

REST /pricing remains the fallback and source of current dealing rules.
"""
from __future__ import annotations

import asyncio
import json
import time
import httpx


class OandaQuoteStream:
    def __init__(self):
        self.quote = None
        self.last_error = None
        self.connected = False
        self.subscription = None

    async def run(self, engine):
        while True:
            if not engine.broker or not engine.mandate:
                self.connected = False
                await asyncio.sleep(3)
                continue
            epic = engine.mandate['epic']
            broker = engine.broker
            stream_base = getattr(broker, 'stream_base', None) or 'https://stream-fxtrade.oanda.com'
            account_id = getattr(broker, 'account_id', None) or getattr(broker, 'identifier', None)
            token = getattr(broker, 'api_key', None)
            if not account_id or not token:
                self.last_error = 'MissingOandaCredentials'
                self.connected = False
                await asyncio.sleep(6)
                continue
            url = f'{stream_base}/v3/accounts/{account_id}/pricing/stream'
            try:
                timeout = httpx.Timeout(10.0, read=120.0)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                    async with client.stream(
                            'GET', url,
                            params={'instruments': epic},
                            headers={
                                'Authorization': 'Bearer ' + token,
                                'Accept-Datetime-Format': 'RFC3339',
                            }) as response:
                        if 300 <= response.status_code < 400:
                            raise RuntimeError('Unexpected stream redirect')
                        response.raise_for_status()
                        self.connected = True
                        self.subscription = epic
                        self.last_error = None
                        async for line in response.aiter_lines():
                            if not (engine.broker is broker and engine.mandate
                                    and engine.mandate.get('epic') == epic):
                                break
                            if not line:
                                continue
                            try:
                                raw = json.loads(line)
                            except (ValueError, TypeError):
                                continue
                            if not isinstance(raw, dict):
                                continue
                            if raw.get('type') == 'HEARTBEAT':
                                continue
                            if raw.get('type') != 'PRICE':
                                continue
                            if raw.get('instrument') != epic:
                                continue
                            if raw.get('tradeable') is False:
                                continue
                            if raw.get('status') not in (None, 'tradeable'):
                                continue
                            try:
                                bids = [float(x['price']) for x in (raw.get('bids') or [])
                                        if float(x.get('liquidity') or 0) > 0]
                                asks = [float(x['price']) for x in (raw.get('asks') or [])
                                        if float(x.get('liquidity') or 0) > 0]
                            except (TypeError, ValueError, KeyError):
                                continue
                            if not bids or not asks:
                                continue
                            bid = max(bids)
                            ask = min(asks)
                            if bid <= 0 or ask < bid:
                                continue
                            self.quote = {
                                'epic': epic,
                                'bid': bid,
                                'ask': ask,
                                'at': time.monotonic(),
                                'timestamp': raw.get('time'),
                            }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            finally:
                self.connected = False
                self.quote = None
            await asyncio.sleep(6)

    def current(self, epic, max_age=9):
        q = self.quote
        return q if q and q['epic'] == epic and time.monotonic() - q['at'] <= max_age else None


# Back-compat alias for runtime_hub / tests during broker migration.
CapitalQuoteStream = OandaQuoteStream
