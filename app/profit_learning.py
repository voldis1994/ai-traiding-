"""Profit-centred learning from closed trades + hypothetical BUY/SELL/HOLD.

Separates:
  - actual broker £ (source of truth for LIVE regret)
  - simulated counterfactual £ (spread + optional slip only — never claimed as cash)

Does NOT invent volume. Costs = spread (+ slip_bps if provided).
Persists history across restart via Store.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'profit_learning_history_v1'
MAX_ROWS = 400


def _finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None


def hypothetical_pnl(direction, entry, exit_level, *, spread=0., slip_bps=0., size=1.):
    """Gross price move minus round-trip cost proxy (price units)."""
    entry=_finite(entry);exit_level=_finite(exit_level)
    if entry is None or exit_level is None or direction not in ('BUY','SELL'):
        return None
    sign=1 if direction=='BUY' else -1
    move=sign*(exit_level-entry)
    mid=max(abs(entry),1e-9)
    slip=abs(float(slip_bps or 0))/10000.*mid
    cost=abs(float(spread or 0))+slip
    return float((move-cost)*float(size or 1.))


def best_action_from_counterfactual(entry, exit_level, *, spread=0., slip_bps=0.):
    """Which of BUY/SELL/HOLD would have been best after costs (simulation only)."""
    buy=hypothetical_pnl('BUY',entry,exit_level,spread=spread,slip_bps=slip_bps)
    sell=hypothetical_pnl('SELL',entry,exit_level,spread=spread,slip_bps=slip_bps)
    if buy is None or sell is None:return None
    # HOLD ≈ 0 after deciding not to pay spread.
    hold=0.
    ranking=sorted([('BUY',buy),('SELL',sell),('HOLD',hold)],key=lambda x:-x[1])
    return {'best':ranking[0][0],'buy':buy,'sell':sell,'hold':hold,'ranking':ranking}


def label_from_profit(direction, broker_pnl, *, flip_after_loss=False,
                      entry=None, exit_level=None, spread=0., slip_bps=0.):
    """Map £ outcome → BUY/SELL/HOLD class. Flip-loss → HOLD (no auto-opposite).

    Labels: 0=BUY, 1=SELL, 2=HOLD — same as NeuralCore.
    """
    pnl=_finite(broker_pnl)
    if flip_after_loss and pnl is not None and pnl<0:
        return {'label':2,'mode':'chop_hold_after_flip_loss','actual_pnl':pnl,
                'simulated':None}
    if pnl is not None and pnl>0 and direction in ('BUY','SELL'):
        return {'label':0 if direction=='BUY' else 1,'mode':'reinforce_win',
                'actual_pnl':pnl,'simulated':None}
    if pnl is not None and pnl<0 and direction in ('BUY','SELL'):
        # Spec: do NOT automatically train the opposite side after a loss.
        # Counterfactual is logged for audit; label stays HOLD unless flip path above.
        sim=best_action_from_counterfactual(entry,exit_level,spread=spread,slip_bps=slip_bps)
        mode='loss_abstain_hold' if sim and sim['best']=='HOLD' else 'loss_hold_default'
        return {'label':2,'mode':mode,'actual_pnl':pnl,'simulated':sim}
    return {'label':2,'mode':'flat_hold','actual_pnl':pnl,'simulated':None}


class ProfitLearning:
    def __init__(self, store):
        self.store=store
        self.history=list(self.store.get(STORE_KEY) or [])

    def _save(self):
        self.store.set(STORE_KEY,self.history[-MAX_ROWS:])

    def record_closed(self, *, deal_id, epic, direction, entry, exit_level,
                      broker_pnl, currency=None, spread=0., slip_bps=0.,
                      flip_after_loss=False, model_version=None, feature_schema=None,
                      shadow_action=None, require_broker_pnl=True):
        """Record a closed trade. Fake ±1 cash is refused when require_broker_pnl."""
        actual=_finite(broker_pnl)
        if require_broker_pnl and actual is None:
            # Do not invent cash for the profit ledger — caller may still train ticks.
            return {'label':None,'mode':'skipped_no_broker_pnl','actual_pnl':None,
                    'simulated':best_action_from_counterfactual(
                        entry,exit_level,spread=spread,slip_bps=slip_bps)}
        decision=label_from_profit(direction,actual,flip_after_loss=flip_after_loss,
                                   entry=entry,exit_level=exit_level,spread=spread,
                                   slip_bps=slip_bps)
        sim=best_action_from_counterfactual(entry,exit_level,spread=spread,slip_bps=slip_bps)
        row={
            'at':datetime.now(timezone.utc).isoformat(),
            'deal_id':deal_id,'epic':epic,'direction':direction,
            'entry':_finite(entry),'exit':_finite(exit_level),
            'actual_pnl':actual,'currency':currency,
            'simulated':sim,'label':decision['label'],'mode':decision['mode'],
            'model_version':model_version,'feature_schema':feature_schema,
            'shadow_action':shadow_action,
            'spread':_finite(spread),'slip_bps':_finite(slip_bps)}
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
