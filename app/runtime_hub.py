"""Per-client robot runtimes — isolated store/brain/engine/worker/stream."""
from __future__ import annotations
import asyncio
import logging
import math
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


def _finite_num(v):
    try:
        n=float(v)
    except (TypeError,ValueError):
        return None
    return n if math.isfinite(n) else None


def _remap_swing_marks(marks,full_n: int,display_n: int,limit: int=6,
                       path_n: int|None=None):
    """Map swing pivot indices onto the displayed candle slice.

    ``structure_memory.normalise`` uses a trailing window (often 80). Pivot ``i``
    is relative to that path — not ``len(candles_full)``. Pass ``path_n`` so
    remap uses ``full_n - path_n`` as the path start inside full history, then
    shift into the display window.
    """
    out=[]
    try:
        full_n=int(full_n);display_n=int(display_n)
    except (TypeError,ValueError):
        return out
    try:
        pn=int(path_n) if path_n is not None else None
    except (TypeError,ValueError):
        pn=None
    if pn is None or pn<=0:
        pn=min(80,full_n) if full_n>0 else 0
    # Path covers the last pn bars of full history.
    path_start=max(0,full_n-pn)
    display_start=max(0,full_n-display_n)
    for m in (marks or [])[-limit:]:
        if not isinstance(m,dict):
            continue
        i=m.get('i')
        try:
            i=int(i) if i is not None else None
        except (TypeError,ValueError):
            i=None
        if i is not None:
            abs_i=path_start+i
            disp_i=abs_i-display_start
            if disp_i<0 or disp_i>=display_n:
                continue
            i=disp_i
        out.append({'price':m.get('price'),'i':i})
    return out


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
        def _live_lvl(v):
            try:
                n=float(v)
            except (TypeError,ValueError):
                return None
            return n if math.isfinite(n) and n>0 else None
        has_sl=_live_lvl(p.get('stopLevel')) is not None
        has_tp=_live_lvl(p.get('profitLevel')) is not None
        enrolled=bool(isinstance(meta,dict) and meta.get('owner')=='AUTO')
        # Prefer hard kill switch; never advertise trail from stale class defaults.
        trail_on=False
        if engine is not None and hasattr(engine,'_trail_is_enabled'):
            try:
                trail_on=bool(engine._trail_is_enabled())
            except Exception:
                trail_on=False
        else:
            trail_on=bool(getattr(engine,'TRAIL_ENABLED',False))
        phase='flat'
        # Trail disabled → never advertise armed/ratchet/trail phases (Admin honesty).
        if not trail_on:
            if not deal:
                phase='unknown'
            elif not has_sl or not has_tp:
                phase='watch'  # brackets incomplete — protect should attach
            elif not robot:
                phase='manual'
            else:
                phase='flat'
        elif not deal:
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
            elif 'no broker-native trail' in status or 'no trail improvement' in status:
                # CONFIRMED once, but brief says idle — do not sticky-label trail.
                phase='armed'
            elif 'deferred' in status or 'backoff' in status:
                phase='watch'
            elif 'confirmed trail' in status:
                phase='trail'
            elif runtime.get('status')=='CONFIRMED' and (
                    'trail' in status or 'ratchet' in status or 'be' in status):
                phase='trail'
            elif runtime.get('status')=='CONFIRMED':
                phase='armed'
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
        market=getattr(engine,'market',None) if engine is not None else None
        if not isinstance(market,dict):
            market={}
        m_mid=market.get('mid')
        m_bid=market.get('bid')
        m_ask=market.get('ask') if market.get('ask') is not None else market.get('offer')
        out=[]
        for p in positions or []:
            if not isinstance(p,dict):
                continue
            truth=self._deal_protection_truth(engine,p,brief=brief)
            bid=p.get('bid') if p.get('bid') is not None else m_bid
            ask=(p.get('ask') if p.get('ask') is not None
                 else (p.get('offer') if p.get('offer') is not None else m_ask))
            mid=p.get('mid')
            if mid is None:
                mid=m_mid
            if mid is None and bid is not None and ask is not None:
                try:mid=(float(bid)+float(ask))/2.0
                except (TypeError,ValueError):mid=None
            current=(p.get('currentLevel') if p.get('currentLevel') is not None
                     else (p.get('current_price') if p.get('current_price') is not None
                           else (p.get('mark') if p.get('mark') is not None else mid)))
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
                'bid':bid,
                'ask':ask,
                'mid':mid,
                'currentLevel':current,
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
        mtf=snap.get('mtf_momentum') if isinstance(snap.get('mtf_momentum'),dict) else None
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
            'forward_live':(snap.get('forward_live')
                            if isinstance(snap.get('forward_live'),dict) else None),
            'mtf_momentum':mtf,
            'scores':{
                'BUY':float(scores.get('BUY') or 0),
                'HOLD':float(scores.get('HOLD') or 0),
                'SELL':float(scores.get('SELL') or 0),
            },
            'action':signal.get('action') or (manage or {}).get('action'),
            # Neural side when action is HOLD (blocked / waiting) — Admin shows HOLD → BUY.
            'proposal':(signal.get('proposal')
                        or (manage or {}).get('proposal')
                        or signal.get('action')
                        or (manage or {}).get('action')),
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
            'entry_path':snap.get('entry_path'),
            'entry_intent':snap.get('entry_intent'),
            'entry_trigger':snap.get('entry_trigger'),
            'entry_wait':snap.get('entry_wait'),
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
        # Last live UPL is provisional — never count as Capital.com cash.
        # Admin must label these ESTIMATE until history refines to broker.
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
        by_ccy_24h: dict[str,list[float]]={}
        signed=[]
        signed_24h=[]
        estimate_signed=[]
        estimate_signed_24h=[]
        recent=[]
        cash_n=0
        cash_n_24h=0
        pending_n=0
        proxy_n=0
        estimate_n=0
        estimate_sum=0.0
        estimate_sum_24h=0.0
        blank_n=0
        from datetime import datetime, timezone, timedelta
        cutoff_24h=datetime.now(timezone.utc)-timedelta(hours=24)

        def _in_24h(closed_raw):
            if not closed_raw:
                return True  # unknown stamp — keep in window rather than drop
            try:
                raw=str(closed_raw).replace('Z','+00:00').replace(' ','T',1)
                dt=datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt=dt.replace(tzinfo=timezone.utc)
                return dt>=cutoff_24h
            except Exception:
                return True

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
                # Capital often journals broker £0 before Funds settles — that
                # zero-poison must NOT paint Desk working_pnl=0 as "truth".
                if abs(float(val))<1e-12:
                    blank_n+=1
                    pending_n+=1
                    row={**row,'pending_cash':True,'pnl_source':'pending_broker_pnl'}
                    if len(recent)<12:
                        recent.append(row)
                else:
                    cash_n+=1
                    by_ccy.setdefault(ccy,[]).append(val)
                    if not acct or ccy==acct:
                        signed.append(val)
                    if _in_24h(t.get('closed')):
                        cash_n_24h+=1
                        by_ccy_24h.setdefault(ccy,[]).append(val)
                        if not acct or ccy==acct:
                            signed_24h.append(val)
                    if len(recent)<12:
                        recent.append(row)
            elif kind=='pending':
                # Last live UPL as display hint when journal has a number or
                # last_upl still holds the close figure — show as ESTIMATE,
                # never as Capital cash (that lied next to the Capital app).
                snap=store.get('last_upl:'+str(t.get('deal_id') or '')) or {}
                try:
                    hint=float(snap.get('upl')) if snap.get('upl') is not None else None
                except (TypeError,ValueError):
                    hint=None
                if hint is not None and not __import__('math').isfinite(hint):
                    hint=None
                show=val if val is not None else hint
                if show is not None:
                    # Operator: closed at 0.01 must show 0.01, not blank gaida.
                    estimate_n+=1
                    estimate_sum+=float(show)
                    estimate_signed.append(float(show))
                    if _in_24h(t.get('closed')):
                        estimate_sum_24h+=float(show)
                        estimate_signed_24h.append(float(show))
                    row={**row,'pnl':round(float(show),4),'pnl_source':'upl_estimate',
                         'pending_cash':False}
                    if hint is not None:
                        row['upl_hint']=round(hint,4)
                    if len(recent)<12:
                        recent.append(row)
                else:
                    pending_n+=1
                    blank_n+=1
                    row['pending_cash']=True
                    if len(recent)<12:
                        recent.append(row)
            elif kind=='estimate' and val is not None:
                estimate_n+=1
                estimate_sum+=float(val)
                estimate_signed.append(float(val))
                if _in_24h(t.get('closed')):
                    estimate_sum_24h+=float(val)
                    estimate_signed_24h.append(float(val))
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
        # Also never invent 0.00 when there are simply no closed trades — that
        # blanked Admin "Kopējais P&L" while live open UPL was available.
        closed_n=len(closed_rows)
        if primary:
            realized=primary['realized_pnl']
        elif by_ccy:
            realized=None
        elif closed_n==0 or cash_n==0:
            # No closes, or closes without broker cash — do not fake 0.00.
            realized=None
        else:
            realized=0.0
        # Honest status: UPL-only closes are NOT Capital cash yet.
        if closed_n>0 and cash_n==0 and estimate_n>0:
            pnl_status='upl_estimate_pending'
        elif closed_n>0 and cash_n==0:
            pnl_status='closed_without_broker_cash'
        elif pending_n>0 or blank_n>0:
            pnl_status='broker_cash_pending'
        elif cash_n>0 and estimate_n>0:
            pnl_status='broker_cash_partial'
        elif cash_n>0:
            pnl_status='broker_cash_ok'
        else:
            pnl_status='no_closes'
        # 24h Capital Funds window — matches phone Closed Positions better than
        # all-time SQLite sum (that made Desk −13 look unlike Funds today).
        realized_24h=None
        if signed_24h:
            realized_24h=round(sum(signed_24h),4)
        elif by_ccy_24h:
            if acct and acct in by_ccy_24h:
                realized_24h=round(sum(by_ccy_24h[acct]),4)
            elif len(by_ccy_24h)==1:
                realized_24h=round(sum(next(iter(by_ccy_24h.values()))),4)
        wins_24h=sum(1 for p in signed_24h if p>0)
        losses_24h=sum(1 for p in signed_24h if p<0)
        # Operator display: prefer 24h Capital £ (Funds window). ≈UPL stays in
        # estimate_* — never invent Kopējais from provisional closes.
        if realized_24h is not None and cash_n_24h>0:
            working_pnl=realized_24h
            working_pnl_source='broker'
            working_pnl_window='24h'
        elif realized is not None and cash_n>0:
            working_pnl=realized
            working_pnl_source='broker'
            working_pnl_window='all'
        elif estimate_signed_24h:
            working_pnl=None
            working_pnl_source='upl_estimate'
            working_pnl_window='24h'
        elif estimate_n>0:
            working_pnl=None
            working_pnl_source='upl_estimate'
            working_pnl_window='all'
        else:
            working_pnl=None
            working_pnl_source=('none' if closed_n else 'empty')
            working_pnl_window=None
        if cash_n_24h>0:
            display_wins,display_losses,wl_source=wins_24h,losses_24h,'broker'
        elif cash_n>0:
            display_wins,display_losses,wl_source=wins,losses,'broker'
        else:
            # Do not advertise estimate W/L as robot results.
            display_wins,display_losses,wl_source=0,0,(
                'upl_estimate' if estimate_n>0 else 'none')
        upl=None
        if open_upl is not None:
            try:
                u=float(open_upl)
                if __import__('math').isfinite(u):
                    upl=round(u,4)
            except (TypeError,ValueError):
                upl=None
        # Equity = broker closed £ + live open UPL only (never provisional closes).
        base_for_equity=working_pnl if working_pnl is not None else realized
        if base_for_equity is not None and upl is not None:
            equity=round(float(base_for_equity)+float(upl),4)
        elif base_for_equity is not None:
            equity=base_for_equity
        elif upl is not None:
            equity=upl
        else:
            equity=None
        pending_q=0
        try:
            pending_q=len(store.get('pending_broker_pnl_deals') or [])
        except Exception:
            pending_q=0
        # Desk fallback: knobs moved from £ but trades journal missed OPEN upsert
        # → still show edge-hist closes so CLIENT CLOSES is not a blank island.
        if not recent:
            try:
                from . import unified_cycle
                hist=list(store.get(unified_cycle.EDGE_OUTCOME_HIST_KEY) or [])
            except Exception:
                hist=[]
            if isinstance(hist,list):
                for h in reversed(hist):
                    if not isinstance(h,dict):
                        continue
                    try:
                        pnl_h=float(h.get('pnl'))
                    except (TypeError,ValueError):
                        continue
                    if not __import__('math').isfinite(pnl_h):
                        continue
                    recent.append({
                        'deal_id':h.get('deal_id') or f"edge-{len(recent)}",
                        'epic':h.get('epic') or 'GOLD',
                        'direction':h.get('direction') or h.get('side') or '?',
                        'closed':h.get('at') or h.get('closed') or h.get('time'),
                        'pnl':round(pnl_h,4),
                        # Edge hist is NOT Capital Funds — never label as broker.
                        'pnl_currency':acct or h.get('currency') or '?',
                        'pnl_source':'edge_hist',
                        'from_edge_hist':True,
                    })
                    if len(recent)>=12:
                        break
        return {
            'closed_trades':closed_n,
            'open_trades_recorded':open_n,
            'cash_trades':cash_n,
            'pending_cash_trades':pending_n,
            'pending_broker_pnl':pending_q,
            'pending_trades':pending_n,
            'proxy_trades':proxy_n,
            'estimate_trades':estimate_n,
            'estimate_pnl':round(estimate_sum,4) if estimate_n else None,
            'estimate_wins':est_wins,
            'estimate_losses':est_losses,
            'blank_pnl_trades':blank_n,
            'pnl_status':pnl_status,
            'realized_pnl':realized,
            'realized_pnl_24h':realized_24h,
            'cash_trades_24h':cash_n_24h,
            'estimate_pnl_24h':(
                round(estimate_sum_24h,4) if estimate_signed_24h else None),
            'working_pnl':working_pnl,
            'working_pnl_source':working_pnl_source,
            'working_pnl_window':working_pnl_window,
            'open_upl':upl,
            'equity_pnl':equity,
            'pnl_currency':primary['currency'] if primary else acct,
            'pnl_by_currency':{k:round(sum(v),4) for k,v in by_ccy.items()},
            'wins':wins,
            'losses':losses,
            'wins_24h':wins_24h,
            'losses_24h':losses_24h,
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

    def _desk_system_payload(self, rt, snap: dict | None = None) -> dict:
        """At-a-glance A–F board for Desk — cards, shadow, params, samples, flow."""
        snap = snap if isinstance(snap, dict) else {}
        store = getattr(rt, 'store', None) if rt is not None else None
        sd = snap.get('self_development') if isinstance(snap.get('self_development'), dict) else {}
        # Keep SHADOW "Live today" news/peers in sync even mid-boot.
        if store is not None:
            try:
                news = snap.get('economic_news_risk')
                im = snap.get('intermarket_analysis')
                if isinstance(news, dict) or isinstance(im, dict):
                    store.set('live_external_context', {
                        'at': __import__('datetime').datetime.now(
                            __import__('datetime').timezone.utc).isoformat(),
                        'news': news if isinstance(news, dict) else {},
                        'intermarket': im if isinstance(im, dict) else {},
                    })
            except Exception:
                pass
        out = {
            'ok': True,
            'can_open': bool(snap.get('entry_can_submit_now')),
            'entry_block': snap.get('entry_block_reason'),
            'entry_intent': snap.get('entry_intent') or snap.get('policy_proposal'),
            'neural_close_executable': bool(snap.get('neural_close_executable', True)),
            'neural_close_diagnostic_only': bool(
                snap.get('neural_close_diagnostic_only', False)),
            'neural_exit_intent': snap.get('neural_exit_intent'),
            'cards': {'active': [], 'n': 0},
            'shadow': {},
            'self_params': {},
            'samples': {},
            'flow_learn': {},
            'adaptive': {},
        }
        try:
            from . import strategy_gen
            bag = strategy_gen.load(store) if store is not None else {'active_ids': []}
            ids = list(bag.get('active_ids') or [])
            out['cards'] = {
                'active': ids[:12],
                'n': len(ids),
                'enabled': True,
            }
        except Exception as exc:
            out['cards'] = {'active': [], 'n': 0, 'error': type(exc).__name__}
        try:
            sp_snap = sd.get('shadow_paper') if isinstance(sd.get('shadow_paper'), dict) else None
            sp_live = None
            if store is not None:
                eng = getattr(rt, 'engine', None) if rt is not None else None
                # Desk poll = boot path: re-audit Capital 24h book BEFORE paper
                # restore, so SHADOW is not stuck at 0/0 while CLIENT CLOSES has £.
                if eng is not None and hasattr(eng, '_backfill_mistake_audits_from_journal'):
                    try:
                        eng._backfill_mistake_audits_from_journal(limit=400, hours=24)
                    except Exception:
                        pass
                # Prefer awaited mirror via refresh_live_truth(); keep a
                # best-effort schedule here if desk path ran without await.
                if eng is not None and hasattr(eng, '_mirror_capital_funds_closes'):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(eng._mirror_capital_funds_closes())
                    except RuntimeError:
                        pass
                    except Exception:
                        pass
                # Shadow neural must not stay "off / booting" on Desk while LIVE runs.
                if eng is not None and hasattr(eng, '_ensure_shadow_brain'):
                    try:
                        eng._ensure_shadow_brain()
                    except Exception:
                        pass
                if eng is not None and getattr(eng, 'shadow_paper', None) is not None:
                    try:
                        # Empty journal + surviving *shadow* audit hist → restore.
                        # Never seed Capital LIVE £ into independent Paper Book.
                        eng.shadow_paper.purge_capital_journal_rows()
                        eng.shadow_paper.backfill_from_mistake_audits()
                    except Exception:
                        pass
                    try:
                        # Re-scale price-only paper closes from LIVE broker £.
                        live = getattr(
                            getattr(eng, 'profit_learning', None), 'history', None) or []
                        eng.shadow_paper.update_cash_scale(live)
                    except Exception:
                        pass
                    sp_live = eng.shadow_paper.summary()
                else:
                    from .shadow_paper import ShadowPaperJournal
                    jp = ShadowPaperJournal(store)
                    try:
                        jp.purge_capital_journal_rows()
                        jp.backfill_from_mistake_audits()
                    except Exception:
                        pass
                    try:
                        jp.update_cash_scale([])
                    except Exception:
                        pass
                    sp_live = jp.summary()
            def _shadow_richer(a, b):
                if not isinstance(a, dict):
                    return b if isinstance(b, dict) else {}
                if not isinstance(b, dict):
                    return a
                # Prefer the journal that actually has closes — snap can be a stale 0.
                # Price-only paper closes (no £ scale yet) still beat a blank snap.
                def score(d):
                    cash = int(d.get('cash_trades') or 0)
                    price = int(d.get('price_trades') or 0)
                    trades = int(d.get('display_trades') or d.get('trades') or 0)
                    return cash * 100 + price * 10 + trades
                return b if score(b) > score(a) else a
            out['shadow'] = _shadow_richer(sp_snap, sp_live)
        except Exception as exc:
            out['shadow'] = {'error': type(exc).__name__}
        try:
            from . import self_params as sp_mod
            if isinstance(sd.get('self_params'), dict):
                out['self_params'] = sd['self_params']
            elif store is not None:
                out['self_params'] = sp_mod.snapshot(store)
        except Exception as exc:
            out['self_params'] = {'error': type(exc).__name__}
        try:
            if store is not None:
                from . import sample_strategies
                flag = store.get(sample_strategies.STORE_FLAG) or {}
                if not isinstance(flag, dict):
                    flag = {}
                lib_n = int(
                    flag.get('library_samples')
                    or sample_strategies.library_sample_count(store) or 0)
                cards_n = int((out.get('cards') or {}).get('n') or 0)
                # Stale ingest flag often keeps active_n=0 after cards regenerate.
                active_n = int(flag.get('active_n') or 0) or cards_n
                closed_n = 0
                try:
                    closed_n = int(store.db.execute(
                        'SELECT COUNT(*) FROM trades WHERE closed IS NOT NULL'
                    ).fetchone()[0] or 0)
                except Exception:
                    closed_n = 0
                # Headline count for Desk — never flash a bare 0 when cards/closes exist.
                display_n = lib_n or active_n or closed_n
                out['samples'] = {
                    'done': bool(flag.get('done')),
                    'library_samples': lib_n,
                    'trades': int(flag.get('trades') or 0),
                    'closed_trades': closed_n,
                    'source': flag.get('source') or (
                        'strategy_cards' if cards_n else (
                            'closed_trades' if closed_n else None)),
                    'active_n': active_n,
                    'display_n': display_n,
                }
        except Exception as exc:
            out['samples'] = {'error': type(exc).__name__}
        try:
            fc = snap.get('flow_consensus') if isinstance(snap.get('flow_consensus'), dict) else {}
            learn = fc.get('learn') if isinstance(fc.get('learn'), dict) else {}
            if not learn and store is not None:
                from . import flow_consensus
                learn = flow_consensus.learn_snapshot(store)
            out['flow_learn'] = learn if isinstance(learn, dict) else {}
            out['flow_consensus'] = {
                'consensus': fc.get('consensus'),
                'confidence': fc.get('confidence'),
                'blocked': bool(fc.get('blocked')),
                'decision_effect': fc.get('decision_effect'),
                'decision_reason': fc.get('decision_reason') or fc.get('reason'),
            }
        except Exception as exc:
            out['flow_learn'] = {'error': type(exc).__name__}
        try:
            from . import adaptive_system
            if store is not None:
                out['adaptive'] = adaptive_system.snapshot(store)
        except Exception as exc:
            out['adaptive'] = {'error': type(exc).__name__}
        try:
            if store is not None:
                from . import unified_cycle
                out['learn_horizon'] = unified_cycle.learn_horizon(store)
        except Exception as exc:
            out['learn_horizon'] = {'error': type(exc).__name__}
        # Highlight knobs operators care about on the board.
        try:
            p = out.get('self_params') or {}
            knobs = p.get('params') if isinstance(p.get('params'), dict) else p
            if isinstance(knobs, dict):
                out['knobs'] = {
                    'flow_nudge': knobs.get('flow_consensus_nudge'),
                    'flow_block': knobs.get('flow_consensus_block_fights'),
                    'flow_impulse': knobs.get('flow_pillar_impulse'),
                    'strategy_gen_nudge': knobs.get('strategy_gen_nudge'),
                    'horizon': knobs.get('self_params_horizon_trades'),
                }
        except Exception:
            out['knobs'] = {}
        # Full SHADOW lab — what the operator can watch live (not a fake 100-sweep).
        try:
            out['shadow_lab'] = self._shadow_lab_payload(rt, store, sd, out)
        except Exception as exc:
            out['shadow_lab'] = {'ok': False, 'error': type(exc).__name__}
        return out

    def _shadow_lab_payload(self, rt, store, sd, desk_out: dict) -> dict:
        """Compact live picture of every shadow / self-dev loop."""
        sd = sd if isinstance(sd, dict) else {}
        paper = desk_out.get('shadow') if isinstance(desk_out.get('shadow'), dict) else {}
        recent = []
        open_pos = paper.get('position') if isinstance(paper.get('position'), dict) else None
        pending = paper.get('pending') if isinstance(paper.get('pending'), dict) else None
        if store is not None:
            try:
                from .shadow_paper import STORE_KEY as PAPER_KEY
                hist = list(store.get(PAPER_KEY) or [])
                for row in reversed(hist):
                    if not isinstance(row, dict):
                        continue
                    recent.append({
                        'at': row.get('at') or row.get('bar_time'),
                        'direction': row.get('direction'),
                        'entry': row.get('entry'),
                        'exit': row.get('exit'),
                        'paper_pnl': row.get('paper_pnl'),
                        'price_pnl': row.get('price_pnl'),
                        'unit': row.get('unit'),
                        'exit_reason': row.get('exit_reason'),
                    })
                    if len(recent) >= 12:
                        break
            except Exception:
                recent = []
            # Paper book empty after bag wipe — still show Capital/audit closes
            # so SHADOW is not a blank "No paper closes" while CLIENT CLOSES has £.
            if not recent:
                try:
                    from . import shadow_mistake_audit as sma
                    lab = sma.lab_payload(store) if hasattr(sma, 'lab_payload') else {}
                    seed = list((lab or {}).get('recent_audits') or [])
                    for row in seed:
                        if not isinstance(row, dict):
                            continue
                        pnl = row.get('pnl')
                        try:
                            pnl_n = float(pnl) if pnl is not None else None
                        except (TypeError, ValueError):
                            pnl_n = None
                        recent.append({
                            'at': row.get('at') or row.get('closed'),
                            'direction': row.get('direction'),
                            'entry': row.get('entry'),
                            'exit': row.get('exit'),
                            'paper_pnl': pnl_n,
                            'price_pnl': None,
                            'unit': 'account_currency',
                            'exit_reason': (
                                'live_journal' if str(row.get('source') or '').startswith('live')
                                else (row.get('source') or 'audit')),
                        })
                        if len(recent) >= 12:
                            break
                except Exception:
                    pass
            if open_pos is None or pending is None:
                try:
                    from .shadow_paper import ShadowPaperJournal
                    live = ShadowPaperJournal(store)
                    if open_pos is None and isinstance(live.position, dict):
                        open_pos = live.position
                    if pending is None and isinstance(live.pending, dict):
                        pending = live.pending
                except Exception:
                    pass
        hold = sd.get('hold_shadow') if isinstance(sd.get('hold_shadow'), dict) else {}
        struct = sd.get('structure_shadow') if isinstance(sd.get('structure_shadow'), dict) else {}
        if store is not None:
            if not hold:
                try:
                    from . import hold_shadow
                    hold = hold_shadow.strategy_evidence(store)
                except Exception:
                    hold = {}
            if not struct:
                try:
                    from . import structure_shadow
                    struct = structure_shadow.summary(store)
                except Exception:
                    struct = {}
        trial = {}
        if store is not None:
            try:
                from . import self_params as sp_mod
                pending_sp = sp_mod._pending(store)
                trial = {
                    'active': bool(pending_sp.get('trial_active')),
                    'trial_id': pending_sp.get('trial_id'),
                    'started_at_trades': int(pending_sp.get('trial_started_at_trades') or 0),
                    'draft': pending_sp.get('draft'),
                    'pending_events': len(pending_sp.get('events') or []),
                    'horizon': (desk_out.get('knobs') or {}).get('horizon'),
                }
            except Exception:
                trial = {}
        last_dev = {}
        evo = {}
        if store is not None:
            try:
                raw = store.get('shadow_self_dev_last') or {}
                if isinstance(raw, dict):
                    dec = raw.get('decision') if isinstance(raw.get('decision'), dict) else {}
                    last_dev = {
                        'promote': bool(dec.get('promote') or dec.get('weights_promoted')),
                        'rollback': bool(dec.get('rollback')),
                        'reason': (dec.get('reason')
                                   or store.get('evolution_last_reason')),
                        'shadow_neurons': raw.get('shadow_neurons'),
                        'shadow_dim': raw.get('shadow_dim'),
                        'live_samples': raw.get('live_samples'),
                        'paper_cash_samples': raw.get('paper_cash_samples'),
                        'method': raw.get('method'),
                    }
            except Exception:
                last_dev = {}
            try:
                evo = {
                    'attempts': int(store.get('evolution_attempts') or 0),
                    'last_reason': store.get('evolution_last_reason'),
                    # Honesty: policy search is ~3 kinds × ~10 margins ≈ 30, not 100.
                    'policy_candidates_approx': 30,
                    'mode': 'paper_book + ~30 policy variants + param trials',
                }
            except Exception:
                evo = {}
        shadow_on = bool(sd.get('shadow_active'))
        if rt is not None:
            eng = getattr(rt, 'engine', None)
            if eng is not None and hasattr(eng, '_ensure_shadow_brain'):
                try:
                    eng._ensure_shadow_brain()
                except Exception:
                    pass
            if not shadow_on:
                shadow_on = bool(
                    eng is not None and getattr(eng, 'shadow_brain', None) is not None)
        running = False
        if rt is not None:
            try:
                running = bool(getattr(rt, 'running', False) or (
                    getattr(rt, 'engine', None) is not None))
            except Exception:
                running = False
        mistake_audit = {}
        if store is not None:
            try:
                from . import shadow_mistake_audit as sma
                # Ensure cycle has today's Capital book even mid-boot.
                eng_ma = getattr(rt, 'engine', None) if rt is not None else None
                if eng_ma is not None and hasattr(
                        eng_ma, '_backfill_mistake_audits_from_journal'):
                    try:
                        eng_ma._backfill_mistake_audits_from_journal(
                            limit=400, hours=24)
                    except Exception:
                        pass
                mistake_audit = sma.lab_payload(store)
            except Exception:
                mistake_audit = {'ok': False}
        cash_n = int(paper.get('cash_trades') or 0)
        trades_n = int(paper.get('trades') or 0)
        price_n = int(paper.get('price_trades') or paper.get('display_trades') or 0)
        win24_n = int(((mistake_audit.get('window_24h') or {}).get('n')) or 0)
        pulse = 'IDLE'
        if open_pos or pending:
            pulse = 'OPEN'
        elif (mistake_audit.get('last') or {}).get('mistakes') or win24_n:
            pulse = 'LEARNING'
        elif cash_n or trades_n or price_n:
            pulse = 'LEARNING'
        elif shadow_on and running:
            pulse = 'WATCHING'
        elif running:
            pulse = 'BOOTING'
        return {
            'ok': True,
            'pulse': pulse,
            'shadow_active': shadow_on,
            'robot_attached': running,
            # Explicit honesty for the operator.
            'truth': {
                'hundred_variations': False,
                'what_runs': (
                    'One shadow neural + paper SL/TP book; mistake-audit '
                    '(OHLC/zones/news/intermarket) → scenario memory → targeted '
                    'fix → promote after repeats → LIVE; ~30 policy edge '
                    'candidates; self-params trials; HOLD opposite. '
                    'Not a 100-parallel sweep.'),
                'policy_candidates_approx': 30,
                'mistake_audit': True,
            },
            'paper': {
                **paper,
                'open': open_pos,
                'pending_fill': pending,
                'recent': recent,
            },
            'hold_shadow': hold if isinstance(hold, dict) else {},
            'structure_shadow': {
                'buckets': struct.get('buckets'),
                'snapshots': struct.get('snapshots'),
                'last_advice': struct.get('last_advice'),
                'top': (struct.get('top') or [])[:5],
            },
            'trial': trial,
            'self_dev_last': last_dev,
            'evolution': evo,
            'mistake_audit': mistake_audit if isinstance(mistake_audit, dict) else {},
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
            'proposal':None,
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
                'closed_trades':0,'open_trades_recorded':0,'realized_pnl':None,
                'working_pnl':None,'working_pnl_source':'empty',
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
            # Fresh news/peers on every Desk paint — kill stale UNMAPPED/WAITING chips.
            desk_news=snap.get('economic_news_risk')
            desk_im=snap.get('intermarket_analysis')
            try:
                if hasattr(rt.engine,'_news_risk'):
                    fresh_news=rt.engine._news_risk()
                    if isinstance(fresh_news,dict):
                        desk_news=fresh_news
            except Exception:
                pass
            try:
                im_obj=getattr(rt.engine,'intermarket',None)
                st_im=getattr(im_obj,'state',None) if im_obj is not None else None
                if isinstance(st_im,dict) and st_im:
                    desk_im=dict(st_im)
            except Exception:
                pass
            card.update({
                'robot_running':True,
                'phase':view.get('phase'),
                'connected':bool(view.get('connected')),
                'operator_paused':bool(view.get('operator_paused')),
                'execution_enabled':bool(view.get('execution_enabled')),
                'armed':bool(view.get('armed')),
                'scores':view.get('scores') or card['scores'],
                'action':view.get('action'),
                'proposal':view.get('proposal'),
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
                    'market_mid':(snap.get('market') or {}).get('mid'),
                    'market_bid':(snap.get('market') or {}).get('bid'),
                    'market_ask':((snap.get('market') or {}).get('ask')
                                 if (snap.get('market') or {}).get('ask') is not None
                                 else (snap.get('market') or {}).get('offer')),
                    'mandate':view.get('mandate'),
                    'position':view.get('position'),
                    'positions':view.get('positions') or (
                        [view['position']] if view.get('position') else []),
                    'protection':view.get('protection') or snap.get('adaptive_protection'),
                    'day_book':view.get('day_book') or snap.get('day_book'),
                    'forward_live':view.get('forward_live') or snap.get('forward_live'),
                    'mtf_momentum':view.get('mtf_momentum') or snap.get('mtf_momentum'),
                    'entry_block_reason':view.get('entry_block_reason') or snap.get('entry_block_reason'),
                    'entry_can_submit_now':bool(view.get('entry_can_submit_now')
                                               or snap.get('entry_can_submit_now')),
                    'entry_path':view.get('entry_path') or snap.get('entry_path'),
                    'entry_intent':view.get('entry_intent') or snap.get('entry_intent'),
                    'entry_trigger':view.get('entry_trigger') or snap.get('entry_trigger'),
                    'entry_wait':view.get('entry_wait') or snap.get('entry_wait'),
                    'model_readiness':snap.get('model_readiness') or view.get('model_readiness'),
                    # Desk truth — never hardcode diagnostic-only (engine snapshot is source).
                    'neural_close_diagnostic_only':bool(
                        snap.get('neural_close_diagnostic_only', False)),
                    'neural_close_executable':bool(
                        snap.get('neural_close_executable', True)),
                    'neural_exit_intent':snap.get('neural_exit_intent'),
                    'neural_exit_rule':snap.get('neural_exit_rule'),
                    'neural_seed':view.get('neural_seed'),
                    'error':view.get('error') or snap.get('error'),
                    'last_start_error':None,
                    'balance':((snap.get('account') or {}).get('balance') or {}),
                    'currency':account_currency,
                    # Desk intel — prefer live remap over a stale UNMAPPED/WAITING snap.
                    'economic_news_risk':desk_news,
                    'intermarket_analysis':desk_im,
                    'trail_enabled':bool(
                        rt.engine._trail_is_enabled()
                        if hasattr(rt.engine,'_trail_is_enabled') else False),
                    # Full robot telemetry for the Desk control panel.
                    'returns_bps':((snap.get('market_context') or {}).get('returns_bps')
                                   if isinstance(snap.get('market_context'),dict) else None),
                    'bar_time':((snap.get('market_context') or {}).get('bar_time')
                                if isinstance(snap.get('market_context'),dict) else None),
                    'market_regime':snap.get('market_regime'),
                    'fusion_block_reason':snap.get('fusion_block_reason'),
                    'directional_advantage':snap.get('directional_advantage'),
                    'economic_utility':snap.get('economic_utility'),
                    'neural_decision_bar':snap.get('neural_decision_bar'),
                    'manage_signal':snap.get('manage_signal'),
                    'situation_memory':snap.get('situation_memory'),
                    'flow_consensus':snap.get('flow_consensus'),
                    'entry_eligible':snap.get('entry_eligible'),
                    'policy_decision':snap.get('policy_decision'),
                    'policy_proposal':snap.get('policy_proposal'),
                    'unresolved_orders':snap.get('unresolved_orders'),
                    'position_age':snap.get('position_age'),
                    'instrument_profile':snap.get('instrument_profile'),
                    'minute_direction_accuracy':snap.get('minute_direction_accuracy'),
                    'trade_win_rate':snap.get('trade_win_rate'),
                    'trade_wins':snap.get('trade_wins'),
                    'trade_losses':snap.get('trade_losses'),
                    'labeled_samples':snap.get('labeled_samples'),
                    'entry_decision_pending_new_bar':snap.get(
                        'entry_decision_pending_new_bar'),
                    'neural_market_exit':snap.get('neural_market_exit'),
                    'spread':(snap.get('market') or {}).get('spread'),
                    'tick':(snap.get('market') or {}).get('tick'),
                    'min_distance':(snap.get('market') or {}).get('min_distance'),
                    'learn_horizon':snap.get('learn_horizon'),
                    'last_open_size_plan':snap.get('last_open_size_plan'),
                    'unified_cycle':snap.get('unified_cycle'),
                    'hard_risk':snap.get('hard_risk'),
                    # A–F desk board: cards / shadow / params / samples.
                    'self_development':snap.get('self_development'),
                    'desk_system':self._desk_system_payload(rt, snap),
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
                pos_list=card.get('activity',{}).get('positions') or []
                if not pos_list:
                    one=card.get('activity',{}).get('position')
                    pos_list=[one] if isinstance(one,dict) else []
                vals=[]
                for pos in pos_list:
                    if not isinstance(pos,dict) or pos.get('upl') is None:
                        continue
                    try:
                        u=float(pos['upl'])
                    except (TypeError,ValueError):
                        continue
                    if __import__('math').isfinite(u):
                        vals.append(u)
                if vals:
                    upl=sum(vals)
                card['results']=self._trade_stats(
                    store,card.get('account_currency'),open_upl=upl)
            except Exception as exc:
                try:
                    store.event('warning','Admin trade stats failed',
                                {'type':type(exc).__name__,'detail':str(exc)[:160]})
                except Exception:
                    pass
                # Keep prior zeros but mark failure so Desk is not a silent lie.
                card['results']={
                    **(card.get('results') or {}),
                    'stats_error':type(exc).__name__,
                }
            try:
                from . import hard_risk
                card['hard_risk']=hard_risk.snapshot(store)
            except Exception:
                card['hard_risk']=None
            # Desk board even when robot is stopped (store-only truth).
            try:
                act=card.setdefault('activity',{})
                if not isinstance(act.get('desk_system'), dict):
                    class _StopRT:
                        pass
                    fake=rt if rt is not None else _StopRT()
                    if getattr(fake,'store',None) is None:
                        fake.store=store
                    act['desk_system']=self._desk_system_payload(fake, snap=None)
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

    def admin_client_structure_view(self,client_id: int,*,n: int=90) -> dict:
        """OHLC + swing S/R + prospective entry zones for Admin mini chart.

        Not every-candle signals — zone bands from structure so the operator
        can see where a future BUY/SELL entry would sit relative to swings.
        """
        from . import (
            structure_memory, bracket_learn, ohlc_entry_zones, mtf_momentum,
            ohlc_scenarios,
        )
        cid=int(client_id)
        rt=self.get(cid)
        store=None
        epic=None
        mid=None
        spread=0.0
        tick=0.01
        action=None
        scores={}
        positions=[]
        try:
            store=self._client_store(cid)
        except FileNotFoundError:
            return {'ok':False,'reason':'no_store','client_id':cid,
                    'candles':[],'zones':{},'levels':{}}
        epic_source=None
        eng=None
        if rt and rt.started and rt.engine is not None:
            eng=rt.engine
            market=getattr(eng,'market',None) or {}
            mandate=(getattr(eng,'mandate',None) or {})
            if market.get('epic'):
                epic=market.get('epic');epic_source='live_quote'
            elif mandate.get('epic'):
                epic=mandate.get('epic');epic_source='mandate'
            elif getattr(eng.brain,'epic',None):
                epic=eng.brain.epic;epic_source='brain'
            mid=market.get('mid')
            try:
                spread=float(market.get('spread') or 0)
            except (TypeError,ValueError):
                spread=0.0
            try:
                tick=float(market.get('tick') or 0.01) or 0.01
            except (TypeError,ValueError):
                tick=0.01
            sig=getattr(eng,'last_signal',None) or {}
            if isinstance(sig,dict):
                action=sig.get('action')
                scores=sig.get('scores') or {}
            positions=list(getattr(eng,'positions',None) or [])
        if not epic:
            epic=store.get('legacy_brain_epic')
            if isinstance(epic,str):
                epic=epic.strip('"') or None
            if epic:
                epic_source='legacy_brain'
        if not epic:
            try:
                row=store.db.execute(
                    "SELECT epic FROM trades ORDER BY opened DESC LIMIT 1").fetchone()
                epic=row[0] if row else None
                if epic:
                    epic_source='last_trade'
            except Exception:
                epic=None
        client_name=None
        username=None
        try:
            crow=self.platform.get_client(cid)
            if isinstance(crow,dict):
                client_name=crow.get('display_name') or crow.get('name')
                username=crow.get('username')
        except Exception:
            pass
        # Never invent GOLD — robot trades whatever Capital mandate/epic is live.
        if not epic:
            return {
                'ok':False,'reason':'no_epic','client_id':cid,
                'client_name':client_name,'display_name':client_name,'username':username,
                'epic':None,'epic_source':None,'candles':[],'zones':{},
                'structure':{},'swing_brackets':None,'open_levels':[],
                'hint':'Atver manuālu darījumu Capital.com jebkurā tirgū — robots adoptē to epic un OHLC.',
            }
        display_n=max(40,min(int(n),180))
        # Fetch enough M1 for MTF (KAMA/ADX/ATR) — same floor LIVE uses.
        try:
            need_mtf=mtf_momentum.min_history_bars(store)
        except Exception:
            need_mtf=720
        hist_n=max(display_n,need_mtf)
        candles_raw=store.candles(epic,n=hist_n,historical_only=False) or []
        # Same cutoff as LIVE OPEN — forming QUOTE minute must not move zones.
        candles_full=ohlc_entry_zones.drop_forming_minute(candles_raw)
        # Chart display includes the live forming minute so Admin tracks Capital
        # tape mid-minute (zones/MTF still use completed bars only).
        candles_chart=list(candles_full or [])
        if candles_raw:
            last_raw=candles_raw[-1]
            last_full=candles_full[-1] if candles_full else None
            if (isinstance(last_raw,dict) and (
                    not last_full or str(last_raw.get('at') or '')!=str(last_full.get('at') or ''))):
                # Prefer live mid as forming close when engine quote is fresher.
                row=dict(last_raw)
                if mid is not None:
                    try:
                        m=float(mid)
                        row['close']=m
                        row['high']=max(float(row.get('high') or m),m)
                        row['low']=min(float(row.get('low') or m),m)
                    except (TypeError,ValueError):
                        pass
                candles_chart.append(row)
        candles=candles_chart[-display_n:] if candles_chart else []
        if mid is None and candles:
            try:
                mid=float(candles[-1].get('close'))
            except (TypeError,ValueError,AttributeError):
                mid=None
        overlays=mtf_momentum.chart_overlays(
            candles_full or candles,store=store,epic=epic,
            display_n=display_n,spread=spread)
        mtf=(overlays.get('analysis') if isinstance(overlays,dict) else None)
        # Same OHLC+MTF entry geometry LIVE waits for before OPEN.
        ez=ohlc_entry_zones.compute_entry_zones(
            candles_full or candles,mid=mid,tick=tick,spread=spread,mtf=mtf,
            store=store)
        live=structure_memory.snapshot_live(
            candles=candles_chart or candles_full or candles,mid=mid) if (
                candles_chart or candles_full or candles) else {}
        path=structure_memory.normalise(candles_full or candles,limit=80) \
            if (candles_full or candles) else []
        piv=structure_memory.swing_pivots(path,left=2,right=2) if path else {}
        range_hi=ez.get('range_hi')
        range_lo=ez.get('range_lo')
        buy_zone=ez.get('buy_entry')
        sell_zone=ez.get('sell_entry')
        # Same-epic shared side first — do not let two clients paint BUY vs SELL
        # zones on one GOLD tape just because neural scores differ.
        preferred=None
        shared_side=None
        try:
            from . import shared_market
            shared_side=shared_market.adopt_side(epic)
        except Exception:
            shared_side=None
        if isinstance(shared_side,dict):
            ss=str(shared_side.get('side') or '').upper()
            if ss in ('BUY','SELL'):
                preferred=ss
        if not preferred and isinstance(mtf,dict) and mtf.get('ready'):
            mp=str(mtf.get('preferred') or '').upper()
            mode=str(mtf.get('mode') or '').upper()
            if mp in ('BUY','SELL'):
                preferred=mp
            # MTF HOLD — do not invent a BUY/SELL zona from neural max scores
            # (that painted opposite sides across clients while MTF said HOLD).
            elif mode=='HOLD':
                preferred=None
        if not preferred and action in ('BUY','SELL'):
            preferred=action
        if not preferred and isinstance(scores,dict):
            mtf_hold=(isinstance(mtf,dict) and str(mtf.get('mode') or '').upper()=='HOLD')
            if not mtf_hold:
                try:
                    preferred=max(('BUY','SELL','HOLD'),
                                  key=lambda a:float(scores.get(a) or 0))
                    if preferred=='HOLD':
                        preferred=None
                except (TypeError,ValueError):
                    preferred=None
        # Live position direction wins for Admin preferred (truth on tape).
        open_dirs=[str(p.get('direction') or '').upper()
                   for p in positions if isinstance(p,dict)]
        open_dirs=[d for d in open_dirs if d in ('BUY','SELL')]
        if open_dirs:
            preferred=open_dirs[0]
        # Publish from this client's MTF/structure so peers can adopt.
        # Skip while in trade — open deal already owns the preferred side.
        if preferred in ('BUY','SELL') and epic and not open_dirs:
            try:
                from . import shared_market
                conf=(mtf or {}).get('confidence') if isinstance(mtf,dict) else None
                shared_market.publish_side(
                    epic,preferred,
                    source='admin_structure_view',
                    confidence=conf,
                    bias=(ez.get('bias') if isinstance(ez,dict) else None),
                    event=(ez.get('event') if isinstance(ez,dict) else None))
            except Exception:
                pass
        atr_hint=None
        kama_hint=None
        # Bracket + Desk ATR14 = M1 only. Never fall back to M5 stop_hint
        # (GOLD M5 ~7.5 used to paint as "ATR14" and disagree with the tape).
        if isinstance(mtf,dict) and mtf.get('ready'):
            tf=mtf.get('tf') or {}
            atr_hint=_finite_num((tf.get('m1') or {}).get('atr14'))
            kama_hint=(tf.get('m5') or {}).get('kama')
            if kama_hint is None:
                kama_hint=(tf.get('m1') or {}).get('kama')
        if atr_hint is None and isinstance(overlays,dict):
            # overlays.atr14 is M1 after chart_overlays fix — still refuse stop_hint.
            atr_hint=_finite_num(overlays.get('atr14'))
        if atr_hint is None and isinstance(ez,dict):
            atr_hint=_finite_num(ez.get('atr14'))
        if atr_hint is None and (candles_full or candles):
            try:
                from .ohlc_entry_zones import _m1_atr_from_candles
                atr_hint=_finite_num(_m1_atr_from_candles(candles_full or candles))
            except Exception:
                atr_hint=None
        if kama_hint is None and isinstance(overlays,dict):
            kama_hint=overlays.get('kama_level')
        swing_brackets=None
        open_levels=[]
        def _level_or_none(raw):
            """Broker unset SL/TP often arrives as 0 — omit for chart + Admin."""
            try:
                n=float(raw)
            except (TypeError,ValueError):
                return None
            if not math.isfinite(n) or n<=0:
                return None
            return n
        for p in positions:
            if not isinstance(p,dict):
                continue
            open_levels.append({
                'deal_id':p.get('dealId'),
                'direction':p.get('direction'),
                'entry':_level_or_none(p.get('level')),
                'stop':_level_or_none(p.get('stopLevel')),
                'profit':_level_or_none(p.get('profitLevel')),
            })
        # Prospective swing brackets only when flat — never stack on live poz.
        if not open_levels:
            entry_px=mid
            if preferred=='BUY' and mid is not None:
                entry_px=mid+(spread/2.0 if spread else 0)
            elif preferred=='SELL' and mid is not None:
                entry_px=mid-(spread/2.0 if spread else 0)
            min_dist=0.
            try:
                # Broker min stop when known — Admin preview must match LIVE.
                min_dist=float((eng.market or {}).get('min_distance')
                               or (eng.market or {}).get('minStopDistance')
                               or 0) if eng else 0.
            except (TypeError,ValueError,AttributeError):
                min_dist=0.
            if entry_px is not None and preferred in ('BUY','SELL') and (candles_full or candles):
                try:
                    zone=bracket_learn.zone_at_open(
                        candles_full or candles,multi_tf=True)
                    swing_brackets=bracket_learn.swing_anchor_brackets(
                        candles=candles_full or candles,entry=entry_px,action=preferred,
                        tick=tick,spread=spread,min_distance=min_dist,zone=zone,
                        atr=atr_hint,kama=kama_hint,store=store)
                except Exception as exc:
                    swing_brackets={'ready':False,'reason':type(exc).__name__}
        # Compact candle payload for canvas (popup may ask for more history).
        bare=[]
        for c in candles:
            if not isinstance(c,dict):
                continue
            try:
                bare.append({
                    'at':c.get('at'),
                    'open':float(c['open']),'high':float(c['high']),
                    'low':float(c['low']),'close':float(c['close']),
                })
            except (KeyError,TypeError,ValueError):
                continue
        # Drop heavy nested analysis from overlays for the wire (keep series+scalars).
        overlay_wire=None
        if isinstance(overlays,dict):
            overlay_wire={k:v for k,v in overlays.items() if k!='analysis'}
        return {
            'ok':True,
            'client_id':cid,
            'client_name':client_name,
            'display_name':client_name,
            'username':username,
            'epic':epic,
            'epic_source':epic_source,
            'mid':mid,
            'action':action,
            'preferred':preferred,
            'shared_side':(shared_side.get('side')
                           if isinstance(shared_side,dict) else None),
            'shared_side_source':(shared_side.get('source')
                                 if isinstance(shared_side,dict) else None),
            'scores':scores,
            'candles':bare,
            'structure':{
                'bias':ez.get('bias') or (live or {}).get('live_bias'),
                'event':ez.get('event') or (live or {}).get('live_event'),
                'range_hi':range_hi,
                'range_lo':range_lo,
                # Remap pivot i (path-relative) onto the displayed candle window.
                'swing_highs':_remap_swing_marks(
                    piv.get('swing_highs'),len(candles_full or []),len(bare),
                    path_n=len(path or [])),
                'swing_lows':_remap_swing_marks(
                    piv.get('swing_lows'),len(candles_full or []),len(bare),
                    path_n=len(path or [])),
            },
            'zones':{
                'support':ez.get('support'),
                'resistance':ez.get('resistance'),
                'buy_entry':buy_zone,
                'sell_entry':sell_zone,
            },
            'entry_zone_source':'ohlc_entry_zones',
            'info_stack':ez.get('info_stack') or 'ohlc_base_impulse',
            'zone_structure':ez.get('structure'),
            'demand_zones':ez.get('demand_zones') or [],
            'supply_zones':ez.get('supply_zones') or [],
            'scenario':ohlc_scenarios.classify(
                candles_full or candles,mid=mid,zones=ez,tick=tick,store=store),
            'entry_path':(getattr(eng,'_entry_path',None) if eng is not None else None),
            'overlays':overlay_wire,
            'mtf':{
                'ready':bool(isinstance(mtf,dict) and mtf.get('ready')),
                'mode':(mtf or {}).get('mode') if isinstance(mtf,dict) else None,
                'preferred':(mtf or {}).get('preferred') if isinstance(mtf,dict) else None,
                'confidence':(mtf or {}).get('confidence') if isinstance(mtf,dict) else None,
                'reason':(mtf or {}).get('reason') if isinstance(mtf,dict) else None,
                'atr14':atr_hint,
                'kama':kama_hint,
                'adx':(overlay_wire or {}).get('adx'),
                'di_plus':(overlay_wire or {}).get('di_plus'),
                'di_minus':(overlay_wire or {}).get('di_minus'),
            },
            'swing_brackets':swing_brackets,
            'open_levels':open_levels,
        }

    def admin_markets_ohlc(self,*,n: int=90) -> dict:
        """One shared OHLC board per open Capital epic across the fleet.

        Admin main screen shows these instead of a chart on every client card.
        A new epic (client opens another market) gets its own panel.
        """
        by_epic={}
        for client in self.platform.list_clients():
            cid=int(client['id'])
            name=client.get('display_name') or client.get('username') or f'#{cid}'
            epics=set()
            rt=self.get(cid)
            if rt and rt.started and rt.engine is not None:
                eng=rt.engine
                market=getattr(eng,'market',None) or {}
                mandate=getattr(eng,'mandate',None) or {}
                for raw in (market.get('epic'),mandate.get('epic'),
                            getattr(getattr(eng,'brain',None),'epic',None)):
                    if raw:
                        epics.add(str(raw))
                try:
                    snap=eng.snapshot()
                except Exception:
                    snap={}
                for p in (snap.get('positions') or []):
                    if isinstance(p,dict) and p.get('epic'):
                        epics.add(str(p['epic']))
            if not epics:
                continue
            for epic in epics:
                row=by_epic.get(epic)
                if row is None:
                    by_epic[epic]={
                        'epic':epic,
                        'donor_client_id':cid,
                        'client_ids':[cid],
                        'client_names':[name],
                    }
                else:
                    row['client_ids'].append(cid)
                    row['client_names'].append(name)
        markets=[]
        for epic in sorted(by_epic.keys()):
            meta=by_epic[epic]
            donor=int(meta['donor_client_id'])
            try:
                view=self.admin_client_structure_view(donor,n=n)
            except Exception as exc:
                view={
                    'ok':False,'reason':type(exc).__name__,'hint':str(exc),
                    'candles':[],'zones':{},'client_id':donor,'epic':epic,
                }
            # Prefer the requested epic label even if donor mid drifted briefly.
            if not view.get('epic'):
                view['epic']=epic
            view['donor_client_id']=donor
            view['client_ids']=list(meta['client_ids'])
            view['client_names']=list(meta['client_names'])
            markets.append(view)
        return {'markets':markets,'count':len(markets)}

    async def prepare_new_market(self,client_id: int) -> dict:
        """Clear mandate so the next Capital manual trade adopts ANY epic.

        Neural weights / per-epic learning bags stay parked. OHLC entry+SL/TP
        will follow the newly adopted instrument's candles — not a GOLD lock.
        """
        cid=int(client_id)
        rt=self.get(cid)
        if not rt or not rt.started or rt.engine is None:
            raise RuntimeError('Robots nav palaists')
        if not rt.engine.broker:
            raise RuntimeError('Nav brokera sesijas')
        out=await rt.engine.prepare_new_instrument()
        return {
            'ok':True,
            'client_id':cid,
            'previous_epic':out.get('previous_epic'),
            'next_step':out.get('next_step') or (
                'Aizver veco pozīciju un Capital.com atver pirmo darījumu jaunajā tirgū'),
            'note':'Robots nav piesaistīts tikai GOLD — adoptē jebkuru Capital epic; OHLC SL/TP no tā tirgus svecēm.',
        }

    def client_hard_risk(self,client_id: int) -> dict:
        from . import hard_risk
        return hard_risk.snapshot(self._client_store(client_id))


    def client_day_risk_gates(self,client_id: int) -> dict:
        """Day phase is informational only — never blocks LIVE OPEN."""
        from . import day_book
        store=self._client_store(client_id)
        snap=day_book.snapshot(store)
        return {
            'risk_gates':False,
            'day_risk_gates':0,
            'phase':snap.get('phase'),
            'goal':snap.get('goal'),
            'note':('LIVE: DEFEND/DONE/PROTECT never block OPEN. '
                    'day_risk_gates flag is ignored by the engine.'),
        }

    def set_client_day_risk_gates(self,client_id: int,*,enabled: bool) -> dict:
        """Kept for Admin API compat — always forces OFF (no risk regimes)."""
        from .self_params import load as sp_load, save as sp_save
        store=self._client_store(client_id,create=True)
        params=sp_load(store)
        params['day_risk_gates']=0
        sp_save(store,params,reason='admin_day_risk_gates_force_off')
        store.event('risk','Operator day risk gates forced OFF (LIVE)',{
            'day_risk_gates':0,'requested_enabled':bool(enabled)})
        return self.client_day_risk_gates(client_id)

    def save_client_hard_risk(self,client_id: int,*,max_risk_per_trade,daily_loss_limit,
                              equity_drawdown_limit,clear_halt: bool=False) -> dict:
        """Operator sets optional monetary limits — informational; never required for OPEN."""
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

    async def refresh_live_truth(self, *, client_id: int | None = None,
                                 force: bool = False) -> dict:
        """Await Funds mirror + remap news/peers + boot shadow before Desk paint.

        ensure_future on the sync card path raced the HTTP response — Desk kept
        −0.81 / UNMAPPED / shadow off until a later poll luckily caught up.
        Mirror is awaited at most every ~12s per client so 2s Desk polls stay snappy.
        """
        import time as _time
        out = {'mirrored': 0, 'shadow': 0, 'remapped': 0, 'peers': 0, 'skipped': 0}
        if not hasattr(self, '_live_truth_last'):
            self._live_truth_last = {}
        clients = self.platform.list_clients()
        now_m = _time.monotonic()
        for c in clients:
            cid = int(c.get('id') or 0)
            if client_id is not None and cid != int(client_id):
                continue
            rt = self.get(cid)
            if not rt or not getattr(rt, 'started', False):
                continue
            eng = getattr(rt, 'engine', None)
            if eng is None:
                continue
            last = float(self._live_truth_last.get(cid) or 0)
            # Remap + shadow every poll (cheap). Capital Funds HTTP only on cooldown.
            do_mirror = force or (now_m - last) >= 12.0
            if hasattr(eng, '_ensure_instrument_mapped'):
                try:
                    before = dict(getattr(eng, 'instrument_profile', None) or {})
                    prof = eng._ensure_instrument_mapped()
                    if (prof or {}).get('currencies') and not (before.get('currencies')):
                        out['remapped'] += 1
                except Exception:
                    pass
            news_live = None
            if hasattr(eng, '_news_risk'):
                try:
                    news_live = eng._news_risk()
                except Exception:
                    news_live = None
            if hasattr(eng, '_ensure_shadow_brain'):
                try:
                    if eng._ensure_shadow_brain() is not None:
                        out['shadow'] += 1
                except Exception:
                    pass
            im = getattr(eng, 'intermarket', None)
            st = (getattr(im, 'state', None) or {}) if im is not None else {}
            if (do_mirror and im is not None and hasattr(eng, 'broker') and eng.broker
                    and str(st.get('status') or '') in ('WAITING', 'UNAVAILABLE', '')):
                try:
                    epic = (getattr(eng, 'mandate', None) or {}).get('epic')
                    inst = {}
                    bm = getattr(eng, '_last_broker_market', None) or {}
                    if isinstance(bm, dict):
                        inst = bm.get('instrument') if isinstance(
                            bm.get('instrument'), dict) else {}
                    candles = []
                    if epic and getattr(eng, 'store', None) is not None:
                        candles = eng.store.candles(epic, n=80, historical_only=True)
                    if epic and hasattr(im, 'refresh'):
                        await asyncio.wait_for(
                            im.refresh(eng.broker, epic, inst, candles),
                            timeout=6.0)
                        out['peers'] += 1
                        st = dict(getattr(im, 'state', None) or {})
                except Exception:
                    pass
            # Keep SHADOW "Live today" + INTEL in sync with remapped news/peers.
            if getattr(eng, 'store', None) is not None and (
                    isinstance(news_live, dict) or isinstance(st, dict)):
                try:
                    from datetime import datetime, timezone
                    eng.store.set('live_external_context', {
                        'at': datetime.now(timezone.utc).isoformat(),
                        'news': news_live if isinstance(news_live, dict) else {},
                        'intermarket': st if isinstance(st, dict) else {},
                    })
                except Exception:
                    pass
            if do_mirror and hasattr(eng, '_mirror_capital_funds_closes') and eng.broker:
                try:
                    n = await asyncio.wait_for(
                        eng._mirror_capital_funds_closes(), timeout=10.0)
                    out['mirrored'] += int(n or 0)
                    self._live_truth_last[cid] = now_m
                except Exception:
                    # Still stamp cooldown so a dead broker cannot stall every poll.
                    self._live_truth_last[cid] = now_m
            elif not do_mirror:
                out['skipped'] += 1
            if hasattr(eng, '_backfill_mistake_audits_from_journal'):
                try:
                    eng._backfill_mistake_audits_from_journal(limit=400, hours=24)
                except Exception:
                    pass
        return out

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
        open_upl_by_ccy: dict[str,float]={}
        display_wins=0
        display_losses=0
        open_positions=0
        for c in clients:
            res=c.get('results') or {}
            act=c.get('activity') or {}
            for ccy,val in (res.get('pnl_by_currency') or {}).items():
                by_ccy[ccy]=round(by_ccy.get(ccy,0.0)+float(val or 0),4)
            if not res.get('pnl_by_currency') and res.get('pnl_currency') and res.get('realized_pnl') is not None:
                ccy=res['pnl_currency']
                by_ccy[ccy]=round(by_ccy.get(ccy,0.0)+float(res.get('realized_pnl') or 0),4)
            wp=res.get('working_pnl')
            wsrc=(res.get('working_pnl_source') or '').strip().lower()
            wccy=(res.get('pnl_currency') or c.get('account_currency') or '').strip().upper() or None
            # Skip empty/none/≈UPL placeholders — they used to invent fleet totals.
            if wp is not None and wccy and wsrc not in ('none','empty','','upl_estimate'):
                working_by_ccy[wccy]=round(working_by_ccy.get(wccy,0.0)+float(wp),4)
            ou=res.get('open_upl')
            if ou is not None and wccy:
                try:
                    ou_f=float(ou)
                except (TypeError,ValueError):
                    ou_f=None
                if ou_f is not None and __import__('math').isfinite(ou_f):
                    open_upl_by_ccy[wccy]=round(open_upl_by_ccy.get(wccy,0.0)+ou_f,4)
            pos_list=act.get('positions') or (
                [act['position']] if isinstance(act.get('position'),dict) else [])
            open_positions+=sum(1 for p in pos_list if isinstance(p,dict))
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
        open_upl_primary=None
        if len(open_upl_by_ccy)==1:
            ccy,val=next(iter(open_upl_by_ccy.items()))
            open_upl_primary={'currency':ccy,'open_upl':val}
        if total_closed>0 and total_cash==0 and total_estimate>0:
            pnl_status='upl_estimate_pending'
        elif total_closed>0 and total_cash==0:
            pnl_status='closed_without_broker_cash'
        elif total_pending>0:
            pnl_status='broker_cash_pending'
        elif total_cash>0 and total_estimate>0:
            pnl_status='broker_cash_partial'
        elif total_cash>0:
            pnl_status='broker_cash_ok'
        else:
            pnl_status='no_closes'
        wl_source=('broker' if total_cash>0 else
                   ('upl_estimate' if total_estimate>0 else 'none'))
        # Live open UPL beats stuck ≈UPL closes — Desk must not wall on gaida.
        working_src=('broker' if total_cash>0 else
                     ('open_upl' if open_upl_primary else
                      ('upl_estimate' if total_estimate>0 else 'none')))
        estimate_pnl_all=None
        if total_estimate>0 and not working_primary and not open_upl_primary:
            est_sum=0.0
            est_ccy=None
            for c in clients:
                res=c.get('results') or {}
                ep=res.get('estimate_pnl')
                if ep is None:
                    continue
                try:
                    est_sum+=float(ep)
                except (TypeError,ValueError):
                    continue
                if est_ccy is None:
                    est_ccy=((res.get('pnl_currency') or c.get('account_currency') or '')
                             .strip().upper() or None)
            estimate_pnl_all=round(est_sum,4)
        return {
            'summary':{
                'clients':len(clients),
                'running_robots':running,
                'open_trades_all':open_positions,
                # Multi-currency: never sum GBP+USD into one number (or fake 0.0).
                'realized_pnl_all':primary['realized_pnl'] if primary else None,
                'working_pnl_all':(working_primary['working_pnl'] if working_primary else None),
                'working_pnl_source':working_src,
                'open_upl_all':(open_upl_primary['open_upl'] if open_upl_primary else None),
                'open_upl_by_currency':open_upl_by_ccy,
                'estimate_pnl_all':estimate_pnl_all,
                'pnl_currency':(working_primary['currency'] if working_primary
                                else (open_upl_primary['currency'] if open_upl_primary
                                      else (primary['currency'] if primary else None))),
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
            engine.client_id=client_id
            # Same-epic MTF/OHLC peers — market perception shared, brains private.
            engine._peer_engines_fn=lambda cid=client_id: [
                r.engine for r in self.all_runtimes()
                if r and r.engine is not None and int(r.client_id)!=int(cid)
            ]
            creds=self.platform.broker_credentials(broker_row)
            store.event('learning','Per-client neural runtime started',
                        {'client_id':client_id,'neural_seed':seed,
                         'data_dir':str(data_dir),
                         'shared_epic_market':True})
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
                    # Fast WS quote ticks while flat + intent — catch break/zone
                    # without waiting the next heavy 3s poll.
                    if (not engine.positions and engine.mandate
                            and hasattr(engine,'quote_entry_tick')
                            and engine._wants_fast_entry_ticks()):
                        for _ in range(2):
                            await asyncio.sleep(1)
                            try:
                                await engine.quote_entry_tick()
                            except Exception:
                                break
                        continue
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
