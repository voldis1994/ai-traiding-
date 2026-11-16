"""Per-client robot runtimes — isolated store/brain/engine/worker/stream."""
from __future__ import annotations
import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .brokers import create_broker
from .core import client_neural_seed
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
    last_start_error: Optional[str]=None
    started: bool=False

    @staticmethod
    def _deal_protection_truth(engine, position, brief=None) -> dict:
        """Admin-facing SL/TP/trail truth for one live deal."""
        p=position if isinstance(position,dict) else {}
        deal=str(p.get('dealId') or '')
        brief=brief if isinstance(brief,dict) else (getattr(engine,'protection_brief',None) or {})
        store=getattr(engine,'store',None)
        robot=bool(store and deal and store.get('robot_open:'+deal))
        meta=(store.get('adaptive_protection:'+deal) if store and deal else None) or {}
        runtime=(store.get('adaptive_runtime:'+deal) if store and deal else None) or {}
        has_sl=p.get('stopLevel') is not None
        has_tp=p.get('profitLevel') is not None
        enrolled=bool(isinstance(meta,dict) and meta.get('owner')=='AUTO')
        phase='flat'
        if not deal:
            phase='unknown'
        elif not robot:
            phase='manual'
        elif not enrolled:
            phase='watch'
        else:
            prop=brief.get('proposal') if isinstance(brief.get('proposal'),dict) else {}
            cand=(runtime.get('phase') or prop.get('phase')
                  or brief.get('phase') or '')
            status=str(brief.get('status') or '').lower()
            if cand in ('watch','armed','ratchet','be_lock','trail'):
                phase=cand
            elif 'waiting for adaptive arm' in status:
                phase='watch'
            elif 'confirmed trail' in status or runtime.get('status')=='CONFIRMED':
                phase='trail'
            elif 'be' in status and 'lock' in status:
                phase='be_lock'
            elif 'ratchet' in status:
                phase='ratchet'
            elif enrolled:
                phase='armed'
            else:
                phase='watch'
        # Only attach brief text when it belongs to this deal (or sole open).
        brief_deal=str(brief.get('deal_id') or '')
        sole=len(getattr(engine,'positions',None) or [])==1
        status_txt=None
        if brief_deal==deal or (sole and brief.get('status')):
            status_txt=brief.get('status')
        return {
            'has_sl':has_sl,
            'has_tp':has_tp,
            'robot_open':robot,
            'enrolled':enrolled,
            'trail_phase':phase,
            'trail_status':status_txt,
            'original_risk':meta.get('original_risk') if isinstance(meta,dict) else None,
            'last_trail_stop':runtime.get('stop') if isinstance(runtime,dict) else None,
        }

    def _enrich_positions(self,engine,positions,brief=None) -> list:
        out=[]
        for p in positions or []:
            if not isinstance(p,dict):
                continue
            truth=self._deal_protection_truth(engine,p,brief=brief)
            out.append({
                'dealId':p.get('dealId'),
                'epic':p.get('epic'),
                'direction':p.get('direction'),
                'size':p.get('size'),
                'level':p.get('level'),
                'stopLevel':p.get('stopLevel'),
                'profitLevel':p.get('profitLevel'),
                'upl':p.get('upl'),
                'currency':p.get('currency'),
                **truth,
            })
        return out

    def client_view(self) -> dict:
        snap=self.engine.snapshot()
        signal=snap.get('signal') or {}
        scores=signal.get('scores') or {}
        neural=snap.get('neural') or {}
        mandate=snap.get('mandate')
        positions=snap.get('positions') or []
        manage=snap.get('manage_signal') if isinstance(snap.get('manage_signal'),dict) else None
        brief=snap.get('adaptive_protection') if isinstance(snap.get('adaptive_protection'),dict) else None
        enriched=self._enrich_positions(self.engine,positions,brief=brief)
        # After restart last_signal is empty until the first completed M1+fusion
        # vote — surface the latest journaled scores so admin is not stuck at 0%.
        # Never do this while a LIVE deal is open: journal rows may be mid-trade
        # perceive noise and would make BUY/HOLD/SELL dance in the UI.
        if (not positions and not signal.get('action') and not any(scores.values())):
            try:
                rows=self.store.signals(1)
                if rows:
                    raw=rows[0].get('scores')
                    if isinstance(raw,str):
                        import json as _json
                        raw=_json.loads(raw)
                    if isinstance(raw,dict) and any(raw.values()):
                        scores=raw
                        signal={**signal,'action':rows[0].get('action'),
                                'confidence':max((float(v) for v in raw.values() if v is not None),default=None),
                                'from_journal':True}
            except Exception:
                pass
        # Open deal with cleared last_signal (adopt/restart): show locked OPEN or
        # first manage vote so Admin is not stuck at 0% forever.
        if positions and not any(float(scores.get(k) or 0) for k in ('BUY','HOLD','SELL')):
            for src in (signal, manage):
                if not isinstance(src,dict):
                    continue
                raw=src.get('scores')
                if isinstance(raw,dict) and any(float(raw.get(k) or 0) for k in ('BUY','HOLD','SELL')):
                    scores=raw
                    if not signal.get('action') and src.get('action'):
                        signal={**signal,'action':src.get('action'),
                                'from_manage':src is manage}
                    break
        code=self.engine.policy.info() if self.engine.policy else {}
        phase='guide' if not mandate else 'robot'
        # Mid-trade: prefer reverse-watch status over a stale "M1 label updated…"
        # reason that still sits in the store from before the deal opened.
        if positions and manage and manage.get('action'):
            adapt_reason=('Reverse watch {0} (manage)'.format(manage.get('action')))
        else:
            adapt_reason=(snap.get('neural_adapt_reason') or code.get('last_reason'))
        day_book=snap.get('day_book') if isinstance(snap.get('day_book'),dict) else None
        return {
            'phase':phase,
            'connected':bool(self.engine.broker),
            'execution_enabled':bool(snap.get('execution_enabled')),
            'armed':bool(snap.get('armed')),
            'operator_paused':bool(getattr(self.engine,'operator_paused',False)),
            'mandate':mandate,
            'position':enriched[0] if enriched else None,
            'positions':enriched,
            'protection':brief,
            'day_book':day_book,
            'scores':{
                'BUY':float(scores.get('BUY') or 0),
                'HOLD':float(scores.get('HOLD') or 0),
                'SELL':float(scores.get('SELL') or 0),
            },
            'action':signal.get('action') or (manage or {}).get('action'),
            # Uncalibrated model score — not calibrated profit probability (audit #24).
            'confidence':signal.get('confidence'),
            'model_score':signal.get('model_score',signal.get('confidence')),
            'model_score_is_calibrated_profit_probability':False,
            'signal_from_journal':bool(signal.get('from_journal')),
            'evolution':{
                # Paaudze = LIVE neural advances only (learning_generation).
                'generation':int(snap.get('learning_generation') or snap.get('code_generation') or 0),
                'learning_generation':int(snap.get('learning_generation') or 0),
                'policy_generation':int(snap.get('policy_generation') or code.get('generation') or 0),
                'strategy':code.get('strategy'),
                'attempts':int(code.get('attempts') or 0),
                'last_reason':adapt_reason,
                'neural_adapt_reason':adapt_reason,
                'model_version':neural.get('model_version') or snap.get('neural',{}).get('model_version'),
                'labeled_samples':int(snap.get('labeled_samples') or 0),
                'neurons':neural.get('neurons'),
                'steps':neural.get('steps'),
                # Minute-direction only — never alias as trade win rate (audit #24).
                'minute_direction_accuracy':snap.get('minute_direction_accuracy'),
            },
            'bracket':snap.get('bracket') or {'kappa':2.0,'sl_scale':1.0},
            'bracket_learn_reason':snap.get('bracket_learn_reason'),
            'bracket_outcomes':int(snap.get('bracket_outcomes') or 0),
            'market_status':(snap.get('market') or {}).get('status'),
            'entry_block_reason':snap.get('entry_block_reason'),
            'entry_can_submit_now':bool(snap.get('entry_can_submit_now')),
            'model_readiness':snap.get('model_readiness') or snap.get('prep_eval'),
            'hard_risk':snap.get('hard_risk'),
            'neural_close_diagnostic_only':bool(snap.get('neural_close_diagnostic_only',False)),
            'neural_close_executable':bool(snap.get('neural_close_executable',True)),
            'neural_exit_intent':snap.get('neural_exit_intent'),
            'manage_signal':manage,
            'neural_seed':int(getattr(self.brain,'init_seed',0) or 0),
            'error':self.last_error or snap.get('error'),
            'tick_count':snap.get('tick_count') or 0,
        }


