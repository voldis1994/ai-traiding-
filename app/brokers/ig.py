"""IG Markets REST adapter (api key + identifier + password).

Same auth shape as Capital.com (CST / X-SECURITY-TOKEN), but IG paths and
VERSION headers differ. Responses are normalised toward the Capital-shaped
surface the engine already expects (nested position/market is fine —
``_map_positions`` already unwraps it).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from urllib.parse import quote
import httpx

from app.broker import capital_api_time

LIVE='https://api.ig.com/gateway/deal'
DEMO='https://demo-api.ig.com/gateway/deal'

# IG resolution names for price history.
_RES={
    'MINUTE':'MINUTE','MINUTE_1':'MINUTE','M1':'MINUTE',
    'MINUTE_5':'MINUTE_5','M5':'MINUTE_5',
    'MINUTE_15':'MINUTE_15','M15':'MINUTE_15',
    'MINUTE_30':'MINUTE_30','M30':'MINUTE_30',
    'HOUR':'HOUR','HOUR_1':'HOUR','H1':'HOUR',
    'HOUR_4':'HOUR_4','H4':'HOUR_4',
    'DAY':'DAY','D1':'DAY',
}


class IgBroker:
    def __init__(self, api_key, identifier, password, live=True, client=None,
                 base_url=None, **_kwargs):
        self.api_key=api_key;self.identifier=identifier;self.password=password
        self.base=(base_url or (LIVE if live else DEMO)).rstrip('/')
        self.streaming_url=None  # REST market poll is the fallback
        self.client=client or httpx.AsyncClient(
            base_url=self.base, timeout=15.0, follow_redirects=False)
        self.cst=None;self.security=None
        self._relogin=asyncio.Lock()
        self._account_id=None

    async def connect(self):
        response=await self.client.post(
            '/session',
            headers={'X-IG-API-KEY':self.api_key,'VERSION':'2',
                     'Accept':'application/json; charset=UTF-8',
                     'Content-Type':'application/json; charset=UTF-8'},
            json={'identifier':self.identifier,'password':self.password})
        response.raise_for_status()
        self.cst=response.headers.get('CST')
        self.security=response.headers.get('X-SECURITY-TOKEN')
        if not self.cst or not self.security:
            raise RuntimeError('Broker session tokens missing')
        body=response.json() or {}
        current=body.get('currentAccountId') or body.get('accountId')
        if current:
            self._account_id=str(current)
        return body

    def headers(self, *, version='1'):
        if not self.cst or not self.security:
            raise RuntimeError('Not authenticated')
        return {
            'X-IG-API-KEY':self.api_key,
            'CST':self.cst,
            'X-SECURITY-TOKEN':self.security,
            'VERSION':str(version),
            'Accept':'application/json; charset=UTF-8',
            'Content-Type':'application/json; charset=UTF-8',
        }

    async def _request(self, method, path, *, params=None, json=None, version='1'):
        response=await self.client.request(
            method, path, headers=self.headers(version=version),
            params=params, json=json)
        if response.status_code==401 and method=='GET':
            async with self._relogin:
                await self.connect()
            response=await self.client.request(
                method, path, headers=self.headers(version=version),
                params=params, json=json)
        response.raise_for_status()
        if response.status_code==204 or not response.content:
            return {}
        return response.json()

    async def accounts(self):
        return await self._request('GET','/accounts',version='1')

    async def ping(self):
        # IG has no /ping — session GET is the health check.
        return await self._request('GET','/session',version='1')

    async def positions(self):
        payload=await self._request('GET','/positions',version='2')
        rows=payload.get('positions') if isinstance(payload,dict) else payload
        out=[]
        for raw in rows or []:
            if not isinstance(raw,dict):
                continue
            pos=raw.get('position') if isinstance(raw.get('position'),dict) else raw
            market=raw.get('market') if isinstance(raw.get('market'),dict) else {}
            enriched=dict(pos)
            # Capital-shaped UPL so Admin / teach-from-UPL path works.
            if enriched.get('upl') is None:
                upl=self._estimate_upl(enriched,market)
                if upl is not None:
                    enriched['upl']=upl
            out.append({'position':enriched,'market':market})
        return out

    @staticmethod
    def _estimate_upl(pos, market):
        try:
            level=float(pos.get('level'))
            size=float(pos.get('size') or 0)
            direction=str(pos.get('direction') or '').upper()
            bid=market.get('bid');offer=market.get('offer')
            if bid is None and offer is None:
                return None
            bid=float(bid) if bid is not None else float(offer)
            offer=float(offer) if offer is not None else float(bid)
            if direction=='BUY':
                return round((bid-level)*size,4)
            if direction=='SELL':
                return round((level-offer)*size,4)
        except (TypeError,ValueError):
            return None
        return None

    async def market(self, epic):
        return await self._request('GET','/markets/'+quote(epic,safe=''),version='3')

    async def search_markets(self, search_term):
        return await self._request(
            'GET','/markets',params={'searchTerm':search_term},version='1')

    async def prices(self, epic, resolution='MINUTE', maximum=80,
                     from_time=None, to_time=None):
        res=_RES.get(str(resolution).upper(),str(resolution).upper())
        maximum=max(1,min(int(maximum),1000))
        path=f'/prices/{quote(epic,safe="")}/{res}/{maximum}'
        params={}
        ft=capital_api_time(from_time);tt=capital_api_time(to_time)
        if ft:params['from']=ft
        if tt:params['to']=tt
        return await self._request('GET',path,params=params or None,version='3')

    async def confirm(self, deal_reference):
        return await self._request(
            'GET','/confirms/'+quote(str(deal_reference),safe=''),version='1')

    async def transactions(self, *, last_period=600, from_time=None, to_time=None,
                           tx_type='ALL'):
        params={'type':tx_type or 'ALL','pageSize':50}
        ft=capital_api_time(from_time);tt=capital_api_time(to_time)
        if ft:params['from']=ft
        if tt:params['to']=tt
        if last_period is not None and not ft:
            # IG wants ISO from/to; approximate last_period window.
            from datetime import datetime, timedelta, timezone
            end=datetime.now(timezone.utc)
            start=end-timedelta(seconds=int(last_period))
            params['from']=capital_api_time(start)
            params['to']=capital_api_time(end)
        return await self._request('GET','/history/transactions',params=params,version='2')

    async def activity(self, *, last_period=600, from_time=None, to_time=None,
                       detailed=True, deal_id=None):
        params={'pageSize':50}
        ft=capital_api_time(from_time);tt=capital_api_time(to_time)
        if ft:params['from']=ft
        if tt:params['to']=tt
        if last_period is not None and not ft:
            from datetime import datetime, timedelta, timezone
            end=datetime.now(timezone.utc)
            start=end-timedelta(seconds=int(last_period))
            params['from']=capital_api_time(start)
            params['to']=capital_api_time(end)
        if deal_id:
            params['dealId']=str(deal_id)
        return await self._request('GET','/history/activity',params=params,version='3')

    async def open_position(self, epic, direction, size, stop_distance=None,
                            profit_distance=None, limit_distance=None):
        if direction not in ('BUY','SELL') or Decimal(str(size))<=0:
            raise ValueError('Invalid trade')
        payload={
            'epic':epic,
            'expiry':'-',
            'direction':direction,
            'size':float(size),
            'orderType':'MARKET',
            'guaranteedStop':False,
            'forceOpen':True,
            'currencyCode':'GBP',
        }
        if stop_distance is not None:
            payload['stopDistance']=float(stop_distance)
        distance=profit_distance if profit_distance is not None else limit_distance
        if distance is not None:
            payload['limitDistance']=float(distance)
        return await self._request('POST','/positions/otc',json=payload,version='2')

    async def close_position(self, deal_id):
        # Need live size/direction to close OTC — fetch first.
        live=await self.positions()
        match=None
        for raw in live:
            pos=raw.get('position') if isinstance(raw,dict) else None
            if not isinstance(pos,dict):
                continue
            if str(pos.get('dealId') or '')==str(deal_id):
                match=pos;break
        if not match:
            raise RuntimeError(f'IG position {deal_id} not found for close')
        direction=str(match.get('direction') or '').upper()
        opposite='SELL' if direction=='BUY' else 'BUY'
        payload={
            'dealId':str(deal_id),
            'direction':opposite,
            'size':float(match.get('size') or 0),
            'orderType':'MARKET',
        }
        return await self._request('DELETE','/positions/otc',json=payload,version='1')

    async def update_position(self, deal_id, stop_level=None, profit_level=None,
                              limit_level=None):
        payload={}
        if stop_level is not None:
            payload['stopLevel']=float(stop_level)
        tp=profit_level if profit_level is not None else limit_level
        if tp is not None:
            payload['limitLevel']=float(tp)
        if not payload:
            raise ValueError('No changes supplied')
        return await self._request(
            'PUT','/positions/otc/'+quote(str(deal_id),safe=''),
            json=payload,version='2')

    async def close(self, *, end_session: bool=True):
        if end_session and self.cst and self.security:
            try:
                await self.client.delete('/session',headers=self.headers(version='1'))
            except Exception:
                pass
        self.cst=None;self.security=None
        await self.client.aclose()
