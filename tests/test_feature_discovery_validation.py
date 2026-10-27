"""Autonomous feature discovery + walk-forward validation (OHLC only)."""
import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.engine import Store, AdaptiveBrain, SingleTradeEngine
from app.feature_discovery import (CANDIDATE_LIBRARY, FeatureDiscovery,
                                   compute_candidate, list_library, _build_library)
from app.feature_registry import FeatureRegistry, MAX_APPROVED
from app.feature_validation import (validate_feature, FeatureValidator,
                                    walk_forward_net_pnl, net_pnl_curve)
from app.market_context import FEATURE_DIM, FEATURE_NAMES, market_features
from app.market_fusion import FUSION_DIM, EXTERNAL_NAMES, fusion_dim_for, fusion_features
from app.core import NeuralCore
from app.profit_learning import label_from_profit, hypothetical_pnl, ProfitLearning
from app.model_validation import compare_models, ModelValidator


BASE=datetime(2026,10,20,12,0,tzinfo=timezone.utc)


def make_bars(n=180, *, trend=0.05, noise=0.01):
    bars=[];price=100.
    for i in range(n):
        o=price
        price=price+trend+noise*math.sin(i/3)
        h=max(o,price)+abs(noise);l=min(o,price)-abs(noise)
        bars.append((o,h,l,price))
    return bars


def varying_spreads(n, *, base=0.02):
    """Per-bar spreads — deliberately not a single pasted value."""
    return [base*(1.+0.15*math.sin(i/7.)) for i in range(n)]


def test_library_has_no_volume_or_dom_claims():
    _build_library()
    for fid,(name,formula,_) in CANDIDATE_LIBRARY.items():
        blob=(fid+name+formula).lower()
        assert 'volume' not in blob and 'dom' not in blob and 'orderflow' not in blob


def test_combinatorial_library_larger_than_seed_dozen():
    _build_library()
    assert len(CANDIDATE_LIBRARY)>20
    gens=[k for k in CANDIDATE_LIBRARY if k.startswith('gen_')]
    assert len(gens)>=10


def test_compute_candidates_on_ohlc_window():
    bars=make_bars()
    window=bars[-61:]
    for spec in list_library()[:5]:
        val=compute_candidate(spec['id'],window,spread=0.03,bar_time=BASE.isoformat())
        assert val is None or math.isfinite(val)


def test_registry_cap_is_not_magic_twelve():
    assert MAX_APPROVED>12


def test_registry_approve_dedup_and_dim_growth():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        reg=FeatureRegistry(store)
        assert reg.ohlc_dim()==FEATURE_DIM
        assert reg.fusion_dim()==FUSION_DIM
        ok,_=reg.approve({'id':'swing_dist_high_20','name':'swing_dist_high_20',
                          'formula':'x','source':'test'})
        assert ok and reg.ohlc_dim()==FEATURE_DIM+1
        assert fusion_dim_for(reg.ohlc_dim())==reg.fusion_dim()
        ok2,why=reg.approve({'id':'swing_dist_high_20','name':'swing_dist_high_20','formula':'x'})
        assert not ok2 and 'duplicate' in why


def test_validation_rejects_single_spread_pasted_on_history():
    bars=make_bars(160)
    flat=[0.03]*len(bars)
    result=validate_feature('atr_ratio_5_20',bars,spreads=flat)
    assert result['approved'] is False
    assert 'single spread' in result['reason']


def test_validation_rejects_missing_spreads():
    bars=make_bars(160)
    result=validate_feature('atr_ratio_5_20',bars,spreads=None)
    assert result['approved'] is False


def test_expand_inputs_insert_before_external_keeps_alignment():
    """Dynamic columns must land before EXTERNAL — same as fusion_features layout."""
    base=FEATURE_DIM;ext=len(EXTERNAL_NAMES)
    core=NeuralCore(n=8,d=base+ext,seed=2)  # already fused: base|external
    # Mark external columns distinctly.
    core.P[:,base:]=5.0
    core.G[base:,:]=5.0
    old_ext_p=core.P[:,base:].copy()
    # Insert 2 dynamic features at OHLC boundary.
    core.expand_inputs(base+2+ext,insert_at=base,init_scale=0.)
    assert core.d==base+2+ext
    # External weights shifted right by 2 — still identical block.
    assert np.allclose(core.P[:,base+2:],old_ext_p)
    assert np.allclose(core.G[base+2:,:],5.0)
    # New dynamic slots are zeros at [base:base+2].
    assert np.allclose(core.P[:,base:base+2],0.)


def test_fusion_layout_base_dynamic_external():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        reg=FeatureRegistry(store)
        reg.approve({'id':'session_phase_utc','name':'session_phase_utc','formula':'sin','source':'t'})
        ohlc=[.01]*(FEATURE_DIM+1)
        peers=[.1]*6;news=[.2,.3]
        # Manual compose matching fusion_features contract.
        vec=ohlc+peers+news
        assert len(vec)==reg.fusion_dim()
        assert len(ohlc)==reg.ohlc_dim()


