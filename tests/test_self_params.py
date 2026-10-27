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


def test_evolve_commit_false_queues_without_saving():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load(store)['bars_between_search']
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        assert load(store)['bars_between_search']==before
        pending=store.get(PENDING_KEY)
        assert pending and pending.get('draft')
        assert pending['draft']['bars_between_search']<=before
        assert snapshot(store)['pending_events']>=1


def test_maybe_commit_requires_long_horizon_profit_proof():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'self_params_horizon_trades':8},reason='test')
        evolve_from_feedback(store,feature_approved='gen_ret_8',commit=False)
        # Too few samples → no commit.
        assert maybe_commit_long_horizon(store,[0.1]*4)['committed'] is False
        # Negative long horizon → no commit.
        assert maybe_commit_long_horizon(store,[-0.2]*10)['committed'] is False
        # Improving positive windows → commit draft.
        pnls=[-0.1]*8+[0.3]*8
        out=maybe_commit_long_horizon(store,pnls)
        assert out['committed'] is True
        assert load(store)['bars_between_search']<DEFAULTS['bars_between_search']
        assert not (store.get(PENDING_KEY) or {}).get('draft')


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
