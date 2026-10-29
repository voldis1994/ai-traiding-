"""Stateful, dynamically resized recurrent network from the supplied mathematical PDF.

Outputs are uncalibrated model scores, NOT proven trading forecasts. Delayed targets
are explicit delayed price-direction proxies (the source PDF does not define y_t).
Gradients flow through P,W,R,O,b,tau,tau_c,G for a one-step recurrent unroll.
"""
import hashlib
import numpy as np
import torch
import torch.nn.functional as F

ACTIONS=('BUY','SELL','HOLD')

# Audit #17 — two distinct supervised objectives share the same NeuralCore.
# Minute-direction (next M1 bar) is frequent and auxiliary; closed-trade P&L is
# the primary corrective teacher. Relative weights keep M1 updates from washing
# out sparse trade-PnL gradients (trade_pnl : minute_direction = 5 : 1).
OBJECTIVE_MINUTE_DIRECTION = 'minute_direction'
OBJECTIVE_TRADE_PNL = 'trade_pnl'
OBJECTIVE_WEIGHTS = {
    OBJECTIVE_MINUTE_DIRECTION: 0.2,
    OBJECTIVE_TRADE_PNL: 1.0,
}
# Documented ratio used by AdaptiveBrain / machine_spec.
MINUTE_VS_TRADE_WEIGHT_RATIO = (
    OBJECTIVE_WEIGHTS[OBJECTIVE_MINUTE_DIRECTION]
    / OBJECTIVE_WEIGHTS[OBJECTIVE_TRADE_PNL]
)

# Audit #18 — ordinary online updates snapshot prior weights, bound ΔW, and
# restore when the same-example CE criterion worsens. Never "proven".
WEIGHT_PARAM_NAMES = ('P', 'W', 'R', 'O', 'b', 'G', 'tau', 'tau_c')
DEFAULT_MAX_WEIGHT_DELTA_NORM = 2.0


def client_neural_seed(client_id) -> int:
    """Stable per-client init seed so fresh brains are not clones of default seed=7.

    Public market features are shared; identical weights would yield identical BUY/SELL.
    Each client must start from its own RNG seed. Existing brain.npz weights still win
    on load — this only affects new/reset cores.
    """
    cid=int(client_id)
    digest=hashlib.blake2b(f'nta-client-{cid}'.encode('utf8'),digest_size=4).digest()
    seed=int.from_bytes(digest,'big')%(2**31-1)
    if seed in (0,7):
        seed=10007+cid
    return int(seed)


