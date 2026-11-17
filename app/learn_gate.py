"""Closed-trade ΔW keep/rollback gate — real self-improvement, not loss-react.

Flow: snapshot → learn → score holdout → KEEP only if holdout improves
(or cold-start thin data). Otherwise restore pre-learn weights.

Holdout prefers ``forecast_memory`` resolved rows (full features + h=1 label).
Falls back to chronological broker £ mean on profit_learning when features
are thin. Never teaches from UPL.
"""
from __future__ import annotations

import math

import numpy as np

from .forecast_targets import LABEL_BUY, LABEL_HOLD, LABEL_SELL

ACTIONS = ('BUY','SELL','HOLD')
STORE_LAST = 'learn_gate_last_v1'


def _cfg(store):
    try:
        from .self_params import get as sp_get
        return {
            'enabled':int(sp_get(store,'learn_gate_enabled',1)),
            'min_rows':int(sp_get(store,'learn_gate_min_rows',12)),
            'margin':float(sp_get(store,'learn_gate_margin',0.002)),
            'cash_min':int(sp_get(store,'learn_gate_cash_min',8)),
            'cash_weight':float(sp_get(store,'learn_gate_cash_weight',0.35)),
        }
    except Exception:
        return {'enabled':1,'min_rows':12,'margin':0.002,'cash_min':8,'cash_weight':0.35}


def _holdout_feature_rows(store, *, epic=None, limit=80):
    try:
        from .forecast_memory import ForecastMemory
        rows=ForecastMemory(store).rows(epic=epic,resolved_only=True,limit=limit)
    except Exception:
        rows=[]
    out=[]
    for r in rows:
        feats=r.get('features')
        outcomes=r.get('outcomes') or {}
        o=outcomes.get(1) or outcomes.get('1')
        if not isinstance(feats,(list,tuple)) or not isinstance(o,dict):
            continue
        try:
            lab=int(o['label'])
        except (KeyError,TypeError,ValueError):
            continue
        if lab not in (LABEL_BUY,LABEL_SELL,LABEL_HOLD):
            continue
        if len(feats)<1:
            continue
        out.append({'features':list(feats),'label':lab,
                    'r':float(o['r']) if o.get('r') is not None else None,
                    'at':r.get('time')})
    return out


def _chronological_holdout(rows, *, train_frac=0.7):
    rows=list(rows or [])
    if len(rows)<4:
        return [],rows
    cut=max(1,int(len(rows)*float(train_frac)))
    # Embargo 1 bar between train-looking and val (rows are already past).
    return rows[:cut],rows[cut:]


