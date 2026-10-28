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
                'last_reason':code.get('last_reason'),
                'labeled_samples':int(snap.get('labeled_samples') or 0),
                'neurons':neural.get('neurons'),
                'steps':neural.get('steps'),
            },
            'bracket':snap.get('bracket') or {'kappa':2.0,'sl_scale':1.0},
            'bracket_learn_reason':snap.get('bracket_learn_reason'),
            'bracket_outcomes':int(snap.get('bracket_outcomes') or 0),
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

    def _trade_stats(self,store: Store, account_currency: str|None=None) -> dict:
        trades=store.trades(200)
        closed=[t for t in trades if t.get('closed')]
        open_rows=[t for t in trades if not t.get('closed')]
        account_currency=(account_currency or '').strip().upper() or None
        # Only sum broker cash rows. price_size / ticks_only are not account money.
        by_ccy: dict[str,list[float]]={}
        signed=[]
        recent=[]
        for t in closed:
            row=dict(t)
            src=(row.get('pnl_source') or '').strip().lower()
            # Broker cash is always account currency; older rows may still say USD
            # because GOLD position.currency was wrongly persisted as the label.
            if (account_currency and row.get('pnl') is not None and
                src not in ('ticks_only','price_size','pending_close_sync','position_disappeared')):
                stored=(row.get('pnl_currency') or '').strip().upper()
                if stored!=account_currency:
                    row['pnl_currency']=account_currency
                    if stored and row.get('deal_id'):
                        try:
                            store.close_trade(row['deal_id'],row.get('exit'),row.get('pnl'),
                                              currency=account_currency,source=src or 'broker',
                                              overwrite=True)
                        except Exception:
                            pass
            recent.append(row)
            if row.get('pnl') is None:continue
            if src in ('ticks_only','price_size','pending_close_sync','position_disappeared'):
                continue
            try:val=float(row['pnl'])
            except (TypeError,ValueError):continue
            ccy=(row.get('pnl_currency') or account_currency or '?').upper()
            by_ccy.setdefault(ccy,[]).append(val)
            signed.append(val)
        wins=sum(1 for p in signed if p>0)
        losses=sum(1 for p in signed if p<0)
        # Prefer a single currency total; if mixed, keep map and avoid one fake sum.
        primary=None
        if len(by_ccy)==1:
            ccy,vals=next(iter(by_ccy.items()))
            primary={'currency':ccy,'realized_pnl':round(sum(vals),4)}
        elif account_currency and account_currency in by_ccy:
            primary={'currency':account_currency,
                     'realized_pnl':round(sum(by_ccy[account_currency]),4)}
        return {
            'closed_trades':len(closed),
            'open_trades_recorded':len(open_rows),
            'realized_pnl':primary['realized_pnl'] if primary else 0.0,
            'pnl_currency':primary['currency'] if primary else (account_currency or None),
            'pnl_by_currency':{k:round(sum(v),4) for k,v in by_ccy.items()},
            'wins':wins,
            'losses':losses,
            'win_rate':round(wins/max(1,wins+losses),3),
            'recent_trades':recent[:12],
        }

    def admin_client_card(self,client: dict) -> dict:
        """Full operator card for one client — neural, activity, results."""
        cid=int(client['id'])
        rt=self.get(cid)
        card={
            **client,
            'robot_running':False,
            'phase':None,
            'connected':False,
            'operator_paused':False,
            'execution_enabled':False,
            'armed':False,
            'scores':{'BUY':0.,'HOLD':0.,'SELL':0.},
            'action':None,
            'evolution':{
                'generation':0,'labeled_samples':0,'neurons':None,'steps':0,
                'accuracy_proxy':0.,'births':0,'pruned':0,'loss':None,
                'smoothed_error':None,'utility_mean':None,
            },
            'activity':{
                'tick_count':0,'market':None,'mandate':None,'position':None,
                'entry_block_reason':None,'error':None,
            },
            'results':{
                'closed_trades':0,'open_trades_recorded':0,'realized_pnl':0.0,
                'pnl_currency':None,'pnl_by_currency':{},
                'wins':0,'losses':0,'win_rate':0.0,'recent_trades':[],
            },
            'account_currency':None,
        }
        store=None
        account_currency=None
        if rt and rt.started:
            view=rt.client_view()
            snap=rt.engine.snapshot()
            neural=snap.get('neural') or {}
            evo=view.get('evolution') or {}
            try:policy_info=rt.engine.policy.info() if rt.engine.policy else {}
            except Exception:policy_info={}
            store=rt.store
            account_currency=((snap.get('account') or {}).get('currency') or '').strip().upper() or None
            pos=view.get('position') or {}
            card.update({
                'robot_running':True,
                'phase':view.get('phase'),
                'connected':bool(view.get('connected')),
                'operator_paused':bool(view.get('operator_paused')),
                'execution_enabled':bool(view.get('execution_enabled')),
                'armed':bool(view.get('armed')),
                'scores':view.get('scores') or card['scores'],
                'action':view.get('action'),
                # Never fall back to position.currency (GOLD quote USD ≠ account cash).
                'account_currency':account_currency,
                'evolution':{
                    'generation':int(evo.get('generation') or policy_info.get('generation') or 0),
                    'labeled_samples':int(evo.get('labeled_samples') or snap.get('labeled_samples') or 0),
                    'neurons':neural.get('neurons'),
                    'steps':int(neural.get('steps') or 0),
                    'accuracy_proxy':round(float(snap.get('accuracy_proxy') or 0),4),
                    'births':int(neural.get('births') or 0),
                    'pruned':int(neural.get('pruned') or 0),
                    'loss':neural.get('loss'),
                    'smoothed_error':neural.get('smoothed_error'),
                    'utility_mean':neural.get('utility_mean'),
                    'strategy':evo.get('strategy') or policy_info.get('strategy'),
                    'attempts':int(policy_info.get('attempts') or evo.get('attempts') or 0),
                    'last_reason':policy_info.get('last_reason') or evo.get('last_reason'),
                },
                'activity':{
                    'tick_count':int(view.get('tick_count') or snap.get('tick_count') or 0),
                    'market':(view.get('mandate') or {}).get('epic') or (snap.get('market') or {}).get('epic'),
                    'market_status':(snap.get('market') or {}).get('status'),
                    'mandate':view.get('mandate'),
                    'position':view.get('position'),
                    'entry_block_reason':snap.get('entry_block_reason'),
                    'error':view.get('error') or snap.get('error'),
                    'balance':((snap.get('account') or {}).get('balance') or {}),
                    'currency':account_currency,
                },
            })
        else:
            data_dir=self.platform.account_data_dir(cid)
            db=data_dir/'robot.sqlite3'
            if db.is_file():
                try:store=Store(db)
                except Exception:store=None
        if store is not None:
            try:card['results']=self._trade_stats(store,card.get('account_currency'))
            except Exception:pass
        return card

    def admin_overview(self) -> dict:
        clients=[self.admin_client_card(c) for c in self.platform.list_clients()]
        running=sum(1 for c in clients if c.get('robot_running'))
        total_closed=sum(int((c.get('results') or {}).get('closed_trades') or 0) for c in clients)
        # Never add GBP + USD into one number.
        by_ccy: dict[str,float]={}
        for c in clients:
            res=c.get('results') or {}
            for ccy,val in (res.get('pnl_by_currency') or {}).items():
                by_ccy[ccy]=round(by_ccy.get(ccy,0.0)+float(val or 0),4)
            if not res.get('pnl_by_currency') and res.get('pnl_currency') and res.get('realized_pnl') is not None:
                ccy=res['pnl_currency']
                by_ccy[ccy]=round(by_ccy.get(ccy,0.0)+float(res.get('realized_pnl') or 0),4)
        primary=None
        if len(by_ccy)==1:
            ccy,val=next(iter(by_ccy.items()))
            primary={'currency':ccy,'realized_pnl':val}
        return {
            'summary':{
                'clients':len(clients),
                'running_robots':running,
                'realized_pnl_all':primary['realized_pnl'] if primary else None,
                'pnl_currency':primary['currency'] if primary else None,
                'pnl_by_currency':by_ccy,
                'closed_trades_all':total_closed,
            },
            'clients':clients,
        }

    async def start_account(self,broker_row: dict, *, connected_broker=None) -> AccountRuntime:
        """Start robot. Pass connected_broker to avoid a second Capital.com login."""
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
            broker=connected_broker
            owns_connect=broker is None
            if owns_connect:
                broker=create_broker(creds['broker_type'],creds['api_key'],creds['identifier'],
                                     creds['password'],live=True)
                try:
                    await broker.connect()
                    accounts=await broker.accounts()
                    await broker.positions()
                except Exception:
                    await broker.close()
                    raise
            else:
                try:
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
