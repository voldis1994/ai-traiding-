"""Profit-centred learning from closed trades + hypothetical BUY/SELL/HOLD.

Separates:
  - actual broker £ (source of truth for LIVE regret)
  - simulated counterfactual (path replay with SL/TP/trail + shared costs —
    never claimed as cash)

Does NOT invent volume. Costs use ``trade_costs`` (one RT spread, optional slip).
Persists history across restart via Store.

Loss labels are NOT hardcoded to HOLD. After a loss the supervised target is
whichever of BUY/SELL/HOLD the post-cost **path** actually rewarded — the brain
must discover abstain vs reverse from outcomes, not from a fixed rule.

Audit #15: counterfactual replays the full subsequent OHLC path with the same
SL/TP/trailing/cost rules as shadow paper — not a single entry→exit mid delta.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .trade_costs import net_mid_move

STORE_KEY = 'profit_learning_history_v1'
_SEED_MAX_ROWS = 400
_ACTION_LABEL = {'BUY': 0, 'SELL': 1, 'HOLD': 2}


def _finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None


def hypothetical_pnl(direction, entry, exit_level, *, spread=0., slip_bps=0.,
                     size=1., spread_exit=None, commission=0., funding=0.):
    """Gross mid move minus one RT cost proxy (price units) — endpoint fallback.

    Prefer path counterfactual when OHLC bars exist.
    Uses shared ``trade_costs.net_mid_move`` (never double-count spread).
    """
    return net_mid_move(
        direction,entry,exit_level,spread_entry=spread,spread_exit=spread_exit,
        slip_bps=slip_bps,commission=commission,funding=funding,size=size)


def best_action_from_counterfactual(entry, exit_level, *, spread=0., slip_bps=0.,
                                    bars=None, stop_distance=None,
                                    profit_distance=None, tick=None,
                                    min_distance=None, trail=True,
                                    spread_exit=None):
    """Which of BUY/SELL/HOLD would have been best after costs (simulation only).

    When ``bars`` (full subsequent path) is provided, replay each side with the
    same SL/TP/trail/cost rules as shadow paper. Otherwise fall back to
    endpoint mid delta minus one RT spread (legacy, weaker).
    """
    hold=0.
    detail={'method':None,'buy_sim':None,'sell_sim':None}
    buy=sell=None
    if bars:
        from .shadow_paper import simulate_action_on_path
        buy_s=simulate_action_on_path(
            'BUY',bars,spread=spread,stop_distance=stop_distance,
            profit_distance=profit_distance,tick=tick,min_distance=min_distance,
            slip_bps=slip_bps,trail=trail)
        sell_s=simulate_action_on_path(
            'SELL',bars,spread=spread,stop_distance=stop_distance,
            profit_distance=profit_distance,tick=tick,min_distance=min_distance,
            slip_bps=slip_bps,trail=trail)
        buy=_finite(buy_s.get('pnl')) if buy_s else None
        sell=_finite(sell_s.get('pnl')) if sell_s else None
        detail={'method':'path','buy_sim':buy_s,'sell_sim':sell_s}
        if buy is None or sell is None:
            buy=sell=None
    if buy is None or sell is None:
        buy=hypothetical_pnl('BUY',entry,exit_level,spread=spread,
                             slip_bps=slip_bps,spread_exit=spread_exit)
        sell=hypothetical_pnl('SELL',entry,exit_level,spread=spread,
                              slip_bps=slip_bps,spread_exit=spread_exit)
        detail={'method':'endpoint','buy_sim':None,'sell_sim':None}
    if buy is None or sell is None:return None
    ranking=sorted([('BUY',buy),('SELL',sell),('HOLD',hold)],key=lambda x:-x[1])
    return {'best':ranking[0][0],'buy':buy,'sell':sell,'hold':hold,
            'ranking':ranking,'method':detail['method'],
            'buy_sim':detail.get('buy_sim'),'sell_sim':detail.get('sell_sim')}


def label_from_profit(direction, broker_pnl, *, flip_after_loss=False,
                      entry=None, exit_level=None, spread=0., slip_bps=0.,
                      bars=None, stop_distance=None, profit_distance=None,
                      tick=None, min_distance=None, trail=True, spread_exit=None):
    """Map £ outcome → BUY/SELL/HOLD from the path itself — no forced HOLD-on-loss.

    Wins reinforce the taken side. Losses / flats use post-cost counterfactual
    (BUY vs SELL vs HOLD≈0). flip_after_loss is audit-only (mode suffix), never
    overrides the target to HOLD.

    Labels: 0=BUY, 1=SELL, 2=HOLD — same as NeuralCore.
    label=None when there is nothing honest to supervise.
    """
    pnl=_finite(broker_pnl)
    sim=best_action_from_counterfactual(
        entry,exit_level,spread=spread,slip_bps=slip_bps,bars=bars,
        stop_distance=stop_distance,profit_distance=profit_distance,
        tick=tick,min_distance=min_distance,trail=trail,spread_exit=spread_exit)
    if pnl is not None and pnl>0 and direction in ('BUY','SELL'):
        return {'label':_ACTION_LABEL[direction],'mode':'reinforce_win',
                'actual_pnl':pnl,'simulated':sim}
    if pnl is not None and pnl<0 and direction in ('BUY','SELL'):
        # Cash loss must never reinforce the side that just lost £ (price-unit
        # sim can otherwise re-pick the same side after fees/FX). Choose among
        # HOLD and the opposite path only.
        if sim:
            opposite='SELL' if direction=='BUY' else 'BUY'
            opp_val=float(sim['sell'] if opposite=='SELL' else sim['buy'])
            hold_val=0.
            best=opposite if opp_val>hold_val else 'HOLD'
            prefix='flip_loss_counterfactual_' if flip_after_loss else 'loss_counterfactual_'
            return {'label':_ACTION_LABEL[best],
                    'mode':prefix+best.lower(),
                    'actual_pnl':pnl,'simulated':sim}
        return {'label':None,'mode':'loss_no_counterfactual','actual_pnl':pnl,
                'simulated':None}
    # Flat / unknown £ — still prefer path evidence over inventing HOLD.
    if sim and sim['best'] in _ACTION_LABEL:
        return {'label':_ACTION_LABEL[sim['best']],
                'mode':'flat_counterfactual_'+sim['best'].lower(),
                'actual_pnl':pnl,'simulated':sim}
    return {'label':None,'mode':'flat_no_signal','actual_pnl':pnl,'simulated':None}


class ProfitLearning:
    def __init__(self, store):
        self.store=store
        self.history=list(self.store.get(STORE_KEY) or [])

    def _save(self):
        from .self_params import get as sp_get
        keep=int(sp_get(self.store,'profit_history_rows',_SEED_MAX_ROWS))
        self.store.set(STORE_KEY,self.history[-keep:])

    def record_closed(self, *, deal_id, epic, direction, entry, exit_level,
                      broker_pnl, currency=None, spread=0., slip_bps=0.,
                      flip_after_loss=False, model_version=None, feature_schema=None,
                      shadow_action=None, require_broker_pnl=True,
                      bars=None, stop_distance=None, profit_distance=None,
                      tick=None, min_distance=None, trail=True, spread_exit=None):
        """Record a closed trade. Fake ±1 cash is refused when require_broker_pnl."""
        cf_kw=dict(spread=spread,slip_bps=slip_bps,bars=bars,
                   stop_distance=stop_distance,profit_distance=profit_distance,
                   tick=tick,min_distance=min_distance,trail=trail,
                   spread_exit=spread_exit)
        actual=_finite(broker_pnl)
        if require_broker_pnl and actual is None:
            # Do not invent cash for the profit ledger — caller may still train ticks.
            return {'label':None,'mode':'skipped_no_broker_pnl','actual_pnl':None,
                    'simulated':best_action_from_counterfactual(
                        entry,exit_level,**cf_kw)}
        decision=label_from_profit(direction,actual,flip_after_loss=flip_after_loss,
                                   entry=entry,exit_level=exit_level,**cf_kw)
        sim=best_action_from_counterfactual(entry,exit_level,**cf_kw)
        row={
            'at':datetime.now(timezone.utc).isoformat(),
            'deal_id':deal_id,'epic':epic,'direction':direction,
            'entry':_finite(entry),'exit':_finite(exit_level),
            'actual_pnl':actual,'currency':currency,
            'simulated':{k:sim.get(k) for k in ('best','buy','sell','hold','method','ranking')
                         } if sim else None,
            'label':decision['label'],'mode':decision['mode'],
            'model_version':model_version,'feature_schema':feature_schema,
            'shadow_action':shadow_action,
            'spread':_finite(spread),'slip_bps':_finite(slip_bps),
            'cf_method':(sim or {}).get('method'),
            'stop_distance':_finite(stop_distance),
            'profit_distance':_finite(profit_distance),
            'path_bars':len(bars) if bars else 0}
        self.history.append(row)
        self._save()
        return decision

    def broker_pnls(self):
        """Only real broker £ rows — never ±1 tick placeholders."""
        return [r['actual_pnl'] for r in self.history
                if r.get('actual_pnl') is not None and r.get('mode')!='skipped_no_broker_pnl']

    def summary(self):
        actual=self.broker_pnls()
        return {
            'trades':len(self.history),
            'broker_trades':len(actual),
            'actual_net':float(sum(actual)) if actual else 0.,
            'wins':sum(1 for x in actual if x>0),
            'losses':sum(1 for x in actual if x<0),
            'modes':{m:sum(1 for r in self.history if r.get('mode')==m)
                     for m in sorted({r.get('mode') for r in self.history if r.get('mode')})}}
