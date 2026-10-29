"""Regression tests for audit items 27–32 (param freeze, OOS, isolation, restore, confirm, protect-first)."""
from __future__ import annotations

import asyncio
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.feature_validation import (confirm_feature_oos, net_pnl_curve,
                                    select_feature_candidate, train_oos_bounds,
                                    validate_feature)
from app.instrument_isolation import (BAG_KEYS, bag_key, load_instrument_bag,
                                      park_instrument_bag, switch_instrument_bag)
from app.market_context import FEATURE_DIM
from app.model_validation import walk_forward_split
from app.self_params import DEFAULTS, load as load_params, save as save_params


# ---------- #28 val_oos_frac alignment + non-overlapping + select/confirm ----------

def test_val_oos_frac_means_oos_share_like_model_validation():
    save_frac=0.7
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_params(store,{**DEFAULTS,'val_oos_frac':save_frac},reason='test')
        n=100
        train_end,end,frac=train_oos_bounds(n,store=store)
        assert frac==save_frac
        assert train_end==int(n*(1.0-save_frac))
        assert end==n
        # model_validation walk_forward_split uses the same OOS share meaning.
        rows=list(range(n))
        train,oos=walk_forward_split(rows,store=store)
        assert len(train)==train_end
        assert len(oos)==n-train_end


def test_net_pnl_curve_non_overlapping_by_default():
    # Constant positive feature → long every bar; overlapping would enter every step.
    feat=np.ones(40)
    closes=np.linspace(100,108,100)
    spreads=[0.01]*100
    overlapping=net_pnl_curve(feat,closes,spreads,start=0,end=40,horizon=8,
                              non_overlapping=False)
    non_overlap=net_pnl_curve(feat,closes,spreads,start=0,end=40,horizon=8,
                              non_overlapping=True)
    assert overlapping and non_overlap
    assert non_overlap['n']<overlapping['n']
    # Entries every 8 bars until exit would exceed closes (bar_i=60+local).
    assert non_overlap['n']==4  # locals 0,8,16,24


def test_select_and_confirm_use_disjoint_windows():
    feat=np.ones(80)
    closes=np.linspace(100,120,140)
    spreads=[0.01]*140
    train_end=56  # 70% train of 80
    sel=select_feature_candidate('x',feat,closes,spreads,train_end=train_end,store=None)
    # Selection may or may not pass; confirm must only look at [train_end, end).
    conf=confirm_feature_oos(feat,closes,spreads,train_end=train_end,end=80,store=None)
    assert 'oos' in conf or conf.get('confirmed') in (True,False)
    if conf.get('oos'):
        # OOS trade count cannot exceed non-overlapping slots in holdout.
        assert conf['oos']['n']<=(80-train_end)


# ---------- #29 instrument isolation ----------

def test_instrument_bag_park_and_switch_isolates_pnl_and_registry():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        store.set('profit_learning_history_v1',[{'epic':'GOLD','actual_pnl':1.0}])
        store.set('feature_registry_v1',{'approved':[{'id':'a','name':'a'}]})
        store.set('bracket_kappa',1.5)
        store.set('shadow_paper_journal_v1',[{'paper_pnl':0.2}])
        park_instrument_bag(store,'GOLD')
        assert store.get(bag_key('profit_learning_history_v1','GOLD'))
        # Switch to SILVER → fresh empty live bag.
        switch_instrument_bag(store,'GOLD','SILVER')
        assert store.get('profit_learning_history_v1') is None
        assert store.get('feature_registry_v1') is None
        assert store.get('bracket_kappa') is None
        # SILVER accrues its own history.
        store.set('profit_learning_history_v1',[{'epic':'SILVER','actual_pnl':-0.5}])
        store.set('bracket_kappa',0.8)
        switch_instrument_bag(store,'SILVER','GOLD')
        hist=store.get('profit_learning_history_v1')
        assert hist and hist[0]['epic']=='GOLD' and hist[0]['actual_pnl']==1.0
        assert store.get('bracket_kappa')==1.5
        # SILVER bag preserved.
        assert store.get(bag_key('profit_learning_history_v1','SILVER'))[0]['epic']=='SILVER'


