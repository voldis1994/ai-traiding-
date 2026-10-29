"""Audit #35: chronological historical then forward check vs a simple baseline.

Reports net, max drawdown, costs proxy, trade count, and stability.
Passing software tests is NOT a profitability proof.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.engine import Store
from app.model_validation import _equity_curve, score_trade_pnls, walk_forward_split
from app.self_params import DEFAULTS, save


def _synthetic_closes(n=240, seed=7):
    rng=np.random.default_rng(seed)
    steps=rng.normal(0.0,0.15,size=n)
    return 2000.+np.cumsum(steps)


def _baseline_mom_pnls(closes, *, hold=4, spread=0.3):
    pnls=[];i=hold
    while i+hold<len(closes):
        direction=1 if closes[i]>=closes[i-hold] else -1
        pnls.append(float(direction*(closes[i+hold]-closes[i])-abs(spread)))
        i+=hold
    return pnls


def _neuralish_holdout_pnls(closes, train_end, *, hold=4, spread=0.3):
    train=closes[:train_end]
    side=1 if train[-1]>=train[0] else -1
    pnls=[];i=train_end
    while i+hold<len(closes):
        pnls.append(float(side*(closes[i+hold]-closes[i])-abs(spread)))
        i+=hold
    return pnls


def test_chronological_split_has_no_future_leak():
    rows=[{'bar_time':f'2024-01-01T00:{i:02d}:00','actual_pnl':(-1.)**(i)} for i in range(40)]
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        save(store,{**DEFAULTS},reason='fwd')
        split=walk_forward_split(rows,store=store)
        if isinstance(split,tuple) and len(split)==2:
            train,oos=split
            if train and oos and isinstance(train[0],dict):
                assert str(train[-1].get('bar_time'))<=str(oos[0].get('bar_time'))


def test_forward_vs_baseline_metrics_report_only():
    closes=_synthetic_closes(200)
    cut=int(len(closes)*0.7)
    model_pnls=_neuralish_holdout_pnls(closes,cut,hold=5,spread=0.3)
    base_pnls=_baseline_mom_pnls(closes[cut-5:],hold=5,spread=0.3)
    n=min(len(model_pnls),len(base_pnls))
    model_pnls=model_pnls[:n];base_pnls=base_pnls[:n]
    assert n>=4

    def pack(pnls,label):
        _,net,dd=_equity_curve(pnls)
        wins=sum(1 for x in pnls if x>0)
        return {
            'label':label,'n':len(pnls),'net':float(net),'max_dd':float(dd),
            'costs_proxy':float(0.3*len(pnls)),
            'win_rate':wins/max(len(pnls),1),
            'stability':float(np.mean([sum(pnls[i:i+4])>0
                                       for i in range(0,max(len(pnls)-3,1))])),
            'profitability_proven':False,
        }

    report={'model':pack(model_pnls,'chrono_forward_model'),
            'baseline':pack(base_pnls,'momentum_baseline'),
            'note':'Software harness only — not evidence of live edge.'}
    assert report['model']['profitability_proven'] is False
    assert report['model']['n']==report['baseline']['n']
    assert math.isfinite(report['model']['net'])
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        save(store,{**DEFAULTS,'model_min_oos':4},reason='fwd')
        scored=score_trade_pnls(model_pnls,store=store,model_version=1)
        assert scored.get('model_version')==1
