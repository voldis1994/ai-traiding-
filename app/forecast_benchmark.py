"""Chronological holdout: existing neural softmax vs multi-horizon forecast head.

Compares forecasting quality only — no new indicators, trade limits, or
execution rules. Same rows, time-ordered split with horizon embargo so
train labels cannot leak into the validation window.
"""
from __future__ import annotations

import math

import numpy as np

from .forecast_calibration import expected_calibration_error, summarize_calibration
from .forecast_model import MultiHorizonForecast
from .forecast_targets import HORIZONS, LABEL_BUY, LABEL_HOLD, LABEL_SELL, bar_stamp

ACTIONS = ('BUY','SELL','HOLD')
LABEL_TO_ACTION = {LABEL_BUY:'BUY',LABEL_SELL:'SELL',LABEL_HOLD:'HOLD'}


def chrono_split(rows, *, train_frac=0.7, embargo=None, horizons=HORIZONS):
    """Time-ordered train/val with embargo ≥ max forecast horizon.

    Without embargo, a train row at t uses y_{t+15} that overlaps features of
    early validation rows — classic leakage. Embargo drops the gap bars.
    """
    rows=sorted(
        list(rows or []),
        key=lambda r:(str(bar_stamp(r.get('time')) or ''),int(r.get('id') or 0)))
    if len(rows)<12:
        return [],[],{'embargo':0,'cut':0}
    max_h=max(int(h) for h in (horizons or HORIZONS))
    gap=int(embargo) if embargo is not None else max_h
    gap=max(gap,max_h)
    cut=max(1,int(len(rows)*float(train_frac)))
    # Ensure val still has room after embargo.
    if cut+gap>=len(rows):
        cut=max(1,len(rows)-gap-4)
    train=rows[:cut]
    val=rows[cut+gap:]
    return train,val,{'embargo':gap,'cut':cut,'max_horizon':max_h}


# Back-compat alias
def _chrono_split(rows, *, train_frac=0.7, embargo=None, horizons=HORIZONS):
    train,val,_meta=chrono_split(
        rows,train_frac=train_frac,embargo=embargo,horizons=horizons)
    return train,val


def _baseline_action(scores):
    if not isinstance(scores,dict):
        return 'HOLD'
    try:
        return max(('BUY','SELL','HOLD'),key=lambda a:float(scores.get(a,0)))
    except (TypeError,ValueError):
        return 'HOLD'


def _label_action(outcomes, horizon):
    o=(outcomes or {}).get(horizon) or (outcomes or {}).get(str(horizon))
    if not isinstance(o,dict):
        return None
    try:
        lab=int(o['label'])
    except (KeyError,TypeError,ValueError):
        return None
    return LABEL_TO_ACTION.get(lab)


def _accuracy(preds, labels):
    pairs=[(p,l) for p,l in zip(preds,labels) if p in ACTIONS and l in ACTIONS]
    if not pairs:
        return None,0
    hit=sum(1 for p,l in pairs if p==l)
    return hit/len(pairs),len(pairs)


def _mae(preds, actuals):
    xs=[(float(p),float(a)) for p,a in zip(preds,actuals)
        if p is not None and a is not None
        and math.isfinite(float(p)) and math.isfinite(float(a))]
    if not xs:
        return None,0
    return float(np.mean([abs(p-a) for p,a in xs])),len(xs)


def sigma_error_diagnostics(r_hats, r_true, sigmas):
    """Check whether σ̂ matches the scale of |r − r̂| on holdout.

    Returns mean |err|, mean σ̂, ratio, and 1σ coverage (Gaussian ≈ 0.68).
    """
    triples=[]
    for rh,rt,sg in zip(r_hats or [],r_true or [],sigmas or []):
        try:
            e=abs(float(rh)-float(rt));s=float(sg)
        except (TypeError,ValueError):
            continue
        if not (math.isfinite(e) and math.isfinite(s)) or s<=0:
            continue
        triples.append((e,s))
    if len(triples)<3:
        return {'n':len(triples),'mean_abs_err':None,'mean_sigma':None,
                'err_over_sigma':None,'coverage_1sigma':None}
    errs=np.array([e for e,_ in triples],dtype=float)
    sigs=np.array([s for _,s in triples],dtype=float)
    mean_e=float(errs.mean());mean_s=float(sigs.mean())
    return {
        'n':len(triples),
        'mean_abs_err':mean_e,
        'mean_sigma':mean_s,
        'err_over_sigma':(mean_e/mean_s if mean_s>0 else None),
        'coverage_1sigma':float(np.mean(errs<=sigs)),
        'rmse':float(np.sqrt(np.mean(errs**2))),
    }


