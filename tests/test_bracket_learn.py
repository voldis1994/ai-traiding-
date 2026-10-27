"""Bracket learning: lot+market is the risk frame; SL/TP shape learns from cash P&L."""
import tempfile
from pathlib import Path

from app.bracket_learn import (
    DEFAULT_KAPPA,DEFAULT_SL_SCALE,EXPLORE_KAPPA,evolve_from_history,load_params,
    propose_for_open,recent_reachable_move,record_outcome,save_params,
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
        assert DEFAULT_KAPPA==1.0  # cold start is 1:1, not frozen 1:2
        for _ in range(6):
            record_outcome(store,kappa=2.0,sl_scale=1.0,pnl=-0.5,currency='GBP')
        for _ in range(6):
            record_outcome(store,kappa=3.0,sl_scale=1.5,pnl=1.2,currency='GBP')
        promoted=evolve_from_history(store,min_samples=6)
        assert promoted is not None
        assert promoted['kappa']==3.0 and promoted['sl_scale']==1.5
        assert load_params(store)['kappa']==3.0
        class FixedRng:
            def choice(self,seq):return seq[0]
        p=propose_for_open(store,explore_every=99,rng=FixedRng())
        assert p['kappa']==3.0 and p['sl_scale']==1.5 and p['explored'] is False


def test_stale_kappa_two_without_history_resets_toward_one_to_one():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        store.set('bracket_kappa',2.0)  # leftover frozen 1:2
        store.set('bracket_sl_scale',1.0)
        p=propose_for_open(store,explore_every=99,ohlc_kappa=1.25)
        # Either OHLC hint saved as champion seed, or explore — never silent κ=2 lock.
        assert p['kappa']!=2.0 or p['explored'] is True


def test_explore_stays_inside_recent_ohlc_zone_not_fantasy_kappa():
    """κ=6/8 must not win when recent M1 bars never printed that TP."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        class FixedRng:
            def choice(self,seq):
                # Prefer the largest candidate — still must be zone-feasible.
                return max(seq,key=lambda ks:ks[0]*ks[1])
        # base_sl=2 → κ=6*sl=1 would need TP=12, but max_tp=5 blocks it.
        p=propose_for_open(store,rng=FixedRng(),ohlc_kappa=1.0,
                           base_sl=2.0,max_tp=5.0)
        assert p['explored'] is True
        assert p['kappa'] in EXPLORE_KAPPA
        assert p['kappa']*p['sl_scale']*2.0 <= 5.0*1.05+1e-9
        assert p['kappa']<=3.0  # fantasy 6/8 not in live explore grid


def test_unreachable_champion_clamped_to_zone():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_params(store,6.0,4.0,reason='old_fantasy')
        for _ in range(12):  # enough history so force-explore softens
            record_outcome(store,kappa=1.0,sl_scale=1.0,pnl=0.1,currency='GBP')
        p=propose_for_open(store,explore_every=99,ohlc_kappa=1.25,
                           base_sl=2.0,max_tp=4.0)
        assert p['kappa']*p['sl_scale']*2.0 <= 4.0*1.05+1e-9


def test_recent_reachable_move_from_candles():
    rows=[{'high':100+i*0.1,'low':100} for i in range(10)]
    rows.append({'high':108,'low':100})  # one large bar
    assert recent_reachable_move(rows,n=60)==8.0 or recent_reachable_move(rows,n=60)>=6.8


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
