"""Shadow neural cycle: persist h/c+steps; no flip-exit vs LIVE trail rules."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.shadow_paper import ShadowPaperJournal


def test_shadow_hc_persist_and_advance():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.core.last_z=np.zeros(brain.core.d)
        brain.core.last_z[0]=0.4
        e=SingleTradeEngine(store,brain)
        shadow=e._ensure_shadow_brain()
        assert shadow.steps==0
        h0=shadow.h.copy();c0=shadow.c.copy()
        scores=e._shadow_scores_now(advance=True,bar_time='2024-01-01T00:01:00+00:00')
        assert scores is not None
        assert shadow.steps==1
        tr=shadow.last_transition
        assert tr is not None
        assert np.allclose(tr['h'],h0)
        assert np.allclose(tr['c'],c0)
        assert len(tr['z'])==shadow.d
        e._persist_shadow_state()
        raw=store.get(e.SHADOW_STATE_KEY)
        assert raw and int(raw['steps'])==1
        assert len(raw['h'])==shadow.n
        saved_h=np.asarray(raw['h']);saved_c=np.asarray(raw['c'])
        saved_steps=int(raw['steps']);saved_d=int(raw['d']);saved_n=int(raw['n'])
        e.shadow_brain=None
        shadow2=e._ensure_shadow_brain()
        assert shadow2.n==saved_n and shadow2.d==saved_d
        assert np.allclose(shadow2.h,saved_h)
        assert np.allclose(shadow2.c,saved_c)
        assert shadow2.steps==saved_steps


def test_shadow_learn_uses_own_decision_state_not_live():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.core.last_z=np.linspace(-0.2,0.2,brain.core.d)
        e=SingleTradeEngine(store,brain)
        shadow=e._ensure_shadow_brain()
        e._shadow_scores_now(advance=True,bar_time='t0')
        own=dict(shadow.last_transition)
        live_tr={
            'z':own['z'].copy(),
            'h':np.ones_like(own['h'])*9.0,
            'c':np.ones_like(own['c'])*9.0,
            'dt':1.0,
            'neuron_ids':own['neuron_ids'].copy(),
        }
        e._shadow_learn(None,0,lr=0.1)
        used=e._shadow_decision_transition()
        assert used is not None
        assert not np.allclose(used['h'],live_tr['h'])
        assert np.allclose(used['h'],own['h'])


def test_no_flip_exit_vs_live_trail_rules():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        j=ShadowPaperJournal(store)
        assert j.on_bar('BUY',mid=100.,spread=0.2,bar_time='b1',
                        high=100.3,low=99.8,tick=0.1,
                        stop_distance=2.0,profit_distance=4.0) is None
        assert j.position is not None
        entry=j.position['entry']
        stop=j.position['stop']
        # Opposite SELL signal — must keep position (LIVE = no flip exit).
        assert j.on_bar('SELL',mid=100.5,spread=0.2,bar_time='b2',
                        high=100.8,low=100.1,tick=0.1) is None
        assert j.position is not None
        assert j.position['direction']=='BUY'
        assert all(r.get('exit_reason')!='flip' for r in j.history)
        assert j.on_bar('HOLD',mid=101.5,spread=0.2,bar_time='b3',
                        high=102.0,low=101.0,tick=0.1) is None
        assert j.position is not None
        closed=j.on_bar('SELL',mid=float(stop)-1.0,spread=0.2,bar_time='b4',
                        high=float(stop)+0.2,low=float(stop)-1.5,tick=0.1)
        assert closed is not None
        assert closed['exit_reason'] in ('sl','trail','tp')
        assert closed['exit_reason']!='flip'
        assert closed['entry']==pytest.approx(entry)