def run_benchmark(rows, *, horizons=HORIZONS, train_frac=0.7, seed=0,
                  embargo=None, epic=None):
    """Fit MultiHorizonForecast on train; score vs neural argmax on validation."""
    if epic:
        rows=[r for r in (rows or []) if str(r.get('epic') or '').upper()==str(epic).upper()]
    train,val,split_meta=chrono_split(
        rows,train_frac=train_frac,embargo=embargo,horizons=horizons)
    if len(train)<8 or len(val)<4:
        return {
            'ok':False,
            'reason':f'need more resolved rows (train={len(train)}, val={len(val)}, '
                     f'embargo={split_meta.get("embargo")})',
            'train_n':len(train),'val_n':len(val),
            'split':split_meta,
            'epic':epic,
        }
    dim=len(train[0].get('features') or [])
    if dim<1:
        return {'ok':False,'reason':'empty features','train_n':len(train),
                'val_n':len(val),'split':split_meta,'epic':epic}
    model=MultiHorizonForecast(dim=dim,horizons=tuple(horizons),seed=seed)
    fit_info=model.fit(train,horizons=horizons)
    if not fit_info.get('fitted'):
        return {'ok':False,'reason':fit_info.get('reason'),**fit_info,
                'split':split_meta,'epic':epic}

    per_h={}
    for h in horizons:
        base_preds=[];new_preds=[];labels=[]
        base_probs=[];new_probs=[]
        r_hats=[];r_true=[];sigmas=[]
        for row in val:
            lab=_label_action(row.get('outcomes'),h)
            if lab is None:
                continue
            scores=row.get('scores') or {}
            feats=row.get('features')
            if not feats or len(feats)!=dim:
                continue
            base_preds.append(_baseline_action(scores))
            base_probs.append(scores)
            action,p=model.action_at(feats,horizon=h)
            new_preds.append(action);new_probs.append(p);labels.append(lab)
            pred=model.predict_dict(feats).get(h)
            o=(row.get('outcomes') or {}).get(h) or {}
            if pred is not None and o.get('r') is not None:
                r_hats.append(pred.r_hat);r_true.append(float(o['r']))
                sigmas.append(pred.sigma_hat)
        base_acc,n=_accuracy(base_preds,labels)
        new_acc,_=_accuracy(new_preds,labels)
        mae,n_r=_mae(r_hats,r_true)
        sigma_diag=sigma_error_diagnostics(r_hats,r_true,sigmas)
        per_h[int(h)]={
            'n':n,
            'baseline_argmax_acc':base_acc,
            'forecast_model_acc':new_acc,
            'r_mae':mae,
            'r_n':n_r,
            'baseline_ece':expected_calibration_error(base_probs,labels),
            'forecast_ece':expected_calibration_error(new_probs,labels),
            'delta_acc':(None if base_acc is None or new_acc is None
                         else new_acc-base_acc),
            'sigma_vs_error':sigma_diag,
        }
    cal=summarize_calibration(
        [r.get('scores') for r in val],
        [_label_action(r.get('outcomes'),1) for r in val])
    # Primary focus: 1-minute horizon (LIVE neural teacher cadence).
    primary=per_h.get(1) or {}
    return {
        'ok':True,
        'train_n':len(train),
        'val_n':len(val),
        'dim':dim,
        'epic':epic,
        'horizons':list(horizons),
        'split':split_meta,
        'fit':fit_info,
        'by_horizon':per_h,
        'primary_horizon':1,
        'primary':primary,
        'baseline_calibration_h1':cal,
        'note':('Forecast quality only — LIVE entry still NeuralCore + policy gate; '
                'embargo ≥ max horizon; σ̂ checked vs |r−r̂| on holdout (esp. h=1).'),
    }


def run_gold_benchmark(store_or_rows, *, train_frac=0.7, seed=0, limit=5000):
    """Train + compare on the same GOLD forecast_memory rows (or an explicit list).

    Emphasizes 1-minute accuracy, class ECE, and σ̂ vs error magnitude.
    """
    if isinstance(store_or_rows,list):
        rows=[r for r in store_or_rows
              if str(r.get('epic') or 'GOLD').upper()=='GOLD']
    else:
        from .forecast_memory import ForecastMemory
        rows=ForecastMemory(store_or_rows).rows(
            epic='GOLD',resolved_only=True,limit=limit)
    report=run_benchmark(
        rows,horizons=HORIZONS,train_frac=train_frac,seed=seed,
        embargo=max(HORIZONS),epic='GOLD')
    if report.get('ok'):
        h1=report.get('by_horizon',{}).get(1) or {}
        report['gold_h1']={
            'accuracy':h1.get('forecast_model_acc'),
            'baseline_accuracy':h1.get('baseline_argmax_acc'),
            'r_mae':h1.get('r_mae'),
            'forecast_ece':h1.get('forecast_ece'),
            'sigma_vs_error':h1.get('sigma_vs_error'),
        }
    return report
