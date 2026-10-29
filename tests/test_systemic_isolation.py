"""Systemic multi-tenant isolation guards beyond seed/API-key uniqueness."""
from pathlib import Path

import numpy as np
import pytest

from app.core import client_neural_seed
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.feature_discovery import FeatureDiscovery, _combinatorial_ids, _build_library
from app.runtime_hub import AccountRuntime, RuntimeHub
from app.stream import CapitalQuoteStream


def test_policy_defaults_next_to_store_not_global_data(tmp_path):
    d=tmp_path/'accounts'/'client_9';d.mkdir(parents=True)
    store=Store(d/'robot.sqlite3')
    brain=AdaptiveBrain(store,data_dir=d,seed=client_neural_seed(9))
    engine=SingleTradeEngine(store,brain)
    assert engine.policy.path==d/'policy.py'
    assert engine.policy.path.exists()


def test_shadow_seed_follows_client_init_seed(tmp_path):
    da=tmp_path/'a';db=tmp_path/'b';da.mkdir();db.mkdir()
    s1=Store(da/'robot.sqlite3');b1=AdaptiveBrain(s1,data_dir=da,seed=client_neural_seed(1))
    s2=Store(db/'robot.sqlite3');b2=AdaptiveBrain(s2,data_dir=db,seed=client_neural_seed(2))
    e1=SingleTradeEngine(s1,b1);e2=SingleTradeEngine(s2,b2)
    sh1=e1._ensure_shadow_brain();sh2=e2._ensure_shadow_brain()
    assert sh1.seed!=sh2.seed
    assert not np.allclose(sh1.P,sh2.P)


def test_feature_search_scopes_gen_ids_to_own_horizons(tmp_path,monkeypatch):
    d1=tmp_path/'c1';d1.mkdir()
    s1=Store(d1/'robot.sqlite3')
    # Poison the process library with an alien horizon first.
    _build_library(horizons=(3,5,99))
    assert 'gen_ret_99' in _combinatorial_ids((3,5,99))
    fd=FeatureDiscovery(s1)
    monkeypatch.setattr(fd,'_params',lambda:{
        'gen_horizons':(3,5,8),'search_limit':50,'max_proposals_kept':20,
        'bars_between_search':1})
    # Realistic OHLC window so combinatorial formulas return values.
    bars=[]
    px=100.0
    for i in range(80):
        o=px;h=px+0.4;l=px-0.3;c=px+((i%5)-2)*0.05;px=c
        bars.append((o,h,l,c))
    found=fd.search(bars,spread=0.01,bar_time='2024-01-01T00:00:00Z')
    ids={p['id'] for p in found}
    assert 'gen_ret_99' not in ids
    # Should still be able to propose something from own horizons / seeds.
    assert found


def test_can_rearm_helper_respects_mismatch():
    class E:
        _paused_for_read=True
        mandate={'epic':'GOLD'}
        operator_paused=False
        _paused_for_mismatch=True
        _paused_for_multi=False
        store=type('S',(),{'unresolved':staticmethod(lambda: [])})()
        positions=[]
    assert RuntimeHub._can_rearm_after_read(E()) is False
    E._paused_for_mismatch=False
    assert RuntimeHub._can_rearm_after_read(E()) is True


def test_client_view_hydrates_scores_from_journal_after_restart(tmp_path):
    """Admin 0% BUY/HOLD/SELL after update — restore last journaled vote for display."""
    d=tmp_path/'client_1';d.mkdir()
    store=Store(d/'robot.sqlite3')
    brain=AdaptiveBrain(store,data_dir=d,seed=11)
    engine=SingleTradeEngine(store,brain,policy_path=d/'policy.py')
    engine.mandate={'epic':'GOLD','size':'0.1'}
    engine.last_signal=None
    # Mimic a prior closed-candle vote still in SQLite after process restart.
    store.db.execute(
        'INSERT INTO signals(time,epic,action,scores,price,model_step,label) VALUES(?,?,?,?,?,?,?)',
        ('2024-01-01T00:00:00+00:00','GOLD','BUY',
         '{"BUY":0.55,"HOLD":0.20,"SELL":0.25}',100.0,10,None))
    store.db.commit()
    rt=AccountRuntime(client_id=1,account_id=1,data_dir=d,store=store,brain=brain,
                      engine=engine,stream=CapitalQuoteStream(),started=True)
    view=rt.client_view()
    assert view['action']=='BUY'
    assert view['scores']['BUY']==pytest.approx(0.55)
    assert view['signal_from_journal'] is True
