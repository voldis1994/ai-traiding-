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


def _broker_pnls(history=None, store=None):
    hist=list(history or [])
    if not hist and store is not None:
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
    return pnls


def score_cash_holdout(store, *, min_trades=8, history=None):
    """Mean broker £ on chronological OOS of profit_learning (no weight replay).

    Used as a secondary gate signal: if recent cash is deeply negative, require
    a larger accuracy lift to KEEP. Does not depend on weight state.
    """
    pnls=_broker_pnls(history,store)
    if len(pnls)<int(min_trades):
        return {'ok':False,'n':len(pnls),'mean':None,'net':None,
                'trade_count':len(pnls)}
    cut=int(len(pnls)*0.7)
    oos=pnls[cut:] or pnls[-max(3,len(pnls)//4):]
    return {'ok':True,'n':len(oos),'mean':float(sum(oos)/len(oos)),
            'net':float(sum(oos)),'trade_count':len(pnls)}


def recent_cash_degradation(store=None, *, history=None, window=5,
                            min_trades=10, slack=0.05):
    """True when the last ``window`` broker £ trades are worse than the prior window.

    Classic failure: first ~5–8 trades OK, then results get worse and worse while
    minute-direction holdout still KEPT ΔW. This flags that cash regime.
    """
    pnls=_broker_pnls(history,store)
    need=max(int(min_trades),2*int(window))
    if len(pnls)<need:
        return {'ok':False,'degrading':False,'trade_count':len(pnls),
                'reason':'need_more_trades'}
    w=int(window)
    recent=pnls[-w:]
    prior=pnls[-2*w:-w]
    recent_net=float(sum(recent))
    prior_net=float(sum(prior))
    recent_mean=recent_net/w
    prior_mean=prior_net/w
    # Only flag when the recent window is a cash loss AND worse than prior.
    # Still-green soft landings (e.g. +2 → +1.5) must not freeze learning.
    degrading=bool(
        recent_mean < 0
        and recent_net < prior_net - float(slack))
    return {
        'ok':True,'degrading':bool(degrading),'trade_count':len(pnls),
        'window':w,'recent_net':recent_net,'prior_net':prior_net,
        'recent_mean':recent_mean,'prior_mean':prior_mean,
        'reason':('cash_degrading' if degrading else 'cash_stable'),
    }


# Live dump smoking gun: KEEP on −£ trades with *noise* minute-holdout lifts
# while cash OOS was already red. Still demand a real lift — but not so high
# that loss ΔW never sticks (neurons must improve on £ minuses).
LOSS_IN_RED_CASH_MIN_LIFT = 0.03


def decide_keep(score_before, score_after, *, cash=None, store=None,
                degradation=None, trade_pnl=None):
    """KEEP if holdout accuracy improved by margin (or cold-start).

    When recent broker £ is degrading after a warm-up of trades, refuse thin
    holdout KEEP and demand a clearer accuracy lift — stops “starts good then
    gets worse after ~10 trades” from rewriting early weights.

    When this closed trade itself lost £ and cash OOS mean is already negative,
    refuse small holdout lifts (loss-taught HOLD looked “accurate” on minutes).
    """
    cfg=_cfg(store)
    margin=float(cfg.get('margin') or 0.002)
    deg=degradation if isinstance(degradation,dict) else {}
    degrading=bool(deg.get('ok') and deg.get('degrading'))
    trade_count=int(
        (cash or {}).get('trade_count')
        or deg.get('trade_count')
        or 0)
    try:
        closed_pnl=float(trade_pnl) if trade_pnl is not None else None
        if closed_pnl is not None and not math.isfinite(closed_pnl):
            closed_pnl=None
    except (TypeError,ValueError):
        closed_pnl=None
    cash_mean_neg=(isinstance(cash,dict) and cash.get('ok')
                   and cash.get('mean') is not None
                   and float(cash['mean'])<0)

    if not score_after.get('ok'):
        # Cannot evaluate after — keep only if we also couldn't before (cold).
        if not score_before.get('ok'):
            # After enough live £ trades, thin forecast holdout must NOT rubber-stamp ΔW.
            if degrading:
                return {'keep':False,'reason':'thin_holdout_cash_degrading',
                        'before':score_before,'after':score_after,'cash':cash,
                        'degradation':deg,'trade_pnl':closed_pnl}
            if trade_count >= int(cfg.get('cash_min') or 8):
                return {'keep':False,'reason':'thin_holdout_refuse_after_warmup',
                        'before':score_before,'after':score_after,'cash':cash,
                        'degradation':deg,'trade_pnl':closed_pnl}
            # Loss + red cash OOS: never rubber-stamp thin holdout KEEP.
            if closed_pnl is not None and closed_pnl<0 and cash_mean_neg:
                return {'keep':False,'reason':'thin_holdout_loss_in_red_cash',
                        'before':score_before,'after':score_after,'cash':cash,
                        'degradation':deg,'trade_pnl':closed_pnl}
            return {'keep':True,'reason':'cold_start_thin_holdout',
                    'before':score_before,'after':score_after,'cash':cash,
                    'degradation':deg,'trade_pnl':closed_pnl}
        return {'keep':False,'reason':'after_holdout_unscored',
                'before':score_before,'after':score_after,'cash':cash,
                'degradation':deg,'trade_pnl':closed_pnl}
    if not score_before.get('ok'):
        # First time we have holdout — keep if accuracy not disastrous.
        acc=float(score_after.get('accuracy') or 0)
        if acc<0.28:
            return {'keep':False,'reason':'cold_start_accuracy_too_low',
                    'before':score_before,'after':score_after,'cash':cash,
                    'degradation':deg,'trade_pnl':closed_pnl}
        if degrading and acc < 0.40:
            return {'keep':False,'reason':'cold_start_degrading_low_acc',
                    'before':score_before,'after':score_after,'cash':cash,
                    'degradation':deg,'trade_pnl':closed_pnl}
        if closed_pnl is not None and closed_pnl<0 and cash_mean_neg and acc < 0.45:
            return {'keep':False,'reason':'cold_start_loss_in_red_cash',
                    'before':score_before,'after':score_after,'cash':cash,
                    'degradation':deg,'trade_pnl':closed_pnl}
        return {'keep':True,'reason':'cold_start_first_holdout',
                'before':score_before,'after':score_after,'cash':cash,
                'degradation':deg,'trade_pnl':closed_pnl}
    before=float(score_before['accuracy'])
    after=float(score_after['accuracy'])
    need=margin
    # If recent cash OOS is bad, demand a clearer accuracy lift.
    if cash_mean_neg:
        need=margin+abs(float(cfg.get('cash_weight') or 0.35))*0.01
    # Rolling £ getting worse → need a real holdout win, not noise.
    if degrading:
        need=max(need,margin*3.0)
    # This trade lost £ while OOS cash already red → tiny lifts must not KEEP.
    loss_in_red=(closed_pnl is not None and closed_pnl<0 and cash_mean_neg)
    if loss_in_red:
        need=max(need,float(LOSS_IN_RED_CASH_MIN_LIFT))
    lift=after-before
    if lift>=need:
        return {'keep':True,'reason':f'holdout_acc +{lift:.4f} ≥ {need:.4f}',
                'before':score_before,'after':score_after,'cash':cash,
                'degradation':deg,'trade_pnl':closed_pnl,'lift':lift}
    if loss_in_red:
        reason=f'loss_in_red_cash_lift {lift:.4f} < {need:.4f}'
    elif degrading:
        reason=f'cash_degrading_lift {lift:.4f} < {need:.4f}'
    else:
        reason=f'holdout_acc lift {lift:.4f} < {need:.4f}'
    return {'keep':False,'reason':reason,
            'before':score_before,'after':score_after,'cash':cash,
            'degradation':deg,'trade_pnl':closed_pnl,'lift':lift}


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


def should_skip_teach(store=None, *, history=None):
    """Skip teach entirely when recent £ is clearly degrading.

    Avoids writing ΔW that thin holdout would rubber-stamp KEEP — classic
    “first trades OK, then worse after ~10” path.
    """
    deg=recent_cash_degradation(store,history=history)
    if deg.get('ok') and deg.get('degrading'):
        return {'skip':True,'reason':'cash_degrading_skip_teach','degradation':deg}
    return {'skip':False,'reason':'ok','degradation':deg}


def gate_closed_trade_learn(engine, learn_fn, *, epic=None, deal_id=None,
                            trade_pnl=None):
    """Snapshot → score_before → learn_fn() → score_after → keep or rollback.

    ``learn_fn`` should perform the weight updates and return
    ``{'any_real_dw': bool, ...}``.
    ``trade_pnl`` is the closed-trade broker £ (when known) — used to refuse
    KEEP on losses while cash OOS is already red.
    """
    store=engine.store
    brain=engine.brain
    cfg=_cfg(store)
    if not cfg.get('enabled'):
        result=learn_fn()
        return {'keep':True,'reason':'learn_gate_disabled','learn':result,
                'rolled_back':False,'skipped':False}
    pl_hist=None
    try:
        pl_hist=list(getattr(getattr(engine,'profit_learning',None),'history',[]) or [])
    except Exception:
        pl_hist=None
    cash=score_cash_holdout(store,min_trades=int(cfg.get('cash_min') or 8),
                            history=pl_hist)
    deg=recent_cash_degradation(store,history=pl_hist)
    skip=should_skip_teach(store,history=pl_hist)
    if skip.get('skip'):
        decision={
            'keep':False,'reason':skip.get('reason') or 'cash_degrading_skip_teach',
            'rolled_back':False,'skipped':True,'cash':cash,'degradation':deg,
            'deal_id':deal_id,'trade_pnl':trade_pnl,
            'learn':{'any_real_dw':False,'skipped':True},
        }
        store.set(STORE_LAST,decision)
        store.event('learning','Closed-trade teach SKIPPED (cash degrading)',
                    {'deal_id':deal_id,'reason':decision.get('reason'),
                     'recent_net':deg.get('recent_net'),
                     'prior_net':deg.get('prior_net'),
                     'trade_count':deg.get('trade_count')})
        return decision
    pre=brain._core_snapshot()
    rows=_holdout_feature_rows(store,epic=epic or getattr(brain,'epic',None))
    before=score_feature_holdout(brain,rows,restore=True)
    learn_result=learn_fn()
    any_dw=bool((learn_result or {}).get('any_real_dw'))
    if not any_dw:
        decision={'keep':True,'reason':'no_real_delta_w','before':before,
                  'after':before,'cash':cash,'degradation':deg,
                  'trade_pnl':trade_pnl,
                  'learn':learn_result,'rolled_back':False,'skipped':False}
        store.set(STORE_LAST,decision)
        return decision
    after=score_feature_holdout(brain,rows,restore=True)
    decision=decide_keep(before,after,cash=cash,store=store,degradation=deg,
                         trade_pnl=trade_pnl)
    decision['learn']=learn_result
    decision['skipped']=False
    decision['degradation']=deg
    if decision.get('keep'):
        decision['rolled_back']=False
        store.set(STORE_LAST,decision)
        store.event('learning','Closed-trade ΔW KEPT by learn_gate',
                    {'deal_id':deal_id,'reason':decision.get('reason'),
                     'before_acc':(before or {}).get('accuracy'),
                     'after_acc':(after or {}).get('accuracy'),
                     'lift':decision.get('lift'),
                     'trade_pnl':trade_pnl})
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
                 'lift':decision.get('lift'),
                 'degrading':bool(deg.get('degrading')),
                 'trade_pnl':trade_pnl})
    return decision
