"""Paaudze tracks real brain advances, not shadow policy-file churn."""
import tempfile
from pathlib import Path

from app.engine import Store
from app.learning_generation import bump, counters, current, display
from app.self_params import DEFAULTS, save, evolve_from_feedback, maybe_commit_long_horizon


def test_bump_advances_and_display_ignores_policy_file_gen():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        assert current(store)==0
        # Policy generation must NOT inflate Paaudze.
        assert display(store,policy_generation=526)==0
        assert bump(store,'feature_registry_expanded')==1
        assert bump(store,'bracket_shape_promoted')==2
        assert current(store)==2
        assert display(store,policy_generation=999)==2


def test_separate_counters_for_kinds():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        bump(store,'closed_trade_training_step',kind='training_steps')
        bump(store,'feature_registry_expanded',kind='architecture_changes')
        bump(store,'self_params_trial_committed',kind='param_changes')
        bump(store,'shadow_weights_promoted_to_live',kind='verified_candidate_promotes',
             weight_checksum='abc123')
        tallies=counters(store)
        assert tallies['training_steps']==1
        assert tallies['architecture_changes']==1
        assert tallies['param_changes']==1
        assert tallies['verified_candidate_promotes']==1
        assert current(store)==4


def test_identical_weight_recopy_does_not_bump():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        assert bump(store,'shadow_weights_promoted_to_live',
                    kind='verified_candidate_promotes',
                    weight_checksum='same-brain')==1
        # Identical checksum → no Paaudze advance, counter stays 1.
        assert bump(store,'shadow_weights_promoted_to_live',
                    kind='verified_candidate_promotes',
                    weight_checksum='same-brain')==1
        assert current(store)==1
        assert counters(store)['verified_candidate_promotes']==1
        # New checksum → advances again.
        assert bump(store,'shadow_weights_promoted_to_live',
                    kind='verified_candidate_promotes',
                    weight_checksum='new-brain')==2
        assert counters(store)['verified_candidate_promotes']==2


def test_reason_auto_maps_to_counter_bucket():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        bump(store,'feature_registry_expanded')
        bump(store,'bracket_shape_promoted')
        bump(store,'m1_horizon_learn_batch')
        tallies=counters(store)
        assert tallies['architecture_changes']==1
        assert tallies['param_changes']==1
        assert tallies['training_steps']==1


def test_self_params_commit_bumps_generation():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'self_params_horizon_trades':8},reason='test')
        store.set('profit_learning_history_v1',
                  [{'actual_pnl':-0.1,'mode':'loss_counterfactual_hold'}]*8)
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        assert current(store)==0
        out=maybe_commit_long_horizon(store,[-0.1]*8+[0.3]*8)
        assert out['committed'] is True
        assert current(store)==1
        assert counters(store)['param_changes']==1
