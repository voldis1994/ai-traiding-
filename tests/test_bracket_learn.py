"""Bracket learning: lot+market is the risk frame; SL/TP shape learns from cash P&L."""
import tempfile
from pathlib import Path

from app.bracket_learn import (
    DEFAULT_KAPPA,DEFAULT_SL_SCALE,evolve_from_history,load_params,
    propose_for_open,record_outcome,save_params,
)
from app.engine import Store
from app.math_rules import stop_take


def sandbox():
    return tempfile.TemporaryDirectory()


def test_stop_take_broker_min_only_no_20_tick_invent():
    # Without min_distance, distance is buffer only (2*tick), not 20 ticks.
    bare=stop_take(100,'BUY',0.01,0.01,0,kappa=2,sl_scale=1)
    assert bare['stop_ticks']==2
    assert bare['profit_ticks']==4
    # Learned wider stop stays >= broker floor.
    wide=stop_take(100,'BUY',0.01,0.01,0.15,kappa=1.5,sl_scale=2)
    assert wide['stop_ticks']>=17  # (0.15+0.02)*2 / 0.01
    assert wide['sl_scale']==2.0
    assert wide['kappa']==1.5


def test_propose_explores_then_evolve_promotes_better_cash():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        assert load_params(store)=={'kappa':DEFAULT_KAPPA,'sl_scale':DEFAULT_SL_SCALE}
        # Force exploration of a worse and a better cell via tagged outcomes.
        for _ in range(4):
            record_outcome(store,kappa=2.0,sl_scale=1.0,pnl=-0.5,currency='GBP')
        for _ in range(4):
            record_outcome(store,kappa=3.0,sl_scale=1.5,pnl=1.2,currency='GBP')
        promoted=evolve_from_history(store,min_samples=8)
        assert promoted is not None
        assert promoted['kappa']==3.0 and promoted['sl_scale']==1.5
        assert load_params(store)['kappa']==3.0
        # With enough history and non-explore cadence, propose returns champion.
        class FixedRng:
            def choice(self,seq):return seq[0]
        p=propose_for_open(store,explore_every=99,rng=FixedRng())
        assert p['kappa']==3.0 and p['sl_scale']==1.5 and p['explored'] is False


def test_save_and_load_reject_invalid():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_params(store,4.0,2.5,reason='manual')
        assert load_params(store)=={'kappa':4.0,'sl_scale':2.5}
        store.set('bracket_kappa',-1)
        store.set('bracket_sl_scale',0.5)
        restored=load_params(store)
        assert restored['kappa']==DEFAULT_KAPPA
        assert restored['sl_scale']==DEFAULT_SL_SCALE
