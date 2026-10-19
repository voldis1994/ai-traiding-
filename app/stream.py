"""Capital.com WebSocket quotes, authenticated with in-memory session tokens.
REST /markets remains the fallback and source of current dealing rules.
"""
import asyncio
import json
import time
import websockets

URL='wss://api-streaming-capital.backend-capital.com/connect'

class CapitalQuoteStream:
    def __init__(self):
        self.quote=None;self.last_error=None;self.connected=False
        self.subscription=None
    async def run(self,engine):
        while True:
            if not engine.broker or not engine.mandate:
                self.connected=False;await asyncio.sleep(3);continue
            epic=engine.mandate['epic'];broker=engine.broker
            try:
                async with websockets.connect(URL,open_timeout=12,ping_interval=120,ping_timeout=20,max_size=2**20) as socket:
                    self.connected=True;self.subscription=epic;self.last_error=None
                    credentials={'cst':broker.cst,'securityToken':broker.security}
                    await socket.send(json.dumps({'destination':'marketData.subscribe','correlationId':'1',
                        **credentials,'payload':{'epics':[epic]}}))
                    while engine.broker is broker and engine.mandate and engine.mandate['epic']==epic:
                        try:message=json.loads(await asyncio.wait_for(socket.recv(),timeout=120))
                        except asyncio.TimeoutError:
                            await socket.send(json.dumps({'destination':'ping','correlationId':'2',**credentials}))
                            continue
                        if message.get('destination')!='quote':continue
                        payload=message.get('payload',{})
                        if payload.get('epic')!=epic:continue
                        bid=payload.get('bid');ask=payload.get('ofr')
                        try:
                            if bid is not None and ask is not None and float(bid)>0 and float(ask)>=float(bid):
                                self.quote={'epic':epic,'bid':float(bid),'ask':float(ask),'at':time.monotonic(),
                                            'timestamp':payload.get('timestamp')}
                        except (ValueError,TypeError):continue
            except asyncio.CancelledError:raise
            except Exception as exc:self.last_error=type(exc).__name__
            finally:self.connected=False;self.quote=None
            await asyncio.sleep(6)
    def current(self,epic,max_age=9):
        q=self.quote
        return q if q and q['epic']==epic and time.monotonic()-q['at']<=max_age else None
