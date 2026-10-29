"""Paaudze tracks real brain advances, not shadow policy-file churn."""
import tempfile
from pathlib import Path

from app.engine import Store
from app.learning_generation import bump, current, display
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
