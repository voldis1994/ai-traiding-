"""Capital.com LIVE REST broker adapter, modelled on https://open-api.capital.com/.
Never automatically retry non-idempotent requests. Credentials stay in RAM.
"""
import asyncio
from decimal import Decimal
from urllib.parse import quote
import httpx

LIVE='https://api-capital.backend-capital.com'
DEMO='https://demo-api-capital.backend-capital.com'

class CapitalBroker:
    def __init__(self, api_key, identifier, password, live=True, client=None):
        self.api_key=api_key;self.identifier=identifier;self.password=password
        self.base=LIVE if live else DEMO
        self.client=client or httpx.AsyncClient(base_url=self.base, timeout=15.0, follow_redirects=False)
        self.cst=None;self.security=None
        self._relogin=asyncio.Lock()

    async def connect(self):
        # Non-encrypted password is sent over HTTPS per broker session API.
        response=await self.client.post('/api/v1/session',headers={'X-CAP-API-KEY':self.api_key},
            json={'identifier':self.identifier,'password':self.password,'encryptedPassword':False})
        response.raise_for_status()
        self.cst=response.headers.get('CST');self.security=response.headers.get('X-SECURITY-TOKEN')
        if not self.cst or not self.security:raise RuntimeError('Broker session tokens missing')
        return response.json()

    def headers(self):
        if not self.cst or not self.security:raise RuntimeError('Not authenticated')
        return {'CST':self.cst,'X-SECURITY-TOKEN':self.security,'X-CAP-API-KEY':self.api_key}

    async def _request(self,method,path,*,params=None,json=None):
        response=await self.client.request(method,path,headers=self.headers(),params=params,json=json)
        # Relogin only for reads. Retrying an order on network error is not safe.
        if response.status_code==401 and method=='GET':
            async with self._relogin:
                await self.connect()
            response=await self.client.request(method,path,headers=self.headers(),params=params,json=json)
        response.raise_for_status()
        return response.json()

    async def positions(self):return (await self._request('GET','/api/v1/positions')).get('positions',[])
    async def accounts(self):return await self._request('GET','/api/v1/accounts')
    async def ping(self):return await self._request('GET','/api/v1/ping')
    async def market(self,epic):return await self._request('GET','/api/v1/markets/'+quote(epic,safe=''))
    async def search_markets(self,search_term):
        return await self._request('GET','/api/v1/markets',params={'searchTerm':search_term})
    async def prices(self,epic,resolution='MINUTE',maximum=80):
        return await self._request('GET','/api/v1/prices/'+quote(epic,safe=''),params={'resolution':resolution,'max':maximum})
    async def confirm(self,deal_reference):
        return await self._request('GET','/api/v1/confirms/'+quote(deal_reference,safe=''))
    async def transactions(self,*,last_period=600,from_time=None,to_time=None,tx_type='TRADE'):
        """Account cash movements. Default last 10 minutes — includes closed-trade P&L."""
        params={}
        if from_time:params['from']=from_time
        if to_time:params['to']=to_time
        if last_period is not None and not from_time:params['lastPeriod']=int(last_period)
        if tx_type:params['type']=tx_type
        return await self._request('GET','/api/v1/history/transactions',params=params or None)
    async def activity(self,*,last_period=600,from_time=None,to_time=None,detailed=True,
                       deal_id=None):
        """Deal activity (SL/TP/USER closes) — often carries dealId + profit details."""
        params={'detailed':'true' if detailed else 'false'}
        if from_time:params['from']=from_time
        if to_time:params['to']=to_time
        if last_period is not None and not from_time:params['lastPeriod']=int(last_period)
        if deal_id:params['dealId']=str(deal_id)
        return await self._request('GET','/api/v1/history/activity',params=params)
    async def open_position(self,epic,direction,size,stop_distance=None,profit_distance=None,limit_distance=None):
        if direction not in ('BUY','SELL') or Decimal(str(size))<=0:raise ValueError('Invalid trade')
        payload={'epic':epic,'direction':direction,'size':float(size),'guaranteedStop':False}
        if stop_distance is not None:payload['stopDistance']=float(stop_distance)
        distance=profit_distance if profit_distance is not None else limit_distance
        if distance is not None:payload['profitDistance']=float(distance)
        return await self._request('POST','/api/v1/positions',json=payload)
    async def close_position(self,deal_id):
        return await self._request('DELETE','/api/v1/positions/'+quote(str(deal_id),safe=''))
    async def update_position(self,deal_id,stop_level=None,profit_level=None,limit_level=None):
        payload={}
        # Capital rejects stopLevel/profitLevel that are not on the price grid
        # (e.g. 4006.075). Pass through floats already snapped by protection_ai.
        if stop_level is not None:payload['stopLevel']=float(stop_level)
        if profit_level is not None or limit_level is not None:
            payload['profitLevel']=float(profit_level if profit_level is not None else limit_level)
        if not payload:raise ValueError('No changes supplied')
        return await self._request('PUT','/api/v1/positions/'+quote(str(deal_id),safe=''),json=payload)
    async def close(self, *, end_session: bool=True):
        # Free Capital.com session slot so a second connect (or another client) is not blocked.
        if end_session and self.cst and self.security:
            try:
                await self.client.delete('/api/v1/session',headers=self.headers())
            except Exception:
                pass
        self.cst=None;self.security=None
        await self.client.aclose()