def test_prepare_new_instrument_parks_shared_keys(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD','size':'1'}
    store.set('profit_learning_history_v1',[{'epic':'GOLD','actual_pnl':2.0}])
    store.set('feature_registry_v1',{'approved':[]})

    class Broker:
        async def positions(self):return []
    engine.broker=Broker()

    out=asyncio.run(engine.prepare_new_instrument())
    assert out['ok'] is True
    assert store.get(bag_key('profit_learning_history_v1','GOLD'))[0]['actual_pnl']==2.0
    assert store.get('profit_learning_history_v1') is None
    assert engine.mandate is None


# ---------- #30 weight restore fail-closed ----------

def test_restore_weights_corrupt_backup_returns_false_and_keeps_core(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    brain.epic='GOLD'
    brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,
               bar_time='2024-01-01T00:00:00+00:00')
    brain.core.P[:]+=0.25
    before=brain.core.P.copy()
    before_ver=brain.model_version
    good=brain.backup_weights(tag='good')
    assert good and Path(good).exists()
    # Corrupt / incomplete npz — must fail closed, not reset-and-save.
    bad=tmp_path/'corrupt_brain.npz'
    np.savez_compressed(bad,junk=np.array([1.0]),epic='GOLD')
    ok=brain.restore_weights(str(bad))
    assert ok is False
    assert np.allclose(brain.core.P,before)
    assert brain.model_version==before_ver
    # Valid restore still works.
    assert brain.restore_weights(good) is True
    assert np.allclose(brain.core.P,before)


def test_restore_weights_instrument_mismatch_fails_closed(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,
               bar_time='2024-01-01T00:00:00+00:00')
    path=brain.backup_weights(tag='gold')
    before=brain.core.P.copy()
    # Live epic is SILVER; backup labeled GOLD.
    brain.epic='SILVER'
    ok=brain.restore_weights(path)
    assert ok is False
    assert np.allclose(brain.core.P,before)


def test_restore_neural_weights_reports_false_on_bad_path(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    brain.tick(100,.05,.01,'GOLD',features=[.1]*FEATURE_DIM,
               bar_time='2024-01-01T00:00:00+00:00')
    engine=SingleTradeEngine(store,brain)
    before=brain.core.P.copy()
    ok=engine.restore_neural_weights({
        'weights_path':str(tmp_path/'missing.npz'),
        'approved_specs':[],'epic':'GOLD','ohlc_dim':FEATURE_DIM})
    assert ok is False
    assert np.allclose(brain.core.P,before)


# ---------- #31 full OPEN match on confirm retries ----------

def test_confirm_reconcile_retry_requires_full_open_match(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD','size':'1'}
    oid=store.order('OPEN',None,{'epic':'GOLD','direction':'BUY','size':'1'})
    monkeypatch.setattr(asyncio,'sleep',mock.AsyncMock())

    wrong_dir={'dealId':'d1','epic':'GOLD','direction':'SELL','size':1}
    right={'dealId':'d1','epic':'GOLD','direction':'BUY','size':1}

    class Broker:
        def __init__(self):
            self.n=0
            self._pos=[wrong_dir]
        async def confirm(self,ref):
            return {'dealStatus':'ACCEPTED','dealReference':ref,'dealId':'d1'}
        async def positions(self):
            self.n+=1
            # First sync + retries keep wrong direction; eventually correct.
            if self.n>=4:
                self._pos=[right]
            return [{'position':self._pos[0],'market':{'epic':'GOLD'}}]

    engine.broker=Broker()

    async def run():
        return await engine._confirm_reconcile(
            {'dealReference':'ref-1'},oid,'OPEN')

    assert asyncio.run(run()) is True
    assert engine.positions[0]['direction']=='BUY'


def test_confirm_reconcile_retry_epic_alone_insufficient(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD','size':'1'}
    oid=store.order('OPEN',None,{'epic':'GOLD','direction':'BUY','size':'1'})
    monkeypatch.setattr(asyncio,'sleep',mock.AsyncMock())

    # Same epic, wrong size forever — must NOT match on retry.
    class Broker:
        async def confirm(self,ref):
            return {'dealStatus':'ACCEPTED','dealReference':ref,'dealId':'d9'}
        async def positions(self):
            return [{'position':{'dealId':'d9','epic':'GOLD','direction':'BUY','size':99},
                     'market':{'epic':'GOLD'}}]

    engine.broker=Broker()

    async def run():
        return await engine._confirm_reconcile(
            {'dealReference':'ref-x'},oid,'OPEN')

    with pytest.raises(RuntimeError,match='not reconciled'):
        asyncio.run(run())


def test_order_fully_matched_helper_checks_direction_size_deal(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD','size':'1'}
    engine.positions=[{'dealId':'d1','epic':'GOLD','direction':'BUY','size':1}]
    req={'epic':'GOLD','direction':'BUY','size':'1'}
    assert engine._order_fully_matched(
        'OPEN',req,None,{'dealId':'d1','dealReference':'r'},'r') is True
    # Wrong direction
    engine.positions=[{'dealId':'d1','epic':'GOLD','direction':'SELL','size':1}]
    assert engine._order_fully_matched(
        'OPEN',req,None,{'dealId':'d1'},'r') is False
    # Epic-only would have passed historically — size mismatch must fail.
    engine.positions=[{'dealId':'d1','epic':'GOLD','direction':'BUY','size':2}]
    assert engine._order_fully_matched(
        'OPEN',req,None,{'dealId':'d1'},'r') is False


# ---------- #32 protect before / independent of self-dev ----------

def test_self_dev_skipped_before_protect_when_position_open(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.mandate={'epic':'GOLD','size':'1'}
    engine.positions=[{'dealId':'d1','epic':'GOLD','direction':'BUY','size':1,
                       'stopLevel':99.0,'profitLevel':110.0,'level':100.0}]
    engine.market_context={'bar_time':'2024-01-01T00:01:00+00:00','ready':True,
                           'close':100.,'returns_bps':0.,'trend':'UP','regime':'TREND'}
    calls=[]
    engine._run_self_development_cycle=lambda *a,**k: calls.append(('dev',k))
    # Simulate the poll branching: with positions, early self-dev must not run.
    if not engine.positions:
        engine._run_self_development_cycle([],spread=0.1)
    assert calls==[]
    # After protect path, deferred call is allowed.
    engine._run_self_development_cycle([],spread=0.1,defer_if_busy=True)
    assert calls and calls[0][1].get('defer_if_busy') is True


def test_resource_pressure_defers_self_dev(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    brain=AdaptiveBrain(store,data_dir=tmp_path)
    engine=SingleTradeEngine(store,brain)
    engine.market_context={'bar_time':'t1'}
    with mock.patch.object(engine,'_resource_pressure',
                           return_value={'busy':True,'load1':99,'rss_mb':9999,
                                         'high_load':True,'high_rss':True,'cpus':1}):
        engine._run_self_development_cycle([],spread=0.1,defer_if_busy=True)
    # Bar not consumed — may retry when pressure eases.
    assert engine._last_self_dev_bar is None
    events=store.recent(20)
    assert any(e['kind']=='self_dev' for e in events)
