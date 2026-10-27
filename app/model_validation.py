"""Independent model validation — Walk-Forward / OOS vs incumbent.

Promotes only when candidate shows better net PnL with controlled drawdown on
chronological holdout. Rejects under-powered or overfit shadows.
Rollback helper restores previous neural snapshot metadata.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

STORE_KEY = 'model_validation_log_v1'
MODEL_STACK_KEY = 'model_version_stack_v1'
MIN_OOS = 24
MIN_LIFT = 0.0  # must beat incumbent net (strict >)
MAX_DD_RATIO = 1.15  # candidate DD may not exceed incumbent by >15%


def _equity_curve(pnls):
    eq=np.cumsum(np.asarray(pnls,dtype=float))
    if len(eq)==0:return eq,0.,0.
    peak=np.maximum.accumulate(eq)
    dd=eq-peak
    return eq,float(eq[-1]),float(dd.min()) if len(dd) else 0.


def score_trade_pnls(pnls):
    """Net, max drawdown, stability (share of positive rolling windows)."""
    pnls=[float(x) for x in pnls if x is not None and math.isfinite(float(x))]
    if len(pnls)<MIN_OOS:
        return {'ok':False,'reason':f'need ≥{MIN_OOS} OOS outcomes','n':len(pnls)}
    _,net,dd=_equity_curve(pnls)
    # Stability: fraction of non-overlapping blocks with positive sum.
    block=max(4,len(pnls)//5)
    pos=0;total=0
    for i in range(0,len(pnls)-block+1,block):
        total+=1
        if sum(pnls[i:i+block])>0:pos+=1
    stability=pos/max(total,1)
    return {'ok':True,'n':len(pnls),'net':net,'max_dd':dd,'stability':stability}


def compare_models(incumbent_pnls, candidate_pnls):
    """Return promote decision with reasons — independent of LIVE path."""
    inc=score_trade_pnls(incumbent_pnls)
    cand=score_trade_pnls(candidate_pnls)
    if not cand.get('ok'):
        return {'promote':False,'reason':cand.get('reason','candidate weak'),'incumbent':inc,'candidate':cand}
    if not inc.get('ok'):
        # Cold start: still require candidate stability.
        if cand['stability']<.4:
            return {'promote':False,'reason':'incumbent thin and candidate unstable',
                    'incumbent':inc,'candidate':cand}
        return {'promote':True,'reason':'cold-start candidate acceptable',
                'incumbent':inc,'candidate':cand}
    if cand['net']<=inc['net']+MIN_LIFT:
        return {'promote':False,'reason':'candidate net PnL not better',
                'incumbent':inc,'candidate':cand}
    # Drawdown: more negative is worse.
    if cand['max_dd']<inc['max_dd']*MAX_DD_RATIO and inc['max_dd']<0:
        return {'promote':False,'reason':'candidate drawdown worse than allowed',
                'incumbent':inc,'candidate':cand}
    if cand['stability']+1e-9<inc['stability']-0.05:
        return {'promote':False,'reason':'candidate less stable',
                'incumbent':inc,'candidate':cand}
    return {'promote':True,'reason':'OOS net/risk/stability beat incumbent',
            'incumbent':inc,'candidate':cand}


def walk_forward_split(rows, *, train_frac=0.7):
    """Chronological split — caller must pre-sort by time."""
    n=len(rows)
    if n<MIN_OOS:return [],list(rows)
    cut=int(n*train_frac)
    return list(rows[:cut]),list(rows[cut:])


class ModelValidator:
    def __init__(self, store):
        self.store=store
        self.log=list(self.store.get(STORE_KEY) or [])
        self.stack=list(self.store.get(MODEL_STACK_KEY) or [])

    def _persist_log(self, entry):
        self.log.append(entry)
        self.store.set(STORE_KEY,self.log[-60:])

    def evaluate(self, incumbent_pnls, candidate_pnls, *, meta=None):
        decision=compare_models(incumbent_pnls,candidate_pnls)
        entry={'at':datetime.now(timezone.utc).isoformat(),
               'decision':decision,'meta':meta or {}}
        self._persist_log(entry)
        self.store.event('model_validation',
            'Candidate '+('PROMOTE' if decision['promote'] else 'REJECT'),
            {'reason':decision['reason'],'meta':meta or {}})
        return decision

    def push_version(self, version_meta):
        """Record live model version for rollback."""
        self.stack.append({**version_meta,
            'pushed_at':datetime.now(timezone.utc).isoformat()})
        self.store.set(MODEL_STACK_KEY,self.stack[-20:])

    def previous_version(self):
        if len(self.stack)<2:return None
        return self.stack[-2]

    def mark_rollback(self, reason):
        prev=self.previous_version()
        self.store.event('model_validation','Rollback requested',
            {'reason':reason,'previous':prev})
        if prev and self.stack:
            self.stack.append({**prev,'restored_at':datetime.now(timezone.utc).isoformat(),
                               'rollback_reason':reason})
            self.store.set(MODEL_STACK_KEY,self.stack[-20:])
        return prev