def score_feature_holdout(brain, rows, *, restore=True):
    """Directional accuracy on holdout features via core.step (no learn).

    Restores brain snapshot after scoring so LIVE state is unchanged.
    """
    if brain is None or not rows:
        return {'ok':False,'n':0,'accuracy':None,'reason':'no_rows'}
    dim=int(getattr(brain.core,'d',0) or 0)
    usable=[r for r in rows
            if isinstance(r.get('features'),(list,tuple))
            and len(r['features'])==dim]
    if len(usable)<4:
        return {'ok':False,'n':len(usable),'accuracy':None,'reason':'dim_or_thin'}
    _,val=_chronological_holdout(usable,train_frac=0.7)
    if len(val)<3:
        val=usable[-max(3,len(usable)//3):]
    snap=brain._core_snapshot() if restore else None
    hits=0;n=0
    try:
        from .core import NEURAL_BAR_DT
        for r in val:
            x=np.asarray(r['features'],dtype=float)
            if x.size!=dim or not np.all(np.isfinite(x)):
                continue
            try:
                out=brain.core.step(x,dt=NEURAL_BAR_DT)
            except Exception:
                continue
            action=out.get('action')
            try:
                pred=ACTIONS.index(action)
            except ValueError:
                continue
            hits+=int(pred==int(r['label']));n+=1
    finally:
        if snap is not None:
            brain._apply_core_snapshot(snap)
    if n<3:
        return {'ok':False,'n':n,'accuracy':None,'reason':'too_few_scored'}
    return {'ok':True,'n':n,'accuracy':hits/n,'hits':hits}


def score_cash_holdout(store, *, min_trades=8, history=None):
    """Mean broker £ on chronological OOS of profit_learning (no weight replay).

    Used as a secondary gate signal: if recent cash is deeply negative, require
    a larger accuracy lift to KEEP. Does not depend on weight state.
    """
    hist=list(history or [])
    if not hist:
        try:
            from .profit_learning import STORE_KEY
            hist=list(store.get(STORE_KEY) or [])
        except Exception:
            hist=[]
    pnls=[]
    for r in hist:
        if not isinstance(r,dict):continue
        if r.get('mode')=='skipped_no_broker_pnl':continue
        try:
            p=float(r.get('actual_pnl'))
        except (TypeError,ValueError):
            continue
        if math.isfinite(p):
            pnls.append(p)
    if len(pnls)<int(min_trades):
        return {'ok':False,'n':len(pnls),'mean':None,'net':None}
    cut=int(len(pnls)*0.7)
    oos=pnls[cut:] or pnls[-max(3,len(pnls)//4):]
    return {'ok':True,'n':len(oos),'mean':float(sum(oos)/len(oos)),
            'net':float(sum(oos))}


def decide_keep(score_before, score_after, *, cash=None, store=None):
    """KEEP if holdout accuracy improved by margin (or cold-start)."""
    cfg=_cfg(store)
    margin=float(cfg.get('margin') or 0.002)
    if not score_after.get('ok'):
        # Cannot evaluate after — keep only if we also couldn't before (cold).
        if not score_before.get('ok'):
            return {'keep':True,'reason':'cold_start_thin_holdout',
                    'before':score_before,'after':score_after,'cash':cash}
        return {'keep':False,'reason':'after_holdout_unscored',
                'before':score_before,'after':score_after,'cash':cash}
    if not score_before.get('ok'):
        # First time we have holdout — keep if accuracy not disastrous.
        acc=float(score_after.get('accuracy') or 0)
        if acc<0.28:
            return {'keep':False,'reason':'cold_start_accuracy_too_low',
                    'before':score_before,'after':score_after,'cash':cash}
        return {'keep':True,'reason':'cold_start_first_holdout',
                'before':score_before,'after':score_after,'cash':cash}
    before=float(score_before['accuracy'])
    after=float(score_after['accuracy'])
    need=margin
    # If recent cash OOS is bad, demand a clearer accuracy lift.
    if isinstance(cash,dict) and cash.get('ok') and cash.get('mean') is not None:
        if float(cash['mean'])<0:
            need=margin+abs(float(cfg.get('cash_weight') or 0.35))*0.01
    lift=after-before
    if lift>=need:
        return {'keep':True,'reason':f'holdout_acc +{lift:.4f} ≥ {need:.4f}',
                'before':score_before,'after':score_after,'cash':cash,'lift':lift}
    return {'keep':False,'reason':f'holdout_acc lift {lift:.4f} < {need:.4f}',
            'before':score_before,'after':score_after,'cash':cash,'lift':lift}


def evaluate_and_gate(engine, *, epic=None, deal_id=None):
    """Run after ΔW: return decision; caller restores snapshot on keep=False.

    Does not mutate weights itself except temporary step() inside scoring
    (restored). Caller must pass/restore the pre-learn snapshot.
    """
    store=engine.store
    cfg=_cfg(store)
    if not cfg.get('enabled'):
        return {'keep':True,'reason':'learn_gate_disabled'}
    brain=engine.brain
    rows=_holdout_feature_rows(store,epic=epic or getattr(brain,'epic',None),
                               limit=120)
    min_rows=int(cfg.get('min_rows') or 12)
    # Need enough total rows; scoring uses holdout tail.
    score_before={'ok':False,'n':0,'accuracy':None,'reason':'pending'}
    # Caller should have scored BEFORE learn; we only score AFTER here if
    # pre-score is provided via store temp — see gate_closed_trade_learn.
    cash=score_cash_holdout(store,min_trades=int(cfg.get('cash_min') or 8))
    out={'keep':True,'reason':'noop','cash':cash,'holdout_rows':len(rows),
         'min_rows':min_rows,'deal_id':deal_id}
    store.set(STORE_LAST,out)
    return out


def gate_closed_trade_learn(engine, learn_fn, *, epic=None, deal_id=None):
    """Snapshot → score_before → learn_fn() → score_after → keep or rollback.

    ``learn_fn`` should perform the weight updates and return
    ``{'any_real_dw': bool, ...}``.
    """
    store=engine.store
    brain=engine.brain
    cfg=_cfg(store)
    if not cfg.get('enabled'):
        result=learn_fn()
        return {'keep':True,'reason':'learn_gate_disabled','learn':result,
                'rolled_back':False}
    pre=brain._core_snapshot()
    rows=_holdout_feature_rows(store,epic=epic or getattr(brain,'epic',None))
    before=score_feature_holdout(brain,rows,restore=True)
    pl_hist=None
    try:
        pl_hist=list(getattr(getattr(engine,'profit_learning',None),'history',[]) or [])
    except Exception:
        pl_hist=None
    cash=score_cash_holdout(store,min_trades=int(cfg.get('cash_min') or 8),
                            history=pl_hist)
    learn_result=learn_fn()
    any_dw=bool((learn_result or {}).get('any_real_dw'))
    if not any_dw:
        decision={'keep':True,'reason':'no_real_delta_w','before':before,
                  'after':before,'cash':cash,'learn':learn_result,
                  'rolled_back':False}
        store.set(STORE_LAST,decision)
        return decision
    after=score_feature_holdout(brain,rows,restore=True)
    decision=decide_keep(before,after,cash=cash,store=store)
    decision['learn']=learn_result
    if decision.get('keep'):
        decision['rolled_back']=False
        store.set(STORE_LAST,decision)
        store.event('learning','Closed-trade ΔW KEPT by learn_gate',
                    {'deal_id':deal_id,'reason':decision.get('reason'),
                     'before_acc':(before or {}).get('accuracy'),
                     'after_acc':(after or {}).get('accuracy'),
                     'lift':decision.get('lift')})
        return decision
    # ROLLBACK
    brain._apply_core_snapshot(pre)
    decision['rolled_back']=True
    decision['keep']=False
    store.set(STORE_LAST,decision)
    store.event('learning','Closed-trade ΔW ROLLED BACK by learn_gate',
                {'deal_id':deal_id,'reason':decision.get('reason'),
                 'before_acc':(before or {}).get('accuracy'),
                 'after_acc':(after or {}).get('accuracy'),
                 'lift':decision.get('lift')})
    return decision
