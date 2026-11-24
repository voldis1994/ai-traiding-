"""Bridge: forecast quality ledger → soft LIVE signal nudge (NeuralCore stays proposer).

Does NOT replace LIVE entry. After enough resolved GOLD/M1 rows, fit a
MultiHorizonForecast head offline-style, store shadow advice, and optionally
tilt scores the same way ``structure_edge`` does — capped, renormalized.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .forecast_memory import ForecastMemory, RESOLVED_OK
from .forecast_model import MultiHorizonForecast
from .forecast_targets import HORIZONS, bar_stamp, candle_bar_time, completed_candles

STORE_SHADOW = 'forecast_shadow_last_v1'
STORE_FIT_META = 'forecast_bridge_fit_v1'
STORE_WEIGHTS = 'forecast_bridge_weights_v1'


def merge_candle_lists(*lists, now_utc=None):
    """Dedupe by minute stamp; keep completed bars only (forming minute out)."""
    by={}
    for lst in lists:
        for c in lst or []:
            if not isinstance(c,dict):
                continue
            st=bar_stamp(candle_bar_time(c))
            if st is None:
                continue
            by[st]=c
    merged=[by[k] for k in sorted(by)]
    return completed_candles(merged,now_utc=now_utc)


def _cfg(store):
    try:
        from .self_params import get as sp_get
        return {
            'enabled':int(sp_get(store,'forecast_bridge_enabled',1)),
            'nudge':float(sp_get(store,'forecast_bridge_nudge',0.06)),
            'min_resolved':int(sp_get(store,'forecast_bridge_min_resolved',24)),
            'refit_every':int(sp_get(store,'forecast_bridge_refit_every',12)),
        }
    except Exception:
        return {'enabled':1,'nudge':0.06,'min_resolved':24,'refit_every':12}


def _weights_path(store):
    base=Path(store.path).parent if hasattr(store,'path') else Path('data')
    return base/'forecast_bridge.npz'


def fit_from_memory(store, *, epic=None, seed=0, min_resolved=24):
    """Fit MultiHorizonForecast on resolved forecast_memory rows; persist weights."""
    mem=ForecastMemory(store)
    rows=mem.rows(epic=epic,resolved_only=True,limit=5000)
    if len(rows)<int(min_resolved):
        return {'fitted':False,'n':len(rows),'reason':f'need ≥{min_resolved} resolved'}
    dim=len(rows[0].get('features') or [])
    if dim<1:
        return {'fitted':False,'n':len(rows),'reason':'empty features'}
    # Drop rows with mismatched dim (schema drift).
    clean=[r for r in rows if isinstance(r.get('features'),list) and len(r['features'])==dim]
    if len(clean)<int(min_resolved):
        return {'fitted':False,'n':len(clean),'reason':'dim mismatch / too few'}
    model=MultiHorizonForecast(dim=dim,horizons=HORIZONS,seed=seed,epochs=35)
    info=model.fit(clean)
    if not info.get('fitted'):
        return info
    path=_weights_path(store)
    np.savez_compressed(
        path,
        W_r=model.W_r,b_r=model.b_r,W_s=model.W_s,b_s=model.b_s,
        W_p=model.W_p,b_p=model.b_p,
        dim=np.array([dim]),horizons=np.array(list(HORIZONS)))
    meta={'fitted':True,'n':len(clean),'dim':dim,'epic':epic,
          'path':str(path),'horizons':list(HORIZONS)}
    store.set(STORE_FIT_META,meta)
    store.set(STORE_WEIGHTS,{'path':str(path),'dim':dim,'n':len(clean)})
    return meta


def load_model(store):
    meta=store.get(STORE_WEIGHTS) or store.get(STORE_FIT_META) or {}
    path=Path(meta.get('path') or _weights_path(store))
    if not path.is_file():
        return None
    try:
        data=np.load(path)
        dim=int(data['dim'][0])
        horizons=tuple(int(x) for x in data['horizons'].tolist())
        model=MultiHorizonForecast(dim=dim,horizons=horizons)
        model.W_r=np.array(data['W_r'],dtype=float)
        model.b_r=np.array(data['b_r'],dtype=float)
        model.W_s=np.array(data['W_s'],dtype=float)
        model.b_s=np.array(data['b_s'],dtype=float)
        model.W_p=np.array(data['W_p'],dtype=float)
        model.b_p=np.array(data['b_p'],dtype=float)
        return model
    except Exception:
        return None


def shadow_advice(store, features, *, neural_scores=None):
    """Multi-horizon forecast vs neural argmax — for UI + soft nudge.

    Uses h=1,3,5,15. Primary advice is majority vote among BUY/SELL (HOLD
    ignored for vote). ``agree`` is True only when majority matches neural.
    """
    if not isinstance(features,(list,tuple,np.ndarray)):
        return {'advice':None,'reason':'no_features'}
    model=load_model(store)
    if model is None:
        return {'advice':None,'reason':'no_fitted_model'}
    x=np.asarray(features,dtype=float).ravel()
    if x.size!=model.dim or not np.all(np.isfinite(x)):
        return {'advice':None,'reason':'feature_dim_mismatch',
                'need_dim':model.dim,'got':int(x.size)}
    try:
        preds=model.predict_dict(x)
    except Exception as exc:
        return {'advice':None,'reason':f'predict_failed:{type(exc).__name__}'}
    by_h={}
    votes={'BUY':0,'SELL':0}
    conf_sum={'BUY':0.0,'SELL':0.0}
    for h in HORIZONS:
        pred=preds.get(h) if isinstance(preds,dict) else None
        if pred is None:
            continue
        action=max(pred.p_hat,key=pred.p_hat.get)
        conf=float(pred.p_hat.get(action) or 0)
        by_h[int(h)]={
            'advice':action,'confidence':conf,
            'sigma_hat':float(pred.sigma_hat),'r_hat':float(pred.r_hat),
            'p_hat':dict(pred.p_hat),
        }
        if action in ('BUY','SELL'):
            votes[action]+=1
            conf_sum[action]+=conf
    if not by_h:
        return {'advice':None,'reason':'no_horizons'}
    # Prefer multi-horizon majority; fall back to h=1.
    if votes['BUY'] or votes['SELL']:
        if votes['BUY']!=votes['SELL']:
            action='BUY' if votes['BUY']>votes['SELL'] else 'SELL'
        else:
            action='BUY' if conf_sum['BUY']>=conf_sum['SELL'] else 'SELL'
        n_vote=max(1,votes[action])
        conf=conf_sum[action]/n_vote
    else:
        h1=by_h.get(1) or next(iter(by_h.values()))
        action=h1['advice']
        conf=float(h1['confidence'])
    pred1=(preds.get(1) if isinstance(preds,dict) else None)
    neural=None
    if isinstance(neural_scores,dict):
        try:
            neural=max(('BUY','SELL','HOLD'),
                       key=lambda a:float(neural_scores.get(a,0)))
        except (TypeError,ValueError):
            neural=None
    agree_n=sum(1 for v in by_h.values()
                if neural and v.get('advice')==neural)
    conflict_n=sum(1 for v in by_h.values()
                   if neural and v.get('advice') in ('BUY','SELL')
                   and v.get('advice')!=neural)
    out={
        'advice':action,
        'confidence':conf,
        'sigma_hat':float(pred1.sigma_hat) if pred1 is not None else None,
        'r_hat':float(pred1.r_hat) if pred1 is not None else None,
        'p_hat':dict(pred1.p_hat) if pred1 is not None else {},
        'neural_argmax':neural,
        'agree':(neural==action) if neural else None,
        'horizon':1,
        'horizons':by_h,
        'agree_horizons':agree_n,
        'conflict_horizons':conflict_n,
        'reason':(f'forecast majority {action} p={conf:.2f} '
                  f'(agree={agree_n}/{len(by_h)} conflict={conflict_n})'
                  + (f'; neural={neural}' if neural else '')),
    }
    store.set(STORE_SHADOW,out)
    return out


def soft_nudge_scores(scores, advice, *, store=None):
    """Boost when multi-horizon forecast **agrees** with neural BUY/SELL.

    Never flips the neural side. When horizons conflict with neural, damp the
    neural side slightly (still no flip). Forecast-HOLD = log only.
    """
    if not isinstance(scores,dict):
        return scores,{'nudged':False,'reason':'no_scores'}
    cfg=_cfg(store)
    if not cfg.get('enabled'):
        return dict(scores),{'nudged':False,'reason':'bridge_off'}
    if not isinstance(advice,dict) or not advice.get('advice'):
        return dict(scores),{'nudged':False,'reason':'no_advice'}
    strength=float(cfg.get('nudge') or 0.06)
    strength=max(0.0,min(0.15,strength))
    out={k:float(scores.get(k,0) or 0) for k in ('BUY','SELL','HOLD')}
    side=str(advice.get('advice') or '').upper()
    conf=float(advice.get('confidence') or 0)
    agree=advice.get('agree')
    if side not in out:
        return dict(scores),{'nudged':False,'reason':'bad_side'}
    if conf<0.40:
        return dict(scores),{'nudged':False,'reason':'low_confidence','advice':advice}
    agree_h=int(advice.get('agree_horizons') or 0)
    conflict_h=int(advice.get('conflict_horizons') or 0)
    # Reinforce when majority agrees; louder when several horizons align.
    if agree is True and side in ('BUY','SELL'):
        scale=1.0+0.25*max(0,agree_h-1)
        delta=strength*min(1.0,conf)*min(1.75,scale)
        out[side]+=delta
        opp='SELL' if side=='BUY' else 'BUY'
        out[opp]=max(0.0,out[opp]-0.35*delta)
        for k in out:
            out[k]=max(0.0,min(1.0,out[k]))
        s=sum(out.values())
        if s<=1e-12:
            return dict(scores),{'nudged':False,'reason':'degenerate'}
        out={k:out[k]/s for k in out}
        return out,{'nudged':True,'delta':delta,'advice':side,'agree':True,
                    'agree_horizons':agree_h,'confidence':conf,
                    'reason':advice.get('reason')}
    # Multi-horizon conflict: damp neural side slightly — never flip.
    neural=str(advice.get('neural_argmax') or '').upper()
    if (agree is False and conflict_h>=2 and neural in ('BUY','SELL')
            and side in ('BUY','SELL') and side!=neural):
        delta=0.5*strength*min(1.0,conf)
        out[neural]=max(0.0,out[neural]-delta)
        out['HOLD']=min(1.0,out.get('HOLD',0)+0.5*delta)
        for k in out:
            out[k]=max(0.0,min(1.0,out[k]))
        s=sum(out.values())
        if s<=1e-12:
            return dict(scores),{'nudged':False,'reason':'degenerate'}
        out={k:out[k]/s for k in out}
        # Guard: neural side must still beat the opposite after damp.
        opp='SELL' if neural=='BUY' else 'BUY'
        if out[neural]+1e-9<out[opp]:
            return dict(scores),{'nudged':False,'reason':'conflict_damp_would_flip',
                                 'advice':side,'agree':False,
                                 'conflict_horizons':conflict_h}
        return out,{'nudged':True,'delta':-delta,'advice':side,'agree':False,
                    'conflict_horizons':conflict_h,'damp':neural,
                    'reason':'multi_horizon_conflict_damp'}
    return dict(scores),{
        'nudged':False,
        'reason':('conflict_no_hold_push' if agree is False
                  else ('forecast_hold_ignored' if side=='HOLD'
                        else 'no_agree_boost')),
        'advice':side,'agree':agree,'confidence':conf,
        'agree_horizons':agree_h,'conflict_horizons':conflict_h,
    }


def maybe_refit_and_advise(store, *, epic=None, features=None, neural_scores=None):
    """After resolve: occasionally refit; always refresh shadow if model exists."""
    cfg=_cfg(store)
    if not cfg.get('enabled'):
        return {'refit':False,'shadow':None}
    mem=ForecastMemory(store)
    n_ok=mem.summary(epic=epic).get('resolved') or 0
    meta=store.get(STORE_FIT_META) or {}
    last_n=int(meta.get('n') or 0)
    need=int(cfg.get('min_resolved') or 24)
    every=int(cfg.get('refit_every') or 12)
    refit_info=None
    if n_ok>=need and (n_ok-last_n>=every or not meta.get('fitted')):
        refit_info=fit_from_memory(store,epic=epic,min_resolved=need)
    shadow=None
    if features is not None:
        shadow=shadow_advice(store,features,neural_scores=neural_scores)
    return {'refit':refit_info,'shadow':shadow,'resolved':n_ok}
