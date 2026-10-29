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
    """Equity from starting capital 0 *before* the first trade.

    Without the initial point, the first loss becomes the peak and is skipped
    in max drawdown (12×−1 looked like −11). Prepending 0 makes max_dd = −12.
    """
    arr=np.asarray(list(pnls),dtype=float)
    if len(arr)==0:return arr,0.,0.
    eq=np.concatenate(([0.],np.cumsum(arr)))
    peak=np.maximum.accumulate(eq)
    dd=eq-peak
    # Return post-trade equity series (excluding the starting 0) for callers
    # that expect len(eq)==len(pnls); max_dd still includes the start point.
    return eq[1:],float(eq[-1]),float(dd.min()) if len(dd) else 0.


def _finite_pnls(pnls):
    return [float(x) for x in pnls if x is not None and math.isfinite(float(x))]


def score_trade_pnls(pnls, *, store=None, model_version=None):
    """Net, max drawdown, stability (share of positive rolling windows).

    `model_version` is recorded on the score so callers never mix versions into
    one candidate number without noticing.
    """
    cfg=_cfg(store)
    min_oos=int(cfg['model_min_oos'])
    pnls=_finite_pnls(pnls)
    if len(pnls)<min_oos:
        return {'ok':False,'reason':f'need ≥{min_oos} OOS outcomes','n':len(pnls),
                'model_version':model_version}
    _,net,dd=_equity_curve(pnls)
    block=max(4,len(pnls)//5)
    pos=0;total=0
    for i in range(0,len(pnls)-block+1,block):
        total+=1
        if sum(pnls[i:i+block])>0:pos+=1
    stability=pos/max(total,1)
    return {'ok':True,'n':len(pnls),'net':net,'max_dd':dd,'stability':stability,
            'model_version':model_version}


def _row_time(row):
    if not isinstance(row,dict):return None
    for key in ('bar_time','at','closed_at','time'):
        v=row.get(key)
        if v:return str(v)
    return None


def _row_pnl(row, *, cash_key='actual_pnl'):
    if isinstance(row,(int,float)):
        return float(row) if math.isfinite(float(row)) else None
    if not isinstance(row,dict):return None
    for key in (cash_key,'paper_pnl','actual_pnl','pnl'):
        if key in row and row[key] is not None:
            try:
                v=float(row[key])
            except (TypeError,ValueError):
                continue
            if math.isfinite(v):return v
    return None


def _row_version(row):
    if not isinstance(row,dict):return None
    v=row.get('model_version')
    if v is None:return None
    try:return int(v)
    except (TypeError,ValueError):return None


def single_version_pnls(rows, *, cash_key='actual_pnl', model_version=None):
    """Keep one model_version's cash outcomes — never mix versions in a score.

    If `model_version` is None, use the latest version present in `rows`.
    Rows without a version are kept only when *no* row is version-tagged
    (legacy histories); otherwise they are dropped so mixed eras stay clean.
    """
    rows=list(rows or [])
    versions=sorted({v for v in (_row_version(r) for r in rows) if v is not None})
    if model_version is None and versions:
        model_version=versions[-1]
    out=[]
    for r in rows:
        ver=_row_version(r)
        if versions:
            if ver is None or int(ver)!=int(model_version):
                continue
        pnl=_row_pnl(r,cash_key=cash_key)
        if pnl is None:continue
        out.append({'pnl':pnl,'at':_row_time(r),'model_version':ver
                    if ver is not None else model_version,'row':r})
    return out,model_version


def aligned_window_pnls(incumbent_rows, candidate_rows, *, oos_frac=0.7,
                        inc_cash_key='actual_pnl', cand_cash_key='paper_pnl',
                        incumbent_version=None, candidate_version=None):
    """Same chronological OOS window + one model_version per side.

    Cuts both series to the overlapping time span after each side's OOS cut so
    shadow and LIVE are scored on comparable available data / cash risk.
    """
    inc,inc_ver=single_version_pnls(incumbent_rows,cash_key=inc_cash_key,
                                    model_version=incumbent_version)
    cand,cand_ver=single_version_pnls(candidate_rows,cash_key=cand_cash_key,
                                      model_version=candidate_version)
    oos_frac=float(oos_frac)
    if oos_frac<0:oos_frac=0.
    if oos_frac>1:oos_frac=1.
    inc_cut=int(len(inc)*(1.0-oos_frac))
    cand_cut=int(len(cand)*(1.0-oos_frac))
    inc_oos=inc[inc_cut:]
    cand_oos=cand[cand_cut:]
    # Align by shared time bounds when timestamps exist on both sides.
    inc_times=[x['at'] for x in inc_oos if x.get('at')]
    cand_times=[x['at'] for x in cand_oos if x.get('at')]
    if inc_times and cand_times:
        start=max(min(inc_times),min(cand_times))
        end=min(max(inc_times),max(cand_times))
        if start<=end:
            inc_oos=[x for x in inc_oos if not x.get('at') or start<=x['at']<=end]
            cand_oos=[x for x in cand_oos if not x.get('at') or start<=x['at']<=end]
    return {
        'incumbent_pnls':[x['pnl'] for x in inc_oos],
        'candidate_pnls':[x['pnl'] for x in cand_oos],
        'incumbent_version':inc_ver,
        'candidate_version':cand_ver,
        'window_start':(max(min(inc_times),min(cand_times))
                        if inc_times and cand_times else None),
        'window_end':(min(max(inc_times),max(cand_times))
                      if inc_times and cand_times else None),
        'incumbent_n_total':len(inc),'candidate_n_total':len(cand),
    }


def compare_models(incumbent_pnls, candidate_pnls, *, store=None,
                   incumbent_version=None, candidate_version=None):
    """Return promote decision with reasons — thresholds from self_params."""
    cfg=_cfg(store)
    dd_ratio=float(cfg['model_max_dd_ratio'])
    slack=float(cfg['model_stability_slack'])
    inc=score_trade_pnls(incumbent_pnls,store=store,model_version=incumbent_version)
    cand=score_trade_pnls(candidate_pnls,store=store,model_version=candidate_version)
    if not cand.get('ok'):
        return {'promote':False,'reason':cand.get('reason','candidate weak'),
                'incumbent':inc,'candidate':cand}
    if not inc.get('ok'):
        if cand['stability']<.4:
            return {'promote':False,'reason':'incumbent thin and candidate unstable',
                    'incumbent':inc,'candidate':cand}
        return {'promote':True,'reason':'cold-start candidate acceptable',
                'incumbent':inc,'candidate':cand}
    # Fair when trade counts differ (shadow vs LIVE often unequal): mean £/trade.
    inc_mean=inc['net']/max(1,inc['n'])
    cand_mean=cand['net']/max(1,cand['n'])
    better_net=cand['net']>inc['net']
    better_mean=cand_mean>inc_mean
    if not better_net and not better_mean:
        return {'promote':False,'reason':'candidate net/mean £ not better',
                'incumbent':inc,'candidate':cand,
                'inc_mean':inc_mean,'cand_mean':cand_mean}
    if cand['max_dd']<inc['max_dd']*dd_ratio and inc['max_dd']<0:
        return {'promote':False,'reason':'candidate drawdown worse than allowed',
                'incumbent':inc,'candidate':cand}
    if cand['stability']+1e-9<inc['stability']-slack:
        return {'promote':False,'reason':'candidate less stable',
                'incumbent':inc,'candidate':cand}
    why=('OOS mean £/trade beat incumbent' if better_mean and not better_net
         else 'OOS net/risk/stability beat incumbent')
    return {'promote':True,'reason':why,
            'incumbent':inc,'candidate':cand,
            'inc_mean':inc_mean,'cand_mean':cand_mean}


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
        decision=compare_models(incumbent_pnls,candidate_pnls,store=self.store,
                                incumbent_version=(meta or {}).get('incumbent_version'),
                                candidate_version=(meta or {}).get('candidate_version'))
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

    def rollback_target(self):
        """Snapshot to restore: prior head if stack≥2, else sole baseline with weights."""
        if len(self.stack)>=2:
            return self.stack[-2]
        if len(self.stack)==1:
            sole=self.stack[0]
            if sole.get('weights_path') or sole.get('approved_specs') is not None:
                return sole
        return None

    def mark_rollback(self, reason, *, restore_fn=None, target=None):
        """Flag rollback and restore neural weights + feature registry snapshot.

        restore_fn(version_meta) → bool should reload AdaptiveBrain weights and
        FeatureRegistry approved specs. `target` overrides previous_version()
        (used when only a sole baseline snapshot exists).
        """
        prev=target if target is not None else self.previous_version()
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
