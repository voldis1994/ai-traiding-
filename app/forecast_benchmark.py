"""Chronological holdout: existing neural softmax vs multi-horizon forecast head.

Compares forecasting quality only — no new indicators, trade limits, or
execution rules. Same rows, time-ordered split.
"""
from __future__ import annotations

import math

import numpy as np

from .forecast_calibration import expected_calibration_error, summarize_calibration
from .forecast_model import MultiHorizonForecast
from .forecast_targets import HORIZONS, LABEL_BUY, LABEL_HOLD, LABEL_SELL

ACTIONS = ('BUY','SELL','HOLD')
LABEL_TO_ACTION = {LABEL_BUY:'BUY',LABEL_SELL:'SELL',LABEL_HOLD:'HOLD'}


def _chrono_split(rows, *, train_frac=0.7):
    rows=list(rows or [])
    if len(rows)<12:
        return [],[]
    cut=max(1,int(len(rows)*float(train_frac)))
    # Leave a tiny embargo when enough data (avoid leakage at the cut).
    embargo=1 if len(rows)>=20 else 0
    train=rows[:cut]
    val=rows[cut+embargo:]
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


def run_benchmark(rows, *, horizons=HORIZONS, train_frac=0.7, seed=0):
    """Fit MultiHorizonForecast on train; score vs neural argmax on validation."""
    train,val=_chrono_split(rows,train_frac=train_frac)
    if len(train)<8 or len(val)<4:
        return {
            'ok':False,
            'reason':f'need more resolved rows (train={len(train)}, val={len(val)})',
            'train_n':len(train),'val_n':len(val),
        }
    dim=len(train[0].get('features') or [])
    if dim<1:
        return {'ok':False,'reason':'empty features','train_n':len(train),'val_n':len(val)}
    model=MultiHorizonForecast(dim=dim,horizons=tuple(horizons),seed=seed)
    fit_info=model.fit(train,horizons=horizons)
    if not fit_info.get('fitted'):
        return {'ok':False,'reason':fit_info.get('reason'),**fit_info}

    per_h={}
    for h in horizons:
        base_preds=[];new_preds=[];labels=[]
        base_probs=[];new_probs=[]
        r_hats=[];r_true=[]
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
        base_acc,n=_accuracy(base_preds,labels)
        new_acc,_=_accuracy(new_preds,labels)
        mae,n_r=_mae(r_hats,r_true)
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
        }
    # Primary report horizon = 1 (matches LIVE minute teacher).
    cal=summarize_calibration(
        [r.get('scores') for r in val],
        [_label_action(r.get('outcomes'),1) for r in val])
    return {
        'ok':True,
        'train_n':len(train),
        'val_n':len(val),
        'dim':dim,
        'horizons':list(horizons),
        'fit':fit_info,
        'by_horizon':per_h,
        'baseline_calibration_h1':cal,
        'note':('Forecast quality only — LIVE entry still NeuralCore + policy gate; '
                'no new execution rules.'),
    }
