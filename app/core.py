"""Stateful, dynamically resized recurrent network from the supplied mathematical PDF.

Outputs are uncalibrated model scores, NOT proven trading forecasts. Delayed targets
are explicit delayed price-direction proxies (the source PDF does not define y_t).
Gradients flow through P,W,R,O,b,tau,tau_c,G for a one-step recurrent unroll.
"""
import numpy as np
import torch
import torch.nn.functional as F

ACTIONS=('BUY','SELL','HOLD')

class NeuralCore:
    def __init__(self,n=16,d=3,seed=7):
        if n<1:raise ValueError('Neuron count must be positive')
        rng=np.random.default_rng(seed);self.rng=rng
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
    # mistake in one update. 0.1 yields ~0.01-scale changes on P/R/O (and larger on b)
    # while gradient clipping (max_norm=2) still bounds instability.
    DEFAULT_LR = 0.1

    def learn(self,transition,label,lr=None,reconstruction_weight=.01):
        """One-step BPTT supervised delayed price-direction class and reconstruction objective.
        Model outputs are trained on market direction proxy, not financial returns.
        """
        if lr is None:lr=self.DEFAULT_LR
        if not (isinstance(lr,(int,float)) and lr>0):raise ValueError('Learning rate must be positive')
        if transition is None or label not in (0,1,2):return None
        t=lambda value,grad=False:torch.tensor(value,dtype=torch.float64,requires_grad=grad)
        tensors={name:t(getattr(self,name),True) for name in ('P','W','R','O','b','G','tau','tau_c')}
        p,w,r,o,b,g,tau,tauc=(tensors[name] for name in ('P','W','R','O','b','G','tau','tau_c'))
        aligned=self._aligned_state(transition['h'],transition['c'],transition.get('neuron_ids'))
        if aligned is None:return None
        z=t(transition['z']);h=t(aligned[0]);c=t(aligned[1]);dt=float(transition['dt'])
        if tuple(z.shape)!=(self.d,) or not np.isfinite(float(dt)) or dt<=0:return None
        lam=1-torch.exp(-dt/torch.clamp(tau,min=.01))
        hn=h+lam*(torch.tanh(p@z+w@h)-h)
        gamma=1-torch.exp(-dt/torch.clamp(tauc,min=.01))
        cn=c+gamma*(torch.tanh(.5*hn+.2*c)-c)
        logits=r@hn+o@cn+b
        ce=F.cross_entropy(logits.unsqueeze(0),torch.tensor([label]))
        reconstruction=F.mse_loss(g@hn,z)
        loss=ce+reconstruction_weight*reconstruction
        if not torch.isfinite(loss):return None
        loss.backward()
        norm=torch.nn.utils.clip_grad_norm_(list(tensors.values()),max_norm=2.)
        if not torch.isfinite(norm):return None
        for name,param in tensors.items():
            grad=param.grad
            if grad is None:continue
            updated=getattr(self,name)-lr*grad.detach().numpy()
            if name in ('tau','tau_c'):updated=np.clip(updated,.1,1000.)
            if not np.isfinite(updated).all():return None
            setattr(self,name,updated)
        self.last_loss=float(loss.detach());self.last_grad=float(norm.detach())
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

    def snapshot(self):
        return {'neurons':self.n,'steps':self.steps,'smoothed_error':self.vbar,
                'mean_activation':float(np.mean(np.abs(self.h))),
                'births':self.births,'pruned':self.deaths,'loss':self.last_loss,
                'gradient_norm':self.last_grad,'utility_mean':float(np.mean(self.utility)),
                'activations':self.h[:72].round(4).tolist(),'memory':self.c[:72].round(4).tolist(),
                'edges':int(np.count_nonzero(self.W))}
