"""Deep audit harness: real weight moves, full shadow→LIVE promote, honest events."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.autonomy import AutoEvolution
from app.core import NeuralCore, WEIGHT_PARAM_NAMES
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.self_params import DEFAULTS, evolve_from_feedback, load, save
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_adopt_architecture_copies_full_shadow_neuron_count():
    """Promote must not silently drop shadow neurons when n differs."""
    live=NeuralCore(n=12,d=8,seed=1)
    shadow=NeuralCore(n=16,d=8,seed=2)
    shadow.P+=0.3
    assert live.copy_weights_from(shadow,adopt_architecture=True) is True
    assert live.n==16
    assert np.allclose(live.P,shadow.P)
    assert np.allclose(live.W,shadow.W)
    assert live.neuron_ids.tolist()==shadow.neuron_ids.tolist()


def test_closed_trade_event_honest_when_scratch_skips_weights():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.market={'epic':'GOLD','mid':100.,'tick':.01,'spread':.02}
        e.attach(HistoryBroker())
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'scr','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.set('entry_brain:scr',{
            'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
            'transition':brain._json_transition(brain.core.last_transition)})
        before=brain.core.snapshot_weights()
        e._learn_closed_trade('scr',{'level':100.0,'profit':-0.02,'pnl_currency':'GBP'})
        after=brain.core.snapshot_weights()
        assert NeuralCore.weight_delta_norm(before,after)==pytest.approx(0)
        # Latest learning event must NOT claim weights updated.
        rows=store.db.execute(
            "SELECT message,details FROM events WHERE kind='learning' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        assert any('without weight update' in (r[0] or '') for r in rows)
        assert not any('updated LIVE neural weights' in (r[0] or '') for r in rows)


def test_closed_trade_win_updates_weights_and_bumps_generation():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.market={'epic':'GOLD','mid':100.,'tick':.01,'spread':.02}
        e.attach(HistoryBroker())
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'win1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.set('entry_brain:win1',{
            'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
            'transition':brain._json_transition(brain.core.last_transition)})
        from app.learning_generation import current as gen_current, counters as gen_counters
        gen0=gen_current(store)
        steps0=gen_counters(store)['training_steps']
        before=brain.core.snapshot_weights()
        e._learn_closed_trade('win1',{'level':101.0,'profit':2.0,'pnl_currency':'GBP'})
        after=brain.core.snapshot_weights()
        assert NeuralCore.weight_delta_norm(before,after)>0
        assert gen_current(store)==gen0+1
        assert gen_counters(store)['training_steps']==steps0+1
        rows=store.db.execute(
            "SELECT message FROM events WHERE kind='learning' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        assert any('updated LIVE neural weights' in (r[0] or '') for r in rows)


def test_trail_and_scratch_knobs_evolve_from_outcomes():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        assert 'trail_open_gap_frac' in DEFAULTS
        frac0=load(store)['trail_open_gap_frac']
        scratch0=load(store)['scratch_pnl_abs']
        evolve_from_feedback(store,broker_pnl=-0.02)  # scratch
        assert load(store)['scratch_pnl_abs']>=scratch0
        assert load(store)['trail_open_gap_frac']>=frac0
        grow0=load(store)['grow_error_threshold']
        evolve_from_feedback(store,broker_pnl=-2.0)
        assert load(store)['grow_error_threshold']<=grow0


def test_post_be_trail_gap_reads_store_knobs():
    from app.protection_ai import _post_be_trail_gap
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        save(store,{**DEFAULTS,'trail_min_mult':3.0,'trail_open_gap_frac':0.5,
                    'trail_fallback_frac':0.6},reason='test')
        # open_gap=1.0, minimum=0.1 → floor = max(0.1, min(1.0, max(0.3, 0.5))) = 0.5
        # No zone → max(floor, fallback*open_gap) = max(0.5, 0.6) = 0.6
        g=_post_be_trail_gap(1.0,0.1,None,store=store)
        assert g==pytest.approx(0.6)
        # Zone below floor → still floor 0.5
        g2=_post_be_trail_gap(1.0,0.1,0.2,store=store)
        assert g2==pytest.approx(0.5)
