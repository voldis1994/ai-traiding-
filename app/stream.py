"""Capital.com WebSocket quotes (audit P1-04).

- Regular application ping timer (Capital requires ≥1 ping / 10 min; we use 60s)
- connected=True only after subscribe acknowledgement
- Broker timestamp monotonicity + quote age
- Fresh CST/security tokens on reconnect
"""
import asyncio
import json
import time
import websockets

URL='wss://api-streaming-capital.backend-capital.com/connect'
APP_PING_INTERVAL_S=60.0
RECV_TIMEOUT_S=30.0
SUBSCRIBE_ACK_TIMEOUT_S=15.0


class CapitalQuoteStream:
    def __init__(self):
        self.quote=None
        self.last_error=None
        self.connected=False
        self.subscription=None
        self.subscribed=False
        self.last_broker_ts=None
        self.last_ping_at=None
        self.quote_age_s=None
        self.reconnects=0

    def _credentials(self,broker):
        return {'cst':broker.cst,'securityToken':broker.security}

    def _parse_broker_ts(self,raw):
        if raw in (None,''):
            return None
        try:
            # Capital may send epoch ms or ISO.
            if isinstance(raw,(int,float)) or str(raw).isdigit():
                v=float(raw)
                if v>1e12:
                    v=v/1000.0
                return v
            text=str(raw).replace('Z','+00:00')
            from datetime import datetime,timezone
            dt=datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    async def _send_ping(self,socket,credentials):
        await socket.send(json.dumps({
            'destination':'ping','correlationId':'ping',**credentials}))
        self.last_ping_at=time.monotonic()

    async def _await_subscribe_ack(self,socket,epic,credentials):
        """Wait for subscribe status; treat first matching quote as soft ack."""
        deadline=time.monotonic()+SUBSCRIBE_ACK_TIMEOUT_S
        while time.monotonic()<deadline:
            remaining=max(0.1,deadline-time.monotonic())
            try:
                raw=await asyncio.wait_for(socket.recv(),timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                message=json.loads(raw)
            except Exception:
                continue
            dest=str(message.get('destination') or '')
            status=str(message.get('status') or message.get('payload',{}).get('status') or '').upper()
            if 'subscribe' in dest.lower() and status in ('OK','SUCCESS','SUBSCRIBED',''):
                # Explicit ack (or empty status on some Capital builds).
                if status in ('REJECTED','ERROR','FAILED'):
                    self.last_error=f'subscribe_{status.lower()}'
                    return False
                return True
            if dest=='quote':
                payload=message.get('payload') or {}
                if payload.get('epic')==epic:
                    self._apply_quote_payload(epic,payload,engine=None)
                    return True
            if dest=='ping' or 'pong' in dest.lower():
                continue
        # No explicit ack — still allow stream if quotes arrive later,
        # but do not mark connected until first good quote.
        return False

    def _apply_quote_payload(self,epic,payload,engine):
        bid=payload.get('bid');ask=payload.get('ofr')
        try:
            if bid is None or ask is None:
                return False
            bid_f=float(bid);ask_f=float(ask)
            if bid_f<=0 or ask_f<bid_f:
                return False
        except (ValueError,TypeError):
            return False
        broker_ts=self._parse_broker_ts(payload.get('timestamp') or payload.get('ut'))
        if broker_ts is not None and self.last_broker_ts is not None:
            if broker_ts<self.last_broker_ts:
                # Stale / out-of-order — keep last good quote.
                self.last_error='stale_broker_ts'
                return False
        if broker_ts is not None:
            self.last_broker_ts=broker_ts
        now_m=time.monotonic()
        self.quote={
            'epic':epic,'bid':bid_f,'ask':ask_f,'at':now_m,
            'timestamp':payload.get('timestamp'),
            'broker_ts':broker_ts,
        }
        self.quote_age_s=0.0
        if engine is not None and hasattr(engine,'_apply_stream_quote'):
            try:
                engine._apply_stream_quote()
            except Exception as exc:
                self.last_error=f'apply_quote:{type(exc).__name__}'
                # Still keep quote — Desk can show age/error.
        return True

    async def run(self,engine):
        while True:
            if not engine.broker or not engine.mandate:
                self.connected=False
                self.subscribed=False
                await asyncio.sleep(3)
                continue
            epic=engine.mandate['epic']
            broker=engine.broker
            self.connected=False
            self.subscribed=False
            try:
                async with websockets.connect(
                        URL,open_timeout=12,ping_interval=None,
                        ping_timeout=None,max_size=2**20) as socket:
                    credentials=self._credentials(broker)
                    await socket.send(json.dumps({
                        'destination':'marketData.subscribe','correlationId':'1',
                        **credentials,'payload':{'epics':[epic]}}))
                    acked=await self._await_subscribe_ack(socket,epic,credentials)
                    if acked:
                        self.subscribed=True
                        self.connected=True
                        self.subscription=epic
                        self.last_error=None
                    else:
                        # Stay in recv loop; first good quote will mark connected.
                        self.subscription=epic
                        self.last_error=self.last_error or 'subscribe_pending'
                    await self._send_ping(socket,credentials)
                    ping_task=asyncio.create_task(self._ping_loop(socket,credentials))
                    try:
                        while (engine.broker is broker and engine.mandate
                               and engine.mandate['epic']==epic):
                            try:
                                message=json.loads(await asyncio.wait_for(
                                    socket.recv(),timeout=RECV_TIMEOUT_S))
                            except asyncio.TimeoutError:
                                # Recv quiet — ping loop still fires; continue.
                                if self.quote:
                                    self.quote_age_s=time.monotonic()-float(self.quote['at'])
                                continue
                            dest=str(message.get('destination') or '')
                            if dest in ('ping','pong') or 'pong' in dest.lower():
                                continue
                            if 'subscribe' in dest.lower():
                                status=str(message.get('status')
                                           or (message.get('payload') or {}).get('status')
                                           or '').upper()
                                if status in ('REJECTED','ERROR','FAILED'):
                                    self.last_error=f'subscribe_{status.lower()}'
                                    self.connected=False
                                    self.subscribed=False
                                    break
                                self.subscribed=True
                                self.connected=True
                                continue
                            if dest!='quote':
                                continue
                            payload=message.get('payload') or {}
                            if payload.get('epic')!=epic:
                                continue
                            if self._apply_quote_payload(epic,payload,engine):
                                if not self.connected:
                                    self.connected=True
                                    self.subscribed=True
                                    self.last_error=None
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error=type(exc).__name__
            finally:
                self.connected=False
                self.subscribed=False
                self.quote=None
                self.quote_age_s=None
                self.reconnects+=1
            await asyncio.sleep(6)

    async def _ping_loop(self,socket,credentials):
        """Application-level ping on a fixed timer, even during continuous quotes."""
        while True:
            await asyncio.sleep(APP_PING_INTERVAL_S)
            try:
                await self._send_ping(socket,credentials)
            except Exception:
                return

    def current(self,epic,max_age=9):
        q=self.quote
        if not q or q['epic']!=epic:
            return None
        age=time.monotonic()-q['at']
        self.quote_age_s=age
        return q if age<=max_age else None