class NeuralCore:
    def __init__(self,n=16,d=3,seed=7):
        if n<1:raise ValueError('Neuron count must be positive')
        self.seed=int(seed)
        rng=np.random.default_rng(self.seed);self.rng=rng
        self.n=n;self.d=d
        self.P=rng.normal(0,.12,(n,d));self.W=np.zeros((n,n))
        self.R=rng.normal(0,.12,(3,n));self.O=rng.normal(0,.12,(3,n));self.b=np.zeros(3)
        self.h=np.zeros(n);self.c=np.zeros(n)
        self.tau=np.ones(n)*10;self.tau_c=np.ones(n)*25
        self.mu=np.zeros(d);self.nu=np.ones(d)
        self.age=np.zeros(n,dtype=int);self.utility=np.zeros(n)
        self.G=rng.normal(0,.12,(d,n))
        self.vbar=0.;self.steps=0;self.last_z=np.zeros(d);self.last_transition=None
        self.births=0;self.deaths=0;self.last_loss=None;self.last_grad=0.;self.accuracy=0.
        self.last_learn_meta=None  # audit #17–18: objective + controlled-update status
        # Stable neuron identities let delayed examples survive network growth/pruning.
        self.neuron_ids=np.arange(n,dtype=np.int64);self.next_neuron_id=n

    @staticmethod
    def _softmax(logits):
        ex=np.exp(logits-np.max(logits));return ex/ex.sum()

    def _forward(self,z,h,c,dt):
        lam=1-np.exp(-dt/self.tau)
        hn=h+lam*(np.tanh(self.P@z+self.W@h)-h)
        gamma=1-np.exp(-dt/self.tau_c)
        cn=c+gamma*(np.tanh(.5*hn+.2*c)-c)
        logits=self.R@hn+self.O@cn+self.b
        return hn,cn,logits

    def step(self,x,dt=1.0,epsilon=1e-6,zeta=.01):
        x=np.asarray(x,dtype=float)
        if x.shape!=(self.d,) or not np.isfinite(x).all() or dt<=0:raise ValueError('Invalid input or time interval')
        sigma=np.maximum(np.sqrt(self.nu),epsilon)
        z=np.clip((x-self.mu)/sigma,-7,7)
        h0=self.h.copy();c0=self.c.copy()
        hn,cn,logits=self._forward(z,h0,c0,dt)
        prob=self._softmax(logits)
        residual=float(np.linalg.norm(z-self.G@hn));self.vbar=.95*self.vbar+.05*residual
        self.last_transition={'z':z.copy(),'h':h0,'c':c0,'dt':dt,
                              'neuron_ids':self.neuron_ids.copy()}
        self.last_z=z.copy();self.h=hn;self.c=cn;self.age+=1;self.steps+=1
        old_mu=self.mu.copy();self.mu=(1-zeta)*self.mu+zeta*x
        self.nu=(1-zeta)*self.nu+zeta*(x-old_mu)**2
        self.nu=np.maximum(self.nu,epsilon**2)
        action=ACTIONS[int(np.argmax(prob))]
        return {'action':action,'scores':dict(zip(ACTIONS,map(float,prob))),
                'neurons':self.n,'reconstruction_error':residual,'smoothed_error':self.vbar,
                'z':z.tolist(),'logits':logits.tolist()}

    def _aligned_state(self,h,c,ids=None):
        """Map a saved recurrent state into current neuron IDs, zero-filling births.

        Pruned neurons cannot participate in training after they have been removed.
        This mapping preserves the surviving units instead of silently losing the
        entire delayed supervision example.
        """
        h=np.asarray(h,dtype=float);c=np.asarray(c,dtype=float)
        if h.ndim!=1 or c.shape!=h.shape:return None
        if ids is None:ids=np.arange(len(h),dtype=np.int64)
        ids=np.asarray(ids,dtype=np.int64)
        if ids.shape!=h.shape or len(set(ids.tolist()))!=len(ids):return None
        if not np.isfinite(h).all() or not np.isfinite(c).all():return None
        lookup={int(k):i for i,k in enumerate(ids)}
        aligned_h=np.zeros(self.n);aligned_c=np.zeros(self.n)
        for dest,key in enumerate(self.neuron_ids):
            pos=lookup.get(int(key))
            if pos is not None:aligned_h[dest]=h[pos];aligned_c[dest]=c[pos]
        return aligned_h,aligned_c

    # Default step was 0.0005 (~1e-5 weight deltas) — too weak to correct a visible
    # mistake in one update. 0.15 yields clear weight moves on each closed-trade
    # regret while gradient clipping (max_norm=2) still bounds instability.
    DEFAULT_LR = 0.15

    def snapshot_weights(self):
        """Copy trainable matrices for controlled update / restore (audit #18)."""
        return {name: np.asarray(getattr(self, name), dtype=float).copy()
                for name in WEIGHT_PARAM_NAMES}

    def restore_weights(self, snap):
        """Reload a prior weight snapshot. Returns False if shapes disagree."""
        if not isinstance(snap, dict):
            return False
        for name in WEIGHT_PARAM_NAMES:
            if name not in snap:
                return False
            cur = getattr(self, name)
            arr = np.asarray(snap[name], dtype=float)
            if arr.shape != np.asarray(cur).shape or not np.isfinite(arr).all():
                return False
        for name in WEIGHT_PARAM_NAMES:
            setattr(self, name, np.asarray(snap[name], dtype=float).copy())
        return True

    @staticmethod
    def weight_delta_norm(before, after):
        """L2 norm of concatenated Δ across trainable params."""
        if not isinstance(before, dict) or not isinstance(after, dict):
            return float('inf')
        total = 0.0
        for name in WEIGHT_PARAM_NAMES:
            if name not in before or name not in after:
                return float('inf')
            d = np.asarray(after[name], dtype=float) - np.asarray(before[name], dtype=float)
            total += float(np.sum(d * d))
        return float(np.sqrt(total))

    def _ce_on_transition(self, transition, label):
        """Forward-only cross-entropy for restore criterion (no grad)."""
        if transition is None or label not in (0, 1, 2):
            return None
        aligned = self._aligned_state(
            transition['h'], transition['c'], transition.get('neuron_ids'))
        if aligned is None:
            return None
        z = np.asarray(transition['z'], dtype=float)
        h, c = aligned
        dt = float(transition['dt'])
        if z.shape != (self.d,) or not np.isfinite(dt) or dt <= 0:
            return None
        lam = 1 - np.exp(-dt / np.maximum(self.tau, 0.01))
        hn = h + lam * (np.tanh(self.P @ z + self.W @ h) - h)
        gamma = 1 - np.exp(-dt / np.maximum(self.tau_c, 0.01))
        cn = c + gamma * (np.tanh(0.5 * hn + 0.2 * c) - c)
        logits = self.R @ hn + self.O @ cn + self.b
        prob = self._softmax(logits)
        p = max(float(prob[label]), 1e-15)
        return float(-np.log(p))

    def learn(self, transition, label, lr=None, reconstruction_weight=.01,
              objective=None, max_delta_norm=None, restore_if_worsens=True):
        """One-step BPTT with objective weighting + controlled ΔW (audit #17–18).

        `objective` selects OBJECTIVE_WEIGHTS (minute_direction vs trade_pnl).
        Prior weights are snapshotted; the step is projected onto
        max_delta_norm; if same-example CE worsens, weights restore.
        Ordinary learn never reports proven_improvement.
        """
        if lr is None:
            lr = self.DEFAULT_LR
        if not (isinstance(lr, (int, float)) and lr > 0):
            raise ValueError('Learning rate must be positive')
        if transition is None or label not in (0, 1, 2):
            return None
        obj = objective if objective in OBJECTIVE_WEIGHTS else None
        obj_weight = float(OBJECTIVE_WEIGHTS[obj]) if obj else 1.0
        eff_lr = float(lr) * obj_weight
        if max_delta_norm is None:
            max_delta_norm = DEFAULT_MAX_WEIGHT_DELTA_NORM
        max_delta_norm = float(max_delta_norm)
        prior = self.snapshot_weights()
        ce_before = self._ce_on_transition(transition, label)

        t = lambda value, grad=False: torch.tensor(value, dtype=torch.float64, requires_grad=grad)
        tensors = {name: t(getattr(self, name), True) for name in WEIGHT_PARAM_NAMES}
        p, w, r, o, b, g, tau, tauc = (tensors[name] for name in WEIGHT_PARAM_NAMES)
        aligned = self._aligned_state(transition['h'], transition['c'], transition.get('neuron_ids'))
        if aligned is None:
            return None
        z = t(transition['z']); h = t(aligned[0]); c = t(aligned[1]); dt = float(transition['dt'])
        if tuple(z.shape) != (self.d,) or not np.isfinite(float(dt)) or dt <= 0:
            return None
        lam = 1 - torch.exp(-dt / torch.clamp(tau, min=.01))
        hn = h + lam * (torch.tanh(p @ z + w @ h) - h)
        gamma = 1 - torch.exp(-dt / torch.clamp(tauc, min=.01))
        cn = c + gamma * (torch.tanh(.5 * hn + .2 * c) - c)
        logits = r @ hn + o @ cn + b
        ce = F.cross_entropy(logits.unsqueeze(0), torch.tensor([label]))
        reconstruction = F.mse_loss(g @ hn, z)
        loss = ce + reconstruction_weight * reconstruction
        if not torch.isfinite(loss):
            return None
        loss.backward()
        norm = torch.nn.utils.clip_grad_norm_(list(tensors.values()), max_norm=2.)
        if not torch.isfinite(norm):
            return None
        proposed = {}
        for name, param in tensors.items():
            grad = param.grad
            if grad is None:
                proposed[name] = getattr(self, name).copy()
                continue
            updated = getattr(self, name) - eff_lr * grad.detach().numpy()
            if name in ('tau', 'tau_c'):
                updated = np.clip(updated, .1, 1000.)
            if not np.isfinite(updated).all():
                return None
            proposed[name] = updated

        # Bound ΔW: project proposed step onto L2 ball of radius max_delta_norm.
        clipped = False
        delta_norm = self.weight_delta_norm(prior, proposed)
        if (np.isfinite(max_delta_norm) and max_delta_norm > 0
                and np.isfinite(delta_norm) and delta_norm > max_delta_norm):
            scale = max_delta_norm / delta_norm
            for name in WEIGHT_PARAM_NAMES:
                proposed[name] = prior[name] + scale * (proposed[name] - prior[name])
                if name in ('tau', 'tau_c'):
                    proposed[name] = np.clip(proposed[name], .1, 1000.)
            clipped = True
            delta_norm = self.weight_delta_norm(prior, proposed)

        for name in WEIGHT_PARAM_NAMES:
            setattr(self, name, proposed[name])

        restored = False
        ce_after = self._ce_on_transition(transition, label)
        if (restore_if_worsens and ce_before is not None and ce_after is not None
                and ce_after > ce_before + 1e-12):
            self.restore_weights(prior)
            restored = True
            delta_norm = 0.0
            ce_after = ce_before

        self.last_loss = float(loss.detach())
        self.last_grad = float(norm.detach())
        # Audit #18: weight change is never a proven trading improvement.
        self.last_learn_meta = {
            'objective': obj or 'unspecified',
            'objective_weight': obj_weight,
            'eff_lr': eff_lr,
            'delta_norm': float(delta_norm) if np.isfinite(delta_norm) else None,
            'clipped': clipped,
            'restored': restored,
            'ce_before': ce_before,
            'ce_after': ce_after,
            'proven_improvement': False,
        }
        # h/c are detached persistent state, not overwritten during learning.
        return self.last_loss

    def update_utility(self,h,c,label,beta=.02,neuron_ids=None):
        if label not in (0,1,2):return
        aligned=self._aligned_state(h,c,neuron_ids)
        if aligned is None:return
        h,c=aligned
        logits=self.R@h+self.O@c+self.b
        loss=-np.log(max(float(self._softmax(logits)[label]),1e-15))
        for i in range(self.n):
            minus=logits-self.R[:,i]*h[i]-self.O[:,i]*c[i]
            qi=-np.log(max(float(self._softmax(minus)[label]),1e-15))-loss
            self.utility[i]=(1-beta)*self.utility[i]+beta*qi

    def grow_if_needed(self,z,threshold=1.,nmax=None):
        z=np.asarray(z,dtype=float)
        error=float(np.linalg.norm(z-self.G@self.h))
        if not (error>threshold and self.vbar>threshold and (nmax is None or self.n<nmax)):return False
        p=z/(np.linalg.norm(z)+1e-6)
        self.P=np.vstack((self.P,p));self.W=np.pad(self.W,((0,1),(0,1)))
        self.R=np.column_stack((self.R,self.rng.normal(0,.001,3)))
        self.O=np.column_stack((self.O,self.rng.normal(0,.001,3)))
        # A newborn neuron initially has zero recurrent state and zero reconstruction contribution.
        self.G=np.column_stack((self.G,np.zeros(self.d)))
        self.h=np.append(self.h,0.);self.c=np.append(self.c,0.)
        self.tau=np.append(self.tau,10.);self.tau_c=np.append(self.tau_c,25.)
        self.age=np.append(self.age,0);self.utility=np.append(self.utility,0.)
        self.neuron_ids=np.append(self.neuron_ids,self.next_neuron_id)
        self.next_neuron_id+=1
        self.n+=1;self.births+=1
        return True

    def prune(self,min_neurons=8,min_age=200,threshold=-.02):
        eligible=np.where((self.utility<threshold)&(self.age>=min_age))[0]
        count=min(len(eligible),max(0,self.n-min_neurons))
        if not count:return 0
        remove=set(eligible[np.argsort(self.utility[eligible])[:count]].tolist())
        keep=[i for i in range(self.n) if i not in remove]
        self.P=self.P[keep,:];self.W=self.W[np.ix_(keep,keep)]
        self.R=self.R[:,keep];self.O=self.O[:,keep];self.G=self.G[:,keep]
        for name in ('h','c','tau','tau_c','age','utility'):
            setattr(self,name,getattr(self,name)[keep])
        self.neuron_ids=self.neuron_ids[keep]
        self.n=len(keep);self.deaths+=count
        return count

    def expand_inputs(self, new_d, *, init_scale=0.01, insert_at=None):
        """Grow input dimension; preserve learned weights on existing columns.

        Layout contract (must match market_fusion): [BASE_OHLC | DYNAMIC | EXTERNAL].
        - append (insert_at=None): grow EXTERNAL or OHLC-only tail
        - insert_at=ohlc_dim: insert DYNAMIC columns *before* EXTERNAL so fusion
          vectors stay aligned with P/G columns (never append dyn after peers/news)

        New slots get small noise so features can train; W,R,O,h,c unchanged.
        """
        new_d=int(new_d)
        if new_d<self.d:raise ValueError('expand_inputs cannot shrink dimension')
        if new_d==self.d:return False
        extra=new_d-self.d
        at=self.d if insert_at is None else int(insert_at)
        if at<0 or at>self.d:raise ValueError('insert_at out of range')
        new_p=self.rng.normal(0,init_scale,(self.n,extra)) if init_scale else np.zeros((self.n,extra))
        new_g=self.rng.normal(0,init_scale,(extra,self.n)) if init_scale else np.zeros((extra,self.n))
        self.P=np.concatenate([self.P[:,:at],new_p,self.P[:,at:]],axis=1)
        self.G=np.concatenate([self.G[:at,:],new_g,self.G[at:,:]],axis=0)
        self.mu=np.concatenate([self.mu[:at],np.zeros(extra),self.mu[at:]])
        self.nu=np.concatenate([self.nu[:at],np.ones(extra),self.nu[at:]])
        self.last_z=np.concatenate([self.last_z[:at],np.zeros(extra),self.last_z[at:]])
        self.last_transition=None
        self.d=new_d
        return True

    def copy_weights_from(self, other):
        """Copy compatible weights from another core (shadow warm-start). Dim must match."""
        if not isinstance(other,NeuralCore) or other.d!=self.d:return False
        n=min(self.n,other.n)
        self.P[:n,:]=other.P[:n,:]
        self.W[:n,:n]=other.W[:n,:n]
        self.R[:,:n]=other.R[:,:n];self.O[:,:n]=other.O[:,:n]
        self.G[:,:n]=other.G[:,:n]
        self.b[:]=other.b
        self.mu[:]=other.mu;self.nu[:]=other.nu
        self.tau[:n]=other.tau[:n];self.tau_c[:n]=other.tau_c[:n]
        return True

    def clone_architecture(self, *, n=None, seed=None, copy_weights=False):
        """Shadow candidate with optional different neuron count.

        Used for alternative-architecture search — NOT a live swap by itself.
        """
        n=int(n) if n is not None else self.n
        seed=int(seed) if seed is not None else 7
        clone=NeuralCore(n=max(8,n),d=self.d,seed=seed)
        if copy_weights:clone.copy_weights_from(self)
        return clone

    def snapshot(self):
        return {'neurons':self.n,'steps':self.steps,'smoothed_error':self.vbar,
                'mean_activation':float(np.mean(np.abs(self.h))),
                'births':self.births,'pruned':self.deaths,'loss':self.last_loss,
                'gradient_norm':self.last_grad,'utility_mean':float(np.mean(self.utility)),
                'activations':self.h[:72].round(4).tolist(),'memory':self.c[:72].round(4).tolist(),
                'edges':int(np.count_nonzero(self.W)),'input_dim':self.d}
