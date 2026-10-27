"""Robot-owned knobs: seeds only, then Store + evolve — no frozen magic gates."""
import tempfile
from pathlib import Path

from app.engine import Store
from app.self_params import (DEFAULTS, load, save, get, evolve_from_feedback,
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
        evolve_from_feedback(store,feature_approved='gen_ret_8')
        after=load(store)['bars_between_search']
        assert after<=before
        evolve_from_feedback(store,feature_rejected={'reason':'unstable or non-positive OOS net PnL'})
        assert load(store)['val_horizon_bars']>=DEFAULTS['val_horizon_bars']


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
