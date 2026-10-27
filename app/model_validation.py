"""Independent model validation — Walk-Forward / OOS vs incumbent.

Promotes only when candidate shows better net PnL with controlled drawdown on
chronological holdout. Rejects under-powered or overfit shadows.

Rollback restores previous *neural weight files* when a backup path is present —
not merely version metadata.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

STORE_KEY = 'model_validation_log_v1'
MODEL_STACK_KEY = 'model_version_stack_v1'


def _cfg(store=None):
    from .self_params import load
    return load(store)


def _equity_curve(pnls):
    eq=np.cumsum(np.asarray(pnls,dtype=float))
    if len(eq)==0:return eq,0.,0.
    peak=np.maximum.accumulate(eq)
    dd=eq-peak
    return eq,float(eq[-1]),float(dd.min()) if len(dd) else 0.


def score_trade_pnls(pnls, *, store=None):
    """Net, max drawdown, stability (share of positive rolling windows)."""
    cfg=_cfg(store)
    min_oos=int(cfg['model_min_oos'])
    pnls=[float(x) for x in pnls if x is not None and math.isfinite(float(x))]
    if len(pnls)<min_oos:
        return {'ok':False,'reason':f'need ≥{min_oos} OOS outcomes','n':len(pnls)}
    _,net,dd=_equity_curve(pnls)
    block=max(4,len(pnls)//5)
    pos=0;total=0
    for i in range(0,len(pnls)-block+1,block):
        total+=1
        if sum(pnls[i:i+block])>0:pos+=1
    stability=pos/max(total,1)
    return {'ok':True,'n':len(pnls),'net':net,'max_dd':dd,'stability':stability}


def compare_models(incumbent_pnls, candidate_pnls, *, store=None):
    """Return promote decision with reasons — thresholds from self_params."""
    cfg=_cfg(store)
    dd_ratio=float(cfg['model_max_dd_ratio'])
    slack=float(cfg['model_stability_slack'])
    inc=score_trade_pnls(incumbent_pnls,store=store)
    cand=score_trade_pnls(candidate_pnls,store=store)
    if not cand.get('ok'):
        return {'promote':False,'reason':cand.get('reason','candidate weak'),'incumbent':inc,'candidate':cand}
    if not inc.get('ok'):
        if cand['stability']<.4:
            return {'promote':False,'reason':'incumbent thin and candidate unstable',
                    'incumbent':inc,'candidate':cand}
        return {'promote':True,'reason':'cold-start candidate acceptable',
                'incumbent':inc,'candidate':cand}
    if cand['net']<=inc['net']:
        return {'promote':False,'reason':'candidate net PnL not better',
                'incumbent':inc,'candidate':cand}
    if cand['max_dd']<inc['max_dd']*dd_ratio and inc['max_dd']<0:
        return {'promote':False,'reason':'candidate drawdown worse than allowed',
                'incumbent':inc,'candidate':cand}
    if cand['stability']+1e-9<inc['stability']-slack:
        return {'promote':False,'reason':'candidate less stable',
                'incumbent':inc,'candidate':cand}
    return {'promote':True,'reason':'OOS net/risk/stability beat incumbent',
            'incumbent':inc,'candidate':cand}


def walk_forward_split(rows, *, train_frac=None, store=None):
    """Chronological split — caller must pre-sort by time."""
    cfg=_cfg(store)
    # self_params val_oos_frac is OOS share → train = 1 - oos.
    if train_frac is None:
        train_frac=1.0-float(cfg['val_oos_frac'])
    n=len(rows)
    if n<int(cfg['model_min_oos']):return [],list(rows)
    cut=int(n*float(train_frac))
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
        from .self_params import evolve_from_feedback
        decision=compare_models(incumbent_pnls,candidate_pnls,store=self.store)
        entry={'at':datetime.now(timezone.utc).isoformat(),
               'decision':decision,'meta':meta or {}}
        self._persist_log(entry)
        self.store.event('model_validation',
            'Candidate '+('PROMOTE' if decision['promote'] else 'REJECT'),
            {'reason':decision['reason'],'meta':meta or {}})
        evolve_from_feedback(self.store,shadow_promote=bool(decision.get('promote')),
                             commit=False)
        return decision

    def push_version(self, version_meta):
        """Record live model version for rollback (include weights_path when set)."""
        self.stack.append({**version_meta,
            'pushed_at':datetime.now(timezone.utc).isoformat()})
        self.store.set(MODEL_STACK_KEY,self.stack[-20:])

    def previous_version(self):
        if len(self.stack)<2:return None
        return self.stack[-2]

    def mark_rollback(self, reason, *, restore_fn=None):
        """Flag rollback and restore neural weights + feature registry snapshot.

        restore_fn(version_meta) → bool should reload AdaptiveBrain weights and
        FeatureRegistry approved specs from the previous stack entry.
        """
        prev=self.previous_version()
        restored=False
        weights_path=None
        registry_restored=False
        if prev:
            weights_path=prev.get('weights_path')
            if restore_fn is not None and (weights_path or prev.get('approved_specs') is not None):
                try:
                    restored=bool(restore_fn(prev))
                    registry_restored=bool(prev.get('approved_specs') is not None and restored)
                except Exception as exc:
                    self.store.event('model_validation','Model snapshot restore failed',
                        {'reason':reason,'path':weights_path,'type':type(exc).__name__})
                    restored=False
            elif weights_path and Path(str(weights_path)).exists() and restore_fn is None:
                restored=False
        self.store.event('model_validation',
            'Rollback '+('restored weights+registry' if restored else 'requested'),
            {'reason':reason,'previous':prev,'weights_path':weights_path,
             'weights_restored':restored,'registry_restored':registry_restored})
        if prev and self.stack:
            self.stack.append({**prev,'restored_at':datetime.now(timezone.utc).isoformat(),
                               'rollback_reason':reason,'weights_restored':restored,
                               'registry_restored':registry_restored})
            self.store.set(MODEL_STACK_KEY,self.stack[-20:])
        return {**(prev or {}), 'weights_restored':restored,
                'registry_restored':registry_restored} if prev else None
