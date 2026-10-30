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
        peers=[.1]*6;news=[.2,.3];liq=[.0]*6
        # Manual compose matching fusion_features contract (peers+news+liquidity).
        vec=ohlc+peers+news+liq
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


def test_registry_shrink_realigns_brain_to_live_features():
    """After feature wipe, brain must shrink — else fusion gap 13!=14 forever."""
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,bar_time=BASE.isoformat())
        reg=FeatureRegistry(store)
        reg.approve({'id':'gap_open_1m','name':'gap_open_1m','formula':'gap','source':'t'})
        brain.apply_registry(reg)
        assert brain.core.d==FEATURE_DIM+1
        assert brain.ohlc_dim==FEATURE_DIM+1
        # Wipe approved features (as "Mācīties no jauna" may).
        store.set('feature_registry_v1',{'approved':[]})
        empty=FeatureRegistry(store)
        assert empty.ohlc_dim()==FEATURE_DIM
        assert brain.apply_registry(empty) is False
        assert brain.core.d==FEATURE_DIM
        assert brain.ohlc_dim==FEATURE_DIM


def test_fusion_registry_shrink_drops_dynamic_keeps_external():
    """Guntis-style: fusion brain at 14 OHLC + EXTERNAL after wipe → 13 + EXTERNAL."""
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,bar_time=BASE.isoformat())
        brain._extend_to_external_features()
        assert brain.core.d==FUSION_DIM
        reg=FeatureRegistry(store)
        reg.approve({'id':'gap_open_1m','name':'gap_open_1m','formula':'gap','source':'t'})
        brain.apply_registry(reg)
        assert brain.ohlc_dim==FEATURE_DIM+1
        assert brain.core.d==fusion_dim_for(FEATURE_DIM+1)
        store.set('feature_registry_v1',{'approved':[]})
        empty=FeatureRegistry(store)
        brain.apply_registry(empty)
        assert brain.ohlc_dim==FEATURE_DIM
        assert brain.fusion_dim==FUSION_DIM
        assert brain.core.d==FUSION_DIM


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


def test_profit_learning_loss_follows_counterfactual_not_forced_hold():
    # Clear down-move after costs → SELL (opposite); never reinforce losing BUY.
    d=label_from_profit('BUY',-1.0,entry=100,exit_level=99,spread=0.1)
    assert d['label']==1 and d['mode']=='loss_counterfactual_sell'
    # Move inside spread → HOLD beats opposite after costs.
    inside=label_from_profit('BUY',-0.2,entry=100,exit_level=99.95,spread=0.3)
    assert inside['label']==2 and inside['mode']=='loss_counterfactual_hold'
    win=label_from_profit('SELL',1.5,entry=100,exit_level=99)
    assert win['label']==1 and win['mode']=='reinforce_win'
    # Flip flag audit-only; SELL loss into a rise → BUY opposite, not HOLD hardcode.
    flip=label_from_profit('SELL',-0.5,flip_after_loss=True,entry=100,exit_level=101,spread=0.1)
    assert flip['label']==0 and flip['mode']=='flip_loss_counterfactual_buy'
    assert hypothetical_pnl('BUY',100,101,spread=0.2)<1.0


def test_model_validation_promote_and_rollback_stack():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        mv=ModelValidator(store)
        bad=compare_models([.1]*30,[-.2]*30)
        assert bad['promote'] is False
        good=compare_models([.05]*30,[.2]*30)
        assert good['promote'] is True
        # Unequal lengths: better mean £/trade can promote even if total net is lower.
        mean_win=compare_models([0.5]*24,[0.6]*12)
        assert mean_win['promote'] is True
        assert 'mean' in mean_win['reason'].lower()
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
        # LIVE broker £ with entry/exit so cash scale can be estimated.
        for i in range(30):
            e.profit_learning.history.append({
                'actual_pnl':0.5 if i%3 else -0.2,
                'direction':'BUY','entry':100.,'exit':100.5 if i%3 else 99.8})
        e.profit_learning._save()
        # Cash-scaled paper rows (unit=account_currency) — not raw price units.
        for i in range(24):
            e.shadow_paper.history.append({
                'paper_pnl':0.4 if i%4 else -0.15,
                'price_pnl':0.4 if i%4 else -0.15,
                'unit':'account_currency','direction':'SELL','source':'shadow_paper'})
        e.shadow_paper._persist()
        from app.autonomy import AutoEvolution
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        out=e.policy.shadow_self_dev_tick(e)
        assert out is not None
        assert store.get('shadow_self_dev_last')['method']=='independent_shadow_paper_cash'


def test_discovery_rotates_past_rejected_candidates():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        disc=FeatureDiscovery(store)
        bars=make_bars(80)
        first=disc.search(bars[-61:],spread=0.03,bar_time=BASE.isoformat(),limit=3)
        assert first
        for p in first:
            disc.mark_rejected(p['id'],'unit-test')
        second=disc.search(bars[-61:],spread=0.03,
                           bar_time=(BASE+timedelta(minutes=1)).isoformat(),limit=3)
        assert second
        first_ids={p['id'] for p in first}
        second_ids={p['id'] for p in second}
        # Cooldown must push the next search onto other formulas.
        assert first_ids.isdisjoint(second_ids)


