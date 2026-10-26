"""Per-client robot runtimes — isolated store/brain/engine/worker/stream."""
from __future__ import annotations
import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .autonomy import AutoEvolution
from .brokers import create_broker
from .engine import AdaptiveBrain, SingleTradeEngine, Store
from .platform import PlatformStore
from .stream import CapitalQuoteStream

log=logging.getLogger('runtime_hub')


@dataclass
class AccountRuntime:
    client_id: int
    account_id: int
    data_dir: Path
    store: Store
    brain: AdaptiveBrain
    engine: SingleTradeEngine
    stream: CapitalQuoteStream
    worker_task: Optional[asyncio.Task]=None
    stream_task: Optional[asyncio.Task]=None
    last_error: Optional[str]=None
    started: bool=False

    def client_view(self) -> dict:
        snap=self.engine.snapshot()
        signal=snap.get('signal') or {}
        scores=signal.get('scores') or {}
        neural=snap.get('neural') or {}
        mandate=snap.get('mandate')
        positions=snap.get('positions') or []
        code=self.engine.policy.info() if self.engine.policy else {}
        phase='guide' if not mandate else 'robot'
        return {
            'phase':phase,
            'connected':bool(self.engine.broker),
            'execution_enabled':bool(snap.get('execution_enabled')),
            'armed':bool(snap.get('armed')),
            'operator_paused':bool(getattr(self.engine,'operator_paused',False)),
            'mandate':mandate,
            'position':positions[0] if positions else None,
            'scores':{
                'BUY':float(scores.get('BUY') or 0),
                'HOLD':float(scores.get('HOLD') or 0),
                'SELL':float(scores.get('SELL') or 0),
            },
            'action':signal.get('action'),
            'confidence':signal.get('confidence'),
            'evolution':{
                'generation':int(code.get('generation') or snap.get('code_generation') or 0),
                'strategy':code.get('strategy'),
                'attempts':int(code.get('attempts') or 0),
                'labeled_samples':int(snap.get('labeled_samples') or 0),
                'neurons':neural.get('neurons'),
                'steps':neural.get('steps'),
            },
            'market_status':(snap.get('market') or {}).get('status'),
            'error':self.last_error or snap.get('error'),
            'tick_count':snap.get('tick_count') or 0,
        }


class RuntimeHub:
    def __init__(self,platform: PlatformStore):
        self.platform=platform
        self._runtimes: dict[int,AccountRuntime]={}  # client_id -> runtime
        self._lock=asyncio.Lock()

    def get(self,client_id: int) -> Optional[AccountRuntime]:
        return self._runtimes.get(int(client_id))

    def all_runtimes(self) -> list[AccountRuntime]:
        return list(self._runtimes.values())

    async def start_account(self,broker_row: dict) -> AccountRuntime:
        client_id=int(broker_row['client_id'])
        async with self._lock:
            existing=self._runtimes.get(client_id)
            if existing and existing.started:
                await self._stop_unlocked(client_id)
            data_dir=self.platform.account_data_dir(client_id)
            store=Store(data_dir/'robot.sqlite3')
            brain=AdaptiveBrain(store,data_dir=data_dir)
            engine=SingleTradeEngine(store,brain)
            engine.policy=AutoEvolution(store,data_dir/'policy.py')
            stream=CapitalQuoteStream()
            engine.quote_stream=stream
            creds=self.platform.broker_credentials(broker_row)
            broker=create_broker(creds['broker_type'],creds['api_key'],creds['identifier'],
                                 creds['password'],live=True)
            try:
                await broker.connect()
                accounts=await broker.accounts()
                await broker.positions()
            except Exception:
                await broker.close()
                raise
            engine.attach(broker)
            engine.account=accounts
            try:
                await engine.reconcile_outstanding()
                await engine.sync()
            except Exception:
                await broker.close()
                engine.broker=None
                raise
            store.event('auth','Client broker session started',
                        {'client_id':client_id,'broker':creds['broker_type'],
                         'account_name':creds['display_name'],
                         'mandate_requires_new_manual_trade':engine.mandate is None})
            rt=AccountRuntime(client_id=client_id,account_id=int(broker_row['id']),
                              data_dir=data_dir,store=store,brain=brain,engine=engine,
                              stream=stream)
            rt.worker_task=asyncio.create_task(self._worker(rt),name=f'worker-c{client_id}')
            rt.stream_task=asyncio.create_task(stream.run(engine),name=f'stream-c{client_id}')
            rt.started=True
            self._runtimes[client_id]=rt
            return rt

    async def ensure_client_running(self,client_id: int) -> Optional[AccountRuntime]:
        rt=self.get(client_id)
        if rt and rt.started and rt.engine.broker:
            return rt
        broker=self.platform.get_broker_for_client(client_id)
        if not broker or not broker.get('active'):
            return None
        return await self.start_account(broker)

    async def start_all_active(self):
        for row in self.platform.list_active_broker_accounts():
            try:
                await self.start_account(row)
            except Exception as exc:
                log.warning('Failed to start client %s: %s',row.get('client_id'),type(exc).__name__)

    async def stop_client(self,client_id: int):
        async with self._lock:
            await self._stop_unlocked(client_id)

    async def _stop_unlocked(self,client_id: int):
        rt=self._runtimes.pop(int(client_id),None)
        if not rt:return
        rt.started=False
        for task in (rt.worker_task,rt.stream_task):
            if task:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
        rt.engine.cancel_peer_refresh()
        rt.engine.cancel_calendar_refresh()
        try:rt.brain.save()
        except Exception:pass
        if rt.engine.broker:
            try:await rt.engine.broker.close()
            except Exception:pass
            rt.engine.broker=None

    async def shutdown(self):
        async with self._lock:
            ids=list(self._runtimes.keys())
            for client_id in ids:
                await self._stop_unlocked(client_id)

    async def _worker(self,rt: AccountRuntime):
        error_count=0
        engine=rt.engine
        store=rt.store
        while True:
            try:
                if engine.broker:
                    if store.unresolved():await engine.reconcile_outstanding()
                    await engine.poll()
                    error_count=0
                    rt.last_error=None
                    engine.last_error=None
                    if (engine._paused_for_read and engine.mandate and not engine.operator_paused
                            and not store.unresolved() and len(engine.positions)<=1):
                        engine._paused_for_read=False
                        engine.armed=True
                        engine.execution_enabled=True
                        store.event('recovery','Read-only broker outage recovered; autonomous execution resumed')
                await asyncio.sleep(3 if error_count<3 else 10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                rt.last_error=f'{type(exc).__name__}: {str(exc)[:180]}'
                engine.last_error=rt.last_error
                error_count+=1
                frames=traceback.extract_tb(exc.__traceback__)
                local=next((f for f in reversed(frames) if '/app/' in f.filename.replace('\\','/')),
                           frames[-1] if frames else None)
                store.event('error','Broker/engine cycle error',{
                    'type':type(exc).__name__,'count':error_count,
                    'source':Path(local.filename).name if local else None,
                    'line':local.lineno if local else None,
                    'client_id':rt.client_id})
                if error_count>=3:
                    if engine.armed or engine.execution_enabled:
                        engine._paused_for_read=True
                    engine.armed=False
                    engine.execution_enabled=False
                await asyncio.sleep(3 if error_count<3 else 10)