def test_market_context_appends_approved_dynamic():
    history=[]
    for i in range(90):
        t=BASE-timedelta(minutes=90-i)
        px=100+i*0.1
        history.append({'at':t.isoformat(),'open':px,'high':px+.05,'low':px-.05,'close':px+.02})
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        reg=FeatureRegistry(store)
        reg.approve({'id':'session_phase_utc','name':'session_phase_utc','formula':'sin','source':'t'})
        snap=market_features(history,now_utc=BASE+timedelta(minutes=1),spread=.03,epic='GOLD',
                             approved=reg.approved,schema=reg.ohlc_schema())
        assert snap['ready']
        assert len(snap['features'])==FEATURE_DIM+1
        assert 'session_phase_utc' in snap['feature_names']


def test_neural_expand_preserves_old_weights():
    core=NeuralCore(n=8,d=FEATURE_DIM,seed=1)
    old_p=core.P.copy();old_g=core.G.copy()
    assert core.expand_inputs(FEATURE_DIM+2)
    assert core.d==FEATURE_DIM+2
    assert np.allclose(core.P[:,:FEATURE_DIM],old_p)
    assert np.allclose(core.G[:FEATURE_DIM,:],old_g)


def test_brain_apply_registry_expands_fusion_ready_network():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,bar_time=BASE.isoformat())
        assert brain.core.d==FEATURE_DIM
        reg=FeatureRegistry(store)
        reg.approve({'id':'gap_open_1m','name':'gap_open_1m','formula':'gap','source':'t'})
        grew=brain.apply_registry(reg)
        assert grew and brain.ohlc_dim==FEATURE_DIM+1 and brain.core.d==FEATURE_DIM+1


def test_profit_learning_refuses_fake_plus_minus_one():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        pl=ProfitLearning(store)
        skipped=pl.record_closed(deal_id='x',epic='GOLD',direction='BUY',
            entry=100,exit_level=99,broker_pnl=None,require_broker_pnl=True)
        assert skipped['mode']=='skipped_no_broker_pnl'
        assert pl.summary()['broker_trades']==0
        pl.record_closed(deal_id='y',epic='GOLD',direction='BUY',
            entry=100,exit_level=101,broker_pnl=2.5,require_broker_pnl=True)
        assert pl.summary()['broker_trades']==1
        assert pl.summary()['actual_net']==pytest.approx(2.5)


def test_profit_learning_no_auto_opposite_after_loss():
    d=label_from_profit('BUY',-1.0,entry=100,exit_level=99,spread=0.1)
    assert d['label']==2
    assert d['mode'] in ('loss_hold_default','loss_abstain_hold')
    win=label_from_profit('SELL',1.5,entry=100,exit_level=99)
    assert win['label']==1 and win['mode']=='reinforce_win'
    flip=label_from_profit('SELL',-0.5,flip_after_loss=True)
    assert flip['label']==2 and flip['mode']=='chop_hold_after_flip_loss'
    assert hypothetical_pnl('BUY',100,101,spread=0.2)<1.0


def test_model_validation_promote_and_rollback_stack():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        mv=ModelValidator(store)
        bad=compare_models([.1]*30,[-.2]*30)
        assert bad['promote'] is False
        good=compare_models([.05]*30,[.2]*30)
        assert good['promote'] is True
        mv.push_version({'model_version':1});mv.push_version({'model_version':2})
        prev=mv.mark_rollback('test')
        assert prev and prev['model_version']==1


def test_engine_wires_self_dev_modules():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        e=SingleTradeEngine(store,brain)
        assert e.feature_registry.ohlc_dim()==FEATURE_DIM
        assert e.feature_discovery is not None
        assert e.profit_learning.summary()['trades']==0
        snap=e.snapshot()
        assert 'self_development' in snap
        assert snap['neural']['input_dimension']==brain.core.d


def test_shadow_is_independent_core_not_halved_losses():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        e=SingleTradeEngine(store,brain)
        shadow=e._ensure_shadow_brain()
        assert shadow is not None and shadow is not brain.core
        assert shadow.d==brain.core.d
        # Fill broker £ history with shadow actions for compare path.
        for i in range(30):
            e.profit_learning.history.append({
                'actual_pnl':0.1 if i%3 else -0.05,
                'direction':'BUY','shadow_action':'HOLD' if i%2 else 'BUY'})
        e.profit_learning._save()
        from app.autonomy import AutoEvolution
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        out=e.policy.shadow_self_dev_tick(e)
        assert out is not None
        assert store.get('shadow_self_dev_last')['method']=='independent_core_broker_pnl'