def test_historical_matrix_not_tiled_snapshot():
    from app.historical_features import build_feature_matrix
    bars=make_bars(200)
    spreads=varying_spreads(len(bars))
    matrix,names,indices=build_feature_matrix(bars,spreads,step=3,max_rows=40)
    assert matrix is not None and len(indices)==matrix.shape[0]>=20
    # Distinct rows — a tiled snapshot would have near-zero row variance.
    row_std=float(np.std(matrix,axis=0).mean())
    assert row_std>1e-6
    assert len(set(indices))==len(indices)


def test_weight_backup_and_restore_reloads_neural_core():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,bar_time=BASE.isoformat())
        brain.core.P[:]+=0.5
        brain.model_version=3
        path=brain.backup_weights(tag='unit')
        assert path and Path(path).exists()
        altered=brain.core.P.copy()
        brain.core.P[:]-=1.0
        assert not np.allclose(brain.core.P,altered)
        ok=brain.restore_weights(path)
        assert ok and np.allclose(brain.core.P,altered)
        mv=ModelValidator(store)
        mv.push_version({'model_version':3,'weights_path':path,'approved_specs':[]})
        brain.core.P[:]-=2.0
        mv.push_version({'model_version':4,'weights_path':brain.backup_weights(tag='v4'),
                         'approved_specs':[]})
        e=SingleTradeEngine(store,brain)
        prev=mv.mark_rollback('unit',restore_fn=e.restore_neural_weights)
        assert prev and prev.get('weights_restored') is True
        assert np.allclose(brain.core.P,altered)


def test_rollback_restores_feature_registry_with_weights():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        brain=AdaptiveBrain(store,data_dir=d)
        brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,bar_time=BASE.isoformat())
        e=SingleTradeEngine(store,brain)
        # v3: no dynamic features
        path_v3=brain.backup_weights(tag='v3')
        e.model_validator.push_version({
            'model_version':3,'weights_path':path_v3,'approved_specs':[],
            'ohlc_dim':FEATURE_DIM})
        # Approve a feature and expand (v4)
        e.feature_registry.approve({
            'id':'session_phase_utc','name':'session_phase_utc','formula':'sin','source':'t'})
        brain.apply_registry(e.feature_registry)
        assert brain.ohlc_dim==FEATURE_DIM+1
        path_v4=brain.backup_weights(tag='v4')
        e.model_validator.push_version({
            'model_version':4,'weights_path':path_v4,
            'approved_specs':[dict(x) for x in e.feature_registry.approved],
            'ohlc_dim':brain.ohlc_dim})
        prev=e.model_validator.mark_rollback(
            'registry+weights',restore_fn=e.restore_neural_weights)
        assert prev and prev.get('weights_restored') and prev.get('registry_restored')
        assert e.feature_registry.approved_ids()==[]
        assert brain.ohlc_dim==FEATURE_DIM
        assert brain.core.d==FEATURE_DIM


def test_shadow_paper_journal_independent_of_live():
    from app.shadow_paper import ShadowPaperJournal, cash_per_price_unit
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        j=ShadowPaperJournal(store)
        # Ideal fills for unit-path asserts; production uses non-zero exec_* defaults.
        z=dict(slip_bps=0.,fill_delay_bars=0)
        # Without cash scale → price_pnl only, paper_pnl (cash) is None.
        # Open with explicit SL/TP distances (LIVE-identical rules — no flip exit).
        assert j.on_bar('BUY',mid=100.,spread=0.2,bar_time='t1',
                        stop_distance=2.,profit_distance=4.,tick=0.1,**z) is None
        # Opposite SELL vote must NOT close (LIVE = SL/TP/trail only).
        assert j.on_bar('SELL',mid=101.,spread=0.2,bar_time='t2',
                        high=101.5,low=100.5,
                        stop_distance=2.,profit_distance=4.,tick=0.1,**z) is None
        assert j.position is not None
        # SL hit closes with exit_reason sl (never flip).
        closed=j.on_bar('HOLD',mid=97.5,spread=0.2,bar_time='t3',
                        high=98.,low=97.,tick=0.1,**z)
        assert closed is not None and closed['source']=='shadow_paper'
        assert closed['exit_reason'] in ('sl','trail')
        assert closed['paper_pnl'] is None
        assert closed['unit']=='price'
        assert j.paper_cash_pnls()==[]
        # With scale from LIVE broker rows → cash comparable to £.
        live=[{'actual_pnl':2.0,'entry':100.,'exit':101.,'direction':'BUY'}]*6
        scale=cash_per_price_unit(live,min_samples=5)
        assert scale==pytest.approx(2.0)
        j2=ShadowPaperJournal(store)
        j2.on_bar('BUY',mid=100.,spread=0.,bar_time='a',cash_scale=scale,
                  stop_distance=1.,profit_distance=2.,tick=0.1,**z)
        assert j2.on_bar('HOLD',mid=100.5,spread=0.,bar_time='a2',cash_scale=scale,
                         high=100.8,low=100.2,tick=0.1,**z) is None
        c2=j2.on_bar('HOLD',mid=102.5,spread=0.,bar_time='b',cash_scale=scale,
                     high=103.,low=102.,tick=0.1,**z)
        assert c2 is not None
        assert c2['exit_reason']=='tp'
        assert c2['unit']=='account_currency'
        assert j2.paper_cash_pnls()