class RuntimeHub:
    def __init__(self,platform: PlatformStore):
        self.platform=platform
        self._runtimes: dict[int,AccountRuntime]={}  # client_id -> runtime
        self._lock=asyncio.Lock()
        # Sticky start failures so admin sees why a client never came up after reboot.
        self._start_errors: dict[int,str]={}

    def get(self,client_id: int) -> Optional[AccountRuntime]:
        return self._runtimes.get(int(client_id))

    def all_runtimes(self) -> list[AccountRuntime]:
        return list(self._runtimes.values())

    @staticmethod
    def _cash_pnl_source(src) -> str:
        """Classify journal source: cash | pending | estimate | proxy."""
        s=(src or '').strip().lower()
        if s in ('ticks_only','price_size','position_disappeared'):
            return 'proxy'
        if s in ('pending_close_sync','pending_broker_pnl'):
            return 'pending'
        # Last UPL shown while waiting for Capital cash — display only, not broker £.
        if s in ('upl_estimate',):
            return 'estimate'
        # Explicit broker, or legacy blank source with a settled pnl figure.
        if s in ('broker','') or not s:
            return 'cash'
        return 'other'

    def _trade_stats(self,store: Store, account_currency: str|None=None,
                     open_upl: float|None=None) -> dict:
        acct=(account_currency or '').strip().upper() or None
        # Fix historical GOLD→USD tags so admin P&L matches Capital account cash.
        if acct:
            try:store.repair_broker_pnl_currency(acct)
            except Exception:pass
        # Full history — Admin P&L must NOT silently drop older closes past a
        # trades(200) window (that made −13 look like −3).
        cols=('deal_id','epic','size','direction','opened','closed','entry','exit',
              'pnl','pnl_currency','pnl_source')
        closed_rows=[dict(zip(cols,r)) for r in store.db.execute(
            'SELECT deal_id,epic,size,direction,opened,closed,entry,exit,pnl,'
            'pnl_currency,pnl_source FROM trades WHERE closed IS NOT NULL '
            'ORDER BY closed DESC')]
        open_n=int(store.db.execute(
            'SELECT COUNT(*) FROM trades WHERE closed IS NULL').fetchone()[0] or 0)
        by_ccy: dict[str,list[float]]={}
        signed=[]
        estimate_signed=[]
        recent=[]
        cash_n=0
        pending_n=0
        proxy_n=0
        estimate_n=0
        estimate_sum=0.0
        blank_n=0
        for t in closed_rows:
            kind=self._cash_pnl_source(t.get('pnl_source'))
            if kind=='proxy':
                proxy_n+=1
                if len(recent)<12:
                    recent.append({**t,'pnl_currency':(
                        (t.get('pnl_currency') or '').strip().upper() or acct or '?')})
                continue
            raw_ccy=(t.get('pnl_currency') or '').strip().upper() or None
            val=None
            if t.get('pnl') is not None:
                try:val=float(t['pnl'])
                except (TypeError,ValueError):val=None
            # Broker / legacy cash → account currency when known.
            if kind=='cash' and acct:
                ccy=acct
            else:
                ccy=raw_ccy or acct or '?'
            row={**t,'pnl_currency':ccy}
            if val is not None and kind=='cash':
                cash_n+=1
                by_ccy.setdefault(ccy,[]).append(val)
                if not acct or ccy==acct:
                    signed.append(val)
                if len(recent)<12:
                    recent.append(row)
            elif kind=='pending':
                pending_n+=1
                if val is None:blank_n+=1
                if len(recent)<12:
                    recent.append(row)
            elif kind=='estimate' and val is not None:
                estimate_n+=1
                estimate_sum+=float(val)
                estimate_signed.append(float(val))
                if len(recent)<12:
                    recent.append(row)
            elif kind=='other' or (kind=='cash' and val is None):
                if kind=='cash' and val is None:blank_n+=1
                if len(recent)<12:
                    recent.append(row)
        wins=sum(1 for p in signed if p>0)
        losses=sum(1 for p in signed if p<0)
        est_wins=sum(1 for p in estimate_signed if p>0)
        est_losses=sum(1 for p in estimate_signed if p<0)
        primary=None
        if len(by_ccy)==1:
            ccy,vals=next(iter(by_ccy.items()))
            primary={'currency':ccy,'realized_pnl':round(sum(vals),4)}
        elif acct and acct in by_ccy:
            primary={'currency':acct,
                     'realized_pnl':round(sum(by_ccy[acct]),4)}
        # Never invent 0.00 when multi-currency cash cannot be summed.
        closed_n=len(closed_rows)
        if primary:
            realized=primary['realized_pnl']
        elif by_ccy:
            realized=None
        elif closed_n>0 and cash_n==0:
            # Never invent a fake 0.00 when closes lack broker cash.
            realized=None
        else:
            realized=0.0
        # Honest status: 13 closes with £0 is NOT "fine" when broker cash never landed.
        if closed_n>0 and cash_n==0:
            pnl_status='closed_without_broker_cash'
        elif pending_n>0 or blank_n>0:
            pnl_status='broker_cash_pending'
        elif cash_n>0:
            pnl_status='broker_cash_ok'
        else:
            pnl_status='no_closes'
        # Operator display: prefer broker £; else UPL-estimate sum so Admin is
        # not stuck on lying 0 / empty while Capital history is pending.
        if realized is not None and cash_n>0:
            working_pnl=realized
            working_pnl_source='broker'
        elif estimate_n>0:
            working_pnl=round(estimate_sum,4)
            working_pnl_source='upl_estimate'
        else:
            working_pnl=realized
            working_pnl_source=('broker' if realized is not None and cash_n>0
                                else ('none' if closed_n else 'empty'))
        if cash_n>0:
            display_wins,display_losses,wl_source=wins,losses,'broker'
        elif estimate_n>0:
            display_wins,display_losses,wl_source=est_wins,est_losses,'upl_estimate'
        else:
            display_wins,display_losses,wl_source=0,0,'none'
        upl=None
        if open_upl is not None:
            try:
                u=float(open_upl)
                if __import__('math').isfinite(u):
                    upl=round(u,4)
            except (TypeError,ValueError):
                upl=None
        base_for_equity=working_pnl if working_pnl is not None else realized
        if base_for_equity is not None and upl is not None:
            equity=round(float(base_for_equity)+float(upl),4)
        elif base_for_equity is not None:
            equity=base_for_equity
        elif upl is not None:
            equity=upl
        else:
            equity=None
        return {
            'closed_trades':closed_n,
            'open_trades_recorded':open_n,
            'cash_trades':cash_n,
            'pending_cash_trades':pending_n,
            'proxy_trades':proxy_n,
            'estimate_trades':estimate_n,
            'estimate_pnl':round(estimate_sum,4) if estimate_n else None,
            'estimate_wins':est_wins,
            'estimate_losses':est_losses,
            'blank_pnl_trades':blank_n,
            'pnl_status':pnl_status,
            'realized_pnl':realized,
            'working_pnl':working_pnl,
            'working_pnl_source':working_pnl_source,
            'open_upl':upl,
            'equity_pnl':equity,
            'pnl_currency':primary['currency'] if primary else acct,
            'pnl_by_currency':{k:round(sum(v),4) for k,v in by_ccy.items()},
            'wins':wins,
            'losses':losses,
            'display_wins':display_wins,
            'display_losses':display_losses,
            'wl_source':wl_source,
            # Closed-trade win rate — NOT minute-direction accuracy (audit #24).
            'win_rate':round(display_wins/max(1,display_wins+display_losses),3),
            'trade_win_rate':round(display_wins/max(1,display_wins+display_losses),3),
            'metric':'closed_trade_win_rate',
            'recent_trades':recent[:12],
            # Compact ledger for Admin cards — every closed row with a number.
            'pnl_ledger':[
                {'pnl':float(v),'source':'broker'} for v in signed
            ]+[
                {'pnl':float(v),'source':'upl_estimate'} for v in estimate_signed
            ],
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
                'positions':[],'protection':None,'day_book':None,
                'entry_block_reason':None,'error':None,'last_start_error':None,
            },
            'last_start_error':self._start_errors.get(cid),
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
                # Never fall back to position.currency (GOLD quote USD ≠ account GBP).
                'account_currency':account_currency,
                'evolution':{
                    'generation':int(evo.get('learning_generation') or snap.get('learning_generation')
                                     or evo.get('generation') or snap.get('code_generation') or 0),
                    'learning_generation':int(evo.get('learning_generation')
                                             or snap.get('learning_generation') or 0),
                    'policy_generation':int(evo.get('policy_generation')
                                           or snap.get('policy_generation')
                                           or policy_info.get('generation') or 0),
                    'labeled_samples':int(evo.get('labeled_samples') or snap.get('labeled_samples') or 0),
                    'neurons':neural.get('neurons'),
                    'steps':int(neural.get('steps') or 0),
                    # Keep accuracy_proxy as minute-direction only; do not merge win rate.
                    'accuracy_proxy':round(float(snap.get('minute_direction_accuracy')
                                                 or snap.get('accuracy_proxy') or 0),4),
                    'minute_direction_accuracy':(
                        round(float(snap['minute_direction_accuracy']),4)
                        if snap.get('minute_direction_accuracy') is not None else None),
                    'births':int(neural.get('births') or 0),
                    'pruned':int(neural.get('pruned') or 0),
                    'loss':neural.get('loss'),
                    'smoothed_error':neural.get('smoothed_error'),
                    'utility_mean':neural.get('utility_mean'),
                    'strategy':evo.get('strategy') or policy_info.get('strategy'),
                    'attempts':int(policy_info.get('attempts') or evo.get('attempts') or 0),
                    'last_reason':(snap.get('neural_adapt_reason') or evo.get('neural_adapt_reason')
                                   or evo.get('last_reason') or policy_info.get('last_reason')),
                    'neural_adapt_reason':snap.get('neural_adapt_reason') or evo.get('neural_adapt_reason'),
                    'model_version':neural.get('model_version'),
                },
                'activity':{
                    'tick_count':int(view.get('tick_count') or snap.get('tick_count') or 0),
                    'market':(view.get('mandate') or {}).get('epic') or (snap.get('market') or {}).get('epic'),
                    'market_status':(snap.get('market') or {}).get('status'),
                    'mandate':view.get('mandate'),
                    'position':view.get('position'),
                    'positions':view.get('positions') or (
                        [view['position']] if view.get('position') else []),
                    'protection':view.get('protection') or snap.get('adaptive_protection'),
                    'day_book':view.get('day_book') or snap.get('day_book'),
                    'entry_block_reason':view.get('entry_block_reason') or snap.get('entry_block_reason'),
                    'entry_can_submit_now':bool(view.get('entry_can_submit_now')
                                               or snap.get('entry_can_submit_now')),
                    'model_readiness':snap.get('model_readiness') or view.get('model_readiness'),
                    'neural_close_diagnostic_only':True,
                    'neural_close_executable':False,
                    'neural_exit_intent':snap.get('neural_exit_intent'),
                    'neural_seed':view.get('neural_seed'),
                    'error':view.get('error') or snap.get('error'),
                    'last_start_error':None,
                    'balance':((snap.get('account') or {}).get('balance') or {}),
                    'currency':account_currency,
                },
                'last_start_error':None,
            })
            self._start_errors.pop(cid,None)
        else:
            card['activity']['last_start_error']=self._start_errors.get(cid)
            card['last_start_error']=self._start_errors.get(cid)
            data_dir=self.platform.account_data_dir(cid)
            db=data_dir/'robot.sqlite3'
            if db.is_file():
                try:store=Store(db)
                except Exception:store=None
        if store is not None:
            try:
                upl=None
                pos=card.get('activity',{}).get('position') or {}
                if isinstance(pos,dict) and pos.get('upl') is not None:
                    try:upl=float(pos['upl'])
                    except (TypeError,ValueError):upl=None
                card['results']=self._trade_stats(
                    store,card.get('account_currency'),open_upl=upl)
            except Exception:pass
            try:
                from . import hard_risk
                card['hard_risk']=hard_risk.snapshot(store)
            except Exception:
                card['hard_risk']=None
            # Day book is the live risk book the robot already uses — surface it
            # even when the worker is stopped so Admin Riski is never blank.
            if not (card.get('activity') or {}).get('day_book'):
                try:
                    from . import day_book
                    equity=None
                    bal=((card.get('activity') or {}).get('balance') or {})
                    for key in ('balance','available','deposit','equity'):
                        if bal.get(key) is not None:
                            try:equity=float(bal[key]);break
                            except (TypeError,ValueError):pass
                    positions=((card.get('activity') or {}).get('positions')
                               or ([card['activity']['position']]
                                   if (card.get('activity') or {}).get('position') else []))
                    card['activity']['day_book']=day_book.snapshot(
                        store,equity=equity,positions=positions)
                except Exception:
                    pass
        else:
            card['hard_risk']=None
        return card

    def _client_store(self,client_id: int,*,create: bool=False):
        """Live runtime store, or on-disk robot.sqlite3 when the robot is stopped."""
        cid=int(client_id)
        rt=self.get(cid)
        if rt and rt.started and rt.store is not None:
            return rt.store
        data_dir=self.platform.account_data_dir(cid)
        data_dir.mkdir(parents=True,exist_ok=True)
        db=data_dir/'robot.sqlite3'
        if not db.is_file() and not create:
            raise FileNotFoundError(str(db))
        return Store(db)

    def client_hard_risk(self,client_id: int) -> dict:
        from . import hard_risk
        return hard_risk.snapshot(self._client_store(client_id))

    def save_client_hard_risk(self,client_id: int,*,max_risk_per_trade,daily_loss_limit,
                              equity_drawdown_limit,clear_halt: bool=False) -> dict:
        """Operator sets all three monetary limits — required for LIVE OPEN."""
        from . import hard_risk
        # Allow configuring before first Start — creates robot.sqlite3 if needed.
        store=self._client_store(client_id,create=True)
        saved=hard_risk.save_operator(store,{
            'max_risk_per_trade':max_risk_per_trade,
            'daily_loss_limit':daily_loss_limit,
            'equity_drawdown_limit':equity_drawdown_limit,
            'trading_halted':False if clear_halt else hard_risk.load(store).get('trading_halted'),
            'halt_reason':None if clear_halt else hard_risk.load(store).get('halt_reason'),
        },reason='admin_hard_risk')
        if clear_halt:
            hard_risk.clear_halt(store,operator=True)
        if not hard_risk.configured(saved):
            raise ValueError('All three hard risk limits must be positive numbers')
        store.event('risk','Operator hard risk limits saved',{
            'max_risk_per_trade':saved.get('max_risk_per_trade'),
            'daily_loss_limit':saved.get('daily_loss_limit'),
            'equity_drawdown_limit':saved.get('equity_drawdown_limit'),
            'configured':True})
        return hard_risk.snapshot(store)

    def admin_overview(self) -> dict:
        clients=[self.admin_client_card(c) for c in self.platform.list_clients()]
        running=sum(1 for c in clients if c.get('robot_running'))
        total_closed=sum(int((c.get('results') or {}).get('closed_trades') or 0) for c in clients)
        total_cash=sum(int((c.get('results') or {}).get('cash_trades') or 0) for c in clients)
        total_pending=sum(int((c.get('results') or {}).get('pending_cash_trades') or 0)
                          for c in clients)
        total_proxy=sum(int((c.get('results') or {}).get('proxy_trades') or 0) for c in clients)
        total_estimate=sum(int((c.get('results') or {}).get('estimate_trades') or 0) for c in clients)
        # Never add GBP + USD into one number.
        by_ccy: dict[str,float]={}
        working_by_ccy: dict[str,float]={}
        display_wins=0
        display_losses=0
        for c in clients:
            res=c.get('results') or {}
            for ccy,val in (res.get('pnl_by_currency') or {}).items():
                by_ccy[ccy]=round(by_ccy.get(ccy,0.0)+float(val or 0),4)
            if not res.get('pnl_by_currency') and res.get('pnl_currency') and res.get('realized_pnl') is not None:
                ccy=res['pnl_currency']
                by_ccy[ccy]=round(by_ccy.get(ccy,0.0)+float(res.get('realized_pnl') or 0),4)
            wp=res.get('working_pnl')
            wccy=(res.get('pnl_currency') or '').strip().upper() or None
            if wp is not None and wccy:
                working_by_ccy[wccy]=round(working_by_ccy.get(wccy,0.0)+float(wp),4)
            display_wins+=int(res.get('display_wins') or 0)
            display_losses+=int(res.get('display_losses') or 0)
        primary=None
        if len(by_ccy)==1:
            ccy,val=next(iter(by_ccy.items()))
            primary={'currency':ccy,'realized_pnl':val}
        working_primary=None
        if len(working_by_ccy)==1:
            ccy,val=next(iter(working_by_ccy.items()))
            working_primary={'currency':ccy,'working_pnl':val}
        if total_closed>0 and total_cash==0:
            pnl_status='closed_without_broker_cash'
        elif total_pending>0:
            pnl_status='broker_cash_pending'
        elif total_cash>0:
            pnl_status='broker_cash_ok'
        else:
            pnl_status='no_closes'
        wl_source=('broker' if total_cash>0 else
                   ('upl_estimate' if total_estimate>0 else 'none'))
        return {
            'summary':{
                'clients':len(clients),
                'running_robots':running,
                # Multi-currency: never sum GBP+USD into one number (or fake 0.0).
                'realized_pnl_all':primary['realized_pnl'] if primary else None,
                'working_pnl_all':(working_primary['working_pnl'] if working_primary else None),
                'working_pnl_source':('broker' if total_cash>0 else
                                     ('upl_estimate' if total_estimate>0 else 'none')),
                'pnl_currency':(working_primary['currency'] if working_primary
                                else (primary['currency'] if primary else None)),
                'pnl_by_currency':by_ccy,
                'working_pnl_by_currency':working_by_ccy,
                'closed_trades_all':total_closed,
                'cash_trades_all':total_cash,
                'pending_cash_trades_all':total_pending,
                'proxy_trades_all':total_proxy,
                'estimate_trades_all':total_estimate,
                'display_wins_all':display_wins,
                'display_losses_all':display_losses,
                'wl_source':wl_source,
                'pnl_status':pnl_status,
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
            # Unique init seed per client — shared public market data must not
            # yield identical neural BUY/SELL clones across accounts.
            seed=client_neural_seed(client_id)
            brain=AdaptiveBrain(store,data_dir=data_dir,seed=seed)
            engine=SingleTradeEngine(store,brain,policy_path=data_dir/'policy.py')
            stream=CapitalQuoteStream()
            engine.quote_stream=stream
            creds=self.platform.broker_credentials(broker_row)
            store.event('learning','Per-client neural runtime started',
                        {'client_id':client_id,'neural_seed':seed,
                         'data_dir':str(data_dir)})
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
            rt.last_start_error=None
            self._start_errors.pop(client_id,None)
            self._runtimes[client_id]=rt
            return rt

    async def ensure_client_running(self,client_id: int) -> Optional[AccountRuntime]:
        rt=self.get(client_id)
        if rt and rt.started and rt.engine.broker:
            return rt
        # Dead broker object after errors — drop and reconnect instead of
        # reporting "running" while execution is permanently paused.
        if rt and rt.started and not rt.engine.broker:
            await self.stop_client(client_id)
        broker=self.platform.get_broker_for_client(client_id)
        if not broker or not broker.get('active'):
            return None
        try:
            return await self.start_account(broker)
        except Exception as exc:
            msg=f'{type(exc).__name__}: {str(exc)[:160]}'
            self._start_errors[int(client_id)]=msg
            log.warning('Failed to start client %s: %s',client_id,type(exc).__name__)
            raise

    async def start_all_active(self):
        for row in self.platform.list_active_broker_accounts():
            cid=int(row.get('client_id') or 0)
            try:
                await self.start_account(row)
            except Exception as exc:
                msg=f'{type(exc).__name__}: {str(exc)[:160]}'
                if cid:self._start_errors[cid]=msg
                log.warning('Failed to start client %s: %s',cid or row.get('client_id'),type(exc).__name__)

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
        # Cancel ALL background feeders before closing the journal — liquidity
        # was previously orphaned and could write to a closed store during reset.
        rt.engine.cancel_peer_refresh()
        rt.engine.cancel_calendar_refresh()
        rt.engine.cancel_liquidity_refresh()
        try:rt.brain.save()
        except Exception:pass
        if rt.engine.broker:
            try:await rt.engine.broker.close()
            except Exception:pass
            rt.engine.broker=None
        # Close journal before admin learning-reset moves robot.sqlite3 aside.
        try:rt.store.close()
        except Exception:pass

    async def shutdown(self):
        async with self._lock:
            ids=list(self._runtimes.keys())
            for client_id in ids:
                await self._stop_unlocked(client_id)

    @staticmethod
    def _can_rearm_after_read(engine) -> bool:
        """Read-only recovery must not clear mismatch / multi / operator pauses."""
        return bool(
            engine._paused_for_read and engine.mandate and not engine.operator_paused
            and not engine._paused_for_mismatch and not engine._paused_for_multi
            and not engine.store.unresolved() and len(engine.positions)<=1)

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
                    if self._can_rearm_after_read(engine):
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
                try:
                    store.event('error','Broker/engine cycle error',{
                        'type':type(exc).__name__,'count':error_count,
                        'source':Path(local.filename).name if local else None,
                        'line':local.lineno if local else None,
                        'client_id':rt.client_id})
                except Exception:
                    pass
                if error_count>=3:
                    # Log only — never auto-pause (operator Pause is the only freeze).
                    store.event('warning',
                        'Repeated broker/engine errors; keeping execution live',
                        {'count':error_count,'client_id':rt.client_id})
                    if hasattr(engine,'_ensure_live_unless_operator_paused'):
                        engine._ensure_live_unless_operator_paused()
                await asyncio.sleep(3 if error_count<3 else 10)
