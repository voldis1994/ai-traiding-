"""Softmax confidence ≠ calibrated probability until checked on holdout.

If the model says HOLD @ 0.80, historically HOLD should occur ~80% of the
time in that confidence bin. ECE / reliability curves make the gap visible.
"""
from __future__ import annotations

import math

import numpy as np

from .forecast_targets import LABEL_BUY, LABEL_HOLD, LABEL_SELL

ACTIONS = ('BUY','SELL','HOLD')
LABEL_TO_ACTION = {LABEL_BUY:'BUY',LABEL_SELL:'SELL',LABEL_HOLD:'HOLD'}


def _as_prob_vector(scores):
    if not isinstance(scores,dict):
        return None
    try:
        v=np.array([float(scores.get(a,0.)) for a in ACTIONS],dtype=float)
    except (TypeError,ValueError):
        return None
    if not np.all(np.isfinite(v)) or v.sum()<=0:
        return None
    return v/v.sum()


def reliability_bins(probs, labels, *, n_bins=10, action=None):
    """Bin max-confidence (or one action's p) vs hit rate.

    ``probs``: list of score dicts or length-3 arrays.
    ``labels``: list of 0/1/2 or BUY/SELL/HOLD.
    """
    confs=[];hits=[]
    for p,lab in zip(probs or [],labels or []):
        vec=_as_prob_vector(p) if isinstance(p,dict) else (
            np.asarray(p,dtype=float) if p is not None else None)
        if vec is None or vec.size!=3:
            continue
        if isinstance(lab,str):
            lab_i={'BUY':0,'SELL':1,'HOLD':2}.get(lab.upper())
        else:
            try:
                lab_i=int(lab)
            except (TypeError,ValueError):
                lab_i=None
        if lab_i not in (0,1,2):
            continue
        if action is None:
            conf=float(vec.max());pred=int(vec.argmax())
            hit=1.0 if pred==lab_i else 0.0
        else:
            ai=ACTIONS.index(action) if action in ACTIONS else None
            if ai is None:continue
            conf=float(vec[ai]);hit=1.0 if lab_i==ai else 0.0
        confs.append(conf);hits.append(hit)
    if not confs:
        return []
    confs=np.asarray(confs);hits=np.asarray(hits)
    edges=np.linspace(0,1,int(n_bins)+1)
    bins=[]
    for i in range(int(n_bins)):
        lo,hi=edges[i],edges[i+1]
        m=(confs>=lo)&(confs<(hi if i<n_bins-1 else hi+1e-12))
        if not m.any():
            bins.append({'lo':float(lo),'hi':float(hi),'n':0,
                         'confidence_conf':None,'hit_rate':None,'gap':None})
            continue
        mean_c=float(confs[m].mean());hit_r=float(hits[m].mean())
        bins.append({'lo':float(lo),'hi':float(hi),'n':int(m.sum()),
                     'mean_conf':mean_c,'hit_rate':hit_r,
                     'gap':abs(mean_c-hit_r)})
    return bins


def expected_calibration_error(probs, labels, *, n_bins=10):
    """ECE on max-probability predictions (lower is better; 0 = perfect)."""
    bins=reliability_bins(probs,labels,n_bins=n_bins,action=None)
    total=sum(b['n'] for b in bins)
    if total<=0:
        return None
    ece=0.0
    for b in bins:
        if b['n']<=0 or b['gap'] is None:
            continue
        ece+=(b['n']/total)*b['gap']
    return float(ece)


def action_frequency(labels):
    counts={a:0 for a in ACTIONS};n=0
    for lab in labels or []:
        if isinstance(lab,str):
            a=lab.upper()
        else:
            a=LABEL_TO_ACTION.get(int(lab) if lab is not None else None)
        if a not in counts:
            continue
        counts[a]+=1;n+=1
    if n<=0:
        return {a:None for a in ACTIONS}
    return {a:counts[a]/n for a in ACTIONS}


def summarize_calibration(probs, labels, *, n_bins=10):
    ece=expected_calibration_error(probs,labels,n_bins=n_bins)
    bins=reliability_bins(probs,labels,n_bins=n_bins)
    return {
        'ece':ece,
        'n':sum(b['n'] for b in bins),
        'bins':bins,
        'label_freq':action_frequency(labels),
        'note':('Softmax max-p is uncalibrated until ECE is near 0 on holdout; '
                'do not treat confidence as profit probability.'),
    }
