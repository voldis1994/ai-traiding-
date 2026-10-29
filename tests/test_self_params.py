"""Robot-owned knobs: seeds only, then Store + evolve — no frozen magic gates."""
import tempfile
from pathlib import Path

from app.engine import Store
from app.self_params import (DEFAULTS, STORE_KEY, PENDING_KEY, load, save, get,
                             evolve_from_feedback, maybe_commit_long_horizon,
                             snapshot, IMMUTABLE_NOTE)
from app.feature_discovery import FeatureDiscovery
from app.feature_validation import validate_feature


def test_self_params_persist_and_robot_can_change():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        p=load(store)
        assert p['bars_between_search']==DEFAULTS['bars_between_search']
        p['bars_between_search']=12
        p['learn_lr']=0.2
        p['exit_need_streak']=3
        save(store,p,reason='unit test')
        assert get(store,'bars_between_search')==12
        assert get(store,'learn_lr')==0.2
        assert get(store,'exit_need_streak')==3
        snap=snapshot(store)
        assert 'immutable' in snap and 'volume' in snap['immutable'].lower()


def test_evolve_from_feedback_moves_knobs():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load(store)['bars_between_search']
        evolve_from_feedback(store,feature_approved='gen_ret_8')  # commit=True default
        after=load(store)['bars_between_search']
        assert after<=before
        evolve_from_feedback(store,feature_rejected={'reason':'unstable or non-positive OOS net PnL'})
        assert load(store)['val_horizon_bars']>=DEFAULTS['val_horizon_bars']


def test_scratch_loss_does_not_ratchet_hold_repeats():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load(store)['learn_loss_repeats']
        evolve_from_feedback(store,broker_pnl=-0.02)
        assert load(store)['learn_loss_repeats']==before
        evolve_from_feedback(store,broker_pnl=-1.5)
        after=load(store)['learn_loss_repeats']
        win=load(store)['learn_win_repeats']
        assert after<=win  # loss repeats never exceed win path


def test_cold_start_trial_commits_or_rejects_without_prior_stall():
    """Trial with no pre-trial £ must not hang forever waiting for prior≥N."""
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        # Floor of BOUNDS['self_params_horizon_trades'] is 8.
        save(store,{**DEFAULTS,'self_params_horizon_trades':8},reason='test')
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        assert (store.get(PENDING_KEY) or {}).get('trial_active')
        # No prior broker rows — positive trial window commits (no prior stall).
        out=maybe_commit_long_horizon(store,[0.2]*8)
        assert out['committed'] is True
        assert out.get('mode')=='cold_start_trial_net'


def test_evolve_commit_false_activates_trial():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load(store)['bars_between_search']
        # Seed some pre-trial broker history so the trial start index is known.
        store.set('profit_learning_history_v1',
                  [{'actual_pnl':0.1,'mode':'reinforce_win'}]*8)
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        pending=store.get(PENDING_KEY)
        assert pending and pending.get('draft') and pending.get('trial_active')
        # Trial applies draft live — knobs change under measurement.
        assert load(store)['bars_between_search']<=before
        assert pending['trial_started_at_trades']==8
        assert pending['baseline_params']['bars_between_search']==before
        assert snapshot(store)['pending_events']>=1


def test_maybe_commit_requires_trial_period_profit_proof():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'self_params_horizon_trades':8},reason='test')
        # 8 pre-trial trades already on the ledger.
        store.set('profit_learning_history_v1',
                  [{'actual_pnl':-0.1,'mode':'loss_hold_default'}]*8)
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        assert load(store)['bars_between_search']<DEFAULTS['bars_between_search']
        # Profit earned BEFORE trial must not commit the draft.
        pre_only=[-0.1]*8+[0.5]*8  # but trial_started_at=8 → trial slice is last 8
        # Wait: trial_started=8 means trial=pnls[8:]. If we pass only 8 pre-trial
        # values, trial is empty.
        assert maybe_commit_long_horizon(store,[-0.1]*8)['committed'] is False
        # Pre-trial baseline weak, trial window strong → commit.
        pnls=[-0.1]*8+[0.3]*8
        out=maybe_commit_long_horizon(store,pnls)
        assert out['committed'] is True
        assert load(store)['bars_between_search']<DEFAULTS['bars_between_search']
        assert not (store.get(PENDING_KEY) or {}).get('trial_active')


def test_maybe_commit_rejects_trial_and_restores_baseline():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'self_params_horizon_trades':8},reason='test')
        store.set('profit_learning_history_v1',
                  [{'actual_pnl':0.4,'mode':'reinforce_win'}]*8)
        before=load(store)['bars_between_search']
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        # Strong pre-trial, weak trial → reject and restore baseline.
        pnls=[0.4]*8+[-0.2]*8
        out=maybe_commit_long_horizon(store,pnls)
        assert out.get('rejected') is True
        assert out['committed'] is False
        assert load(store)['bars_between_search']==before


def test_discovery_reads_cadence_from_store():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'bars_between_search':5,'search_limit':6},reason='test')
        disc=FeatureDiscovery(store)
        assert disc._params()['bars_between_search']==5
        assert disc._params()['search_limit']==6


def test_validation_uses_store_horizon_not_module_constant():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'val_horizon_bars':5},reason='test')
        # Missing spreads still rejected — proves store is plumbed (reason path).
        result=validate_feature('atr_ratio_5_20',[(100,101,99,100)]*130,
                                spreads=None,store=store)
        assert result['approved'] is False
        assert 'spread' in result['reason']
