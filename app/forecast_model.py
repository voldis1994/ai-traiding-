"""Multi-task multi-horizon forecast head (offline / shadow quality only).

Output form (audit):
  f_θ(X) = { r̂_{t,h}, σ̂_{t,h}, p̂_{t,h} }_{h ∈ {1,3,5,15}}

Does not replace LIVE NeuralCore entry — used for quality comparison.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .forecast_targets import HORIZONS, LABEL_BUY, LABEL_HOLD, LABEL_SELL

ACTIONS = ('BUY','SELL','HOLD')
LABEL_TO_IDX = {LABEL_BUY:0,LABEL_SELL:1,LABEL_HOLD:2}


def _softmax(logits):
    z=np.asarray(logits,dtype=float)
    z=z-np.max(z)
    e=np.exp(z)
    return e/np.maximum(e.sum(),1e-12)


@dataclass
class ForecastPrediction:
    horizon: int
    r_hat: float
    sigma_hat: float
    p_hat: dict  # BUY/SELL/HOLD


@dataclass
class MultiHorizonForecast:
    """Linear multi-task head: shared features → per-horizon r, logσ, class logits."""
    dim: int
    horizons: tuple = HORIZONS
    lr: float = 0.05
    l2: float = 1e-3
    epochs: int = 40
    seed: int = 0
    W_r: np.ndarray = field(default=None,repr=False)
    b_r: np.ndarray = field(default=None,repr=False)
    log_sigma: np.ndarray = field(default=None,repr=False)
    W_p: np.ndarray = field(default=None,repr=False)
    b_p: np.ndarray = field(default=None,repr=False)

    def __post_init__(self):
        rng=np.random.default_rng(self.seed)
        H=len(self.horizons);D=int(self.dim)
        if self.W_r is None:
            self.W_r=rng.normal(0,0.01,size=(H,D))
            self.b_r=np.zeros(H)
            self.log_sigma=np.zeros(H)  # σ = softplus(log_sigma) ≈ 0.69 initially via softplus(0)
            self.W_p=rng.normal(0,0.01,size=(H,3,D))
            self.b_p=np.zeros((H,3))

    @staticmethod
    def _softplus(x):
        # numerically stable softplus
        x=np.asarray(x,dtype=float)
        return np.where(x>20,x,np.log1p(np.exp(np.clip(x,-40,20))))

    def predict_one(self, x):
        x=np.asarray(x,dtype=float).ravel()
        if x.size!=self.dim:
            raise ValueError(f'feature dim {x.size} != model dim {self.dim}')
        out=[]
        for i,h in enumerate(self.horizons):
            r=float(self.W_r[i]@x+self.b_r[i])
            sig=float(self._softplus(self.log_sigma[i])+1e-6)
            logits=self.W_p[i]@x+self.b_p[i]
            p=_softmax(logits)
            out.append(ForecastPrediction(
                horizon=int(h),r_hat=r,sigma_hat=sig,
                p_hat={ACTIONS[j]:float(p[j]) for j in range(3)}))
        return out

    def predict_dict(self, x):
        return {p.horizon:p for p in self.predict_one(x)}

    def fit(self, rows, *, horizons=None):
        """SGD on resolved forecast_memory rows with features + outcomes."""
        horizons=tuple(horizons or self.horizons)
        h_index={h:i for i,h in enumerate(self.horizons)}
        X=[];Y_r=[];Y_lab=[];mask=[]
        for row in rows or []:
            feats=row.get('features')
            outcomes=row.get('outcomes') or {}
            if not feats or not outcomes:
                continue
            x=np.asarray(feats,dtype=float).ravel()
            if x.size!=self.dim or not np.all(np.isfinite(x)):
                continue
            r_vec=np.full(len(self.horizons),np.nan)
            lab_vec=np.full(len(self.horizons),-1,dtype=int)
            m=np.zeros(len(self.horizons),dtype=bool)
            for h in horizons:
                if h not in h_index:continue
                o=outcomes.get(h) or outcomes.get(str(h))
                if not isinstance(o,dict):continue
                try:
                    r=float(o['r']);lab=int(o['label'])
                except (KeyError,TypeError,ValueError):
                    continue
                if not math.isfinite(r) or lab not in LABEL_TO_IDX:
                    continue
                i=h_index[h]
                r_vec[i]=r;lab_vec[i]=LABEL_TO_IDX[lab];m[i]=True
            if not m.any():
                continue
            X.append(x);Y_r.append(r_vec);Y_lab.append(lab_vec);mask.append(m)
        if len(X)<8:
            return {'fitted':False,'n':len(X),'reason':'need ≥8 resolved rows'}
        X=np.asarray(X);Y_r=np.asarray(Y_r);Y_lab=np.asarray(Y_lab);mask=np.asarray(mask)
        n=X.shape[0]
        rng=np.random.default_rng(self.seed)
        for _ in range(int(self.epochs)):
            order=rng.permutation(n)
            for idx in order:
                x=X[idx]
                for i in range(len(self.horizons)):
                    if not mask[idx,i]:
                        continue
                    # Return head + heteroscedastic NLL
                    r_hat=float(self.W_r[i]@x+self.b_r[i])
                    sig=float(self._softplus(self.log_sigma[i])+1e-6)
                    err=r_hat-float(Y_r[idx,i])
                    # d/dθ ½(err/σ)² + log σ
                    d_r=err/(sig*sig)
                    self.W_r[i]-=self.lr*(d_r*x+self.l2*self.W_r[i])
                    self.b_r[i]-=self.lr*d_r
                    # softplus'(z)=sigmoid(z); ∂NLL/∂z ≈ (1 - err²/σ²)*sigmoid
                    sigmoid=1/(1+math.exp(-float(np.clip(self.log_sigma[i],-40,40))))
                    d_ls=(1.0-(err*err)/(sig*sig))*sigmoid
                    self.log_sigma[i]-=self.lr*0.1*d_ls
                    # Classification CE
                    logits=self.W_p[i]@x+self.b_p[i]
                    p=_softmax(logits)
                    y=int(Y_lab[idx,i])
                    g=p.copy();g[y]-=1.0
                    self.W_p[i]-=self.lr*(np.outer(g,x)+self.l2*self.W_p[i])
                    self.b_p[i]-=self.lr*g
        return {'fitted':True,'n':n,'horizons':list(self.horizons),'dim':self.dim}

    def action_at(self, x, horizon=1):
        pred=self.predict_dict(x).get(int(horizon))
        if pred is None:
            return 'HOLD',{}
        action=max(pred.p_hat,key=pred.p_hat.get)
        return action,pred.p_hat
