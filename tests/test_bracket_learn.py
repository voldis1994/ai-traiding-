"""SL/TP from OHLC at open; cash P&L teaches when to raise / cut (audit #19)."""
import tempfile
from pathlib import Path

import pytest

from app.bracket_learn import (
    DEFAULT_KAPPA,DEFAULT_SL_SCALE,adapt_after_outcome,evolve_from_history,
    load_multipliers,load_params,propose_for_open,recent_reachable_move,
    record_outcome,save_multipliers,save_params,zone_at_open,
)
from app.engine import Store
from app.math_rules import stop_take


def sandbox():
    return tempfile.TemporaryDirectory()


def test_stop_take_broker_min_only_no_20_tick_invent():
    bare=stop_take(100,'BUY',0.01,0.01,0,kappa=2,sl_scale=1)
    assert bare['stop_ticks']==2
    assert bare['profit_ticks']==4
    wide=stop_take(100,'BUY',0.01,0.01,0.15,kappa=1.5,sl_scale=2)
    assert wide['stop_ticks']>=17
    assert wide['sl_scale']==2.0
    assert wide['kappa']==1.5


def test_zone_at_open_computes_kappa_from_moves_not_grid():
    rows=[{'open':100,'high':103,'low':99,'close':100} for _ in range(30)]
    z=zone_at_open(rows)
    assert z['ready'] is True
    assert z['median_up']==3.0 and z['median_down']==1.0
    assert z['kappa']==3.0
    assert z['max_tp'] is not None
    assert z['lookback'] is not None and z['lookback']>=3
    assert z['noise_distance']==4.0
    assert z['structure']==4.0
    assert z['pull_share']==pytest.approx(0.25)
    assert z['favor_share']==pytest.approx(0.75)


def test_zone_sl_uses_market_shares_of_structure_not_hardcoded_fractions():
    """SL/TP shares come from OHLC up/down vs span — not coded 1/3 or 1/2."""
    rows=[]
    px=100.0
    for i in range(40):
        o=px
        px=px+0.05
        rows.append({'open':o,'high':px+0.05,'low':o-0.05,'close':px})
    z=zone_at_open(rows)
    assert z['ready'] is True
    assert z['structure'] is not None and z['structure']>z['noise_distance']
    assert z['pull_share'] is not None and z['favor_share'] is not None
    # Structure scaled by *observed* pull/favor shares from this window.
    assert z['range_distance']>=z['structure']*z['pull_share']-1e-9
    assert z['max_tp']>=z['structure']*z['favor_share']-1e-9


def test_scratch_on_sl_does_not_raise_risk():
    """Scratch must not widen SL — that ratcheted £ losses with fixed lot size."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.04,exit_reason='sl',
            stop_distance=0.2,volatility=0.3,adverse_path=0.15)
        assert out['reason']=='scratch_hold_sl'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_win_reinforces_kappa_not_sl_width():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=0.80,exit_reason='tp')
        assert out['reason'] in ('win_reinforce','tp_reinforce')
        assert load_multipliers(store)['kappa']>=before['kappa']
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_growing_sl_loss_streak_cuts_risk():
    """Rising |£| SL losses with fixed lot → cut sl_scale, do not widen again."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_multipliers(store,1.0,2.0,reason='seed_wide')
        store.set('bracket_outcome_history',[
            {'kappa':1.0,'sl_scale':1.5,'pnl':-0.50,'exit_reason':'sl'},
            {'kappa':1.0,'sl_scale':1.8,'pnl':-0.80,'exit_reason':'sl'},
        ])
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=2.0,pnl=-1.34,exit_reason='sl',
            stop_distance=1.0,volatility=0.2,adverse_path=1.0)
        assert out['reason']=='sl_loss_streak_cut_risk'
        assert out.get('growing_sl_losses') is True
        assert load_multipliers(store)['sl_scale']<2.0


def test_adaptive_lookback_picks_elbow_from_curve():
    from app.math_rules import adaptive_lookback,ohlc_range_distance
    rows=[{'open':100,'high':100.5,'low':99.5,'close':100} for _ in range(40)]
    n=adaptive_lookback(rows)
    assert 3<=n<=40
    assert ohlc_range_distance(rows) is not None


def test_propose_uses_ohlc_not_random_fantasy_kappa():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        rows=[{'open':100,'high':100.4,'low':99.7,'close':100.1} for _ in range(50)]
        p=propose_for_open(store,candles=rows,base_sl=1.0,max_tp=1.2)
        assert p['explored'] is False
        assert p['kappa']*p['sl_scale']*1.0 <= 1.2*1.05+1e-9
        assert p['kappa']<5.0


def test_loss_without_path_context_does_not_auto_raise_sl():
    """Audit #19: a bare loss is NOT proof that SL was too tight."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.20)
        assert out['reason']=='loss_insufficient_context_hold'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_sl_noise_vs_vol_raises_sl_when_tight():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.20,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=99.9,
            stop_distance=0.1,adverse_path=0.1,volatility=0.12)
        assert out['reason']=='sl_noise_raise_sl'
        assert load_multipliers(store)['sl_scale']>before['sl_scale']


def test_adverse_path_beyond_vol_holds_sl():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-1.50,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=98.0,
            stop_distance=2.0,adverse_path=2.0,volatility=0.5)
        assert out['reason']=='loss_adverse_hold_sl'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_wide_loss_teaches_cut_tp():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        adapt_after_outcome(
            store,kappa=2.5,sl_scale=1.5,pnl=-0.30,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=99.5,
            stop_distance=0.5,adverse_path=0.5,volatility=0.4)
        assert load_multipliers(store)['kappa']<1.0


def test_scratch_does_not_raise_sl():
    """Old heuristic test_scratch_teaches_raise_sl — inverted by audit #19."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.02)
        assert out['reason']=='scratch_hold_sl'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_vanish_holds_without_widening_sl():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.40,exit_reason='vanish')
        assert out['reason']=='vanish_hold'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_tp_win_reinforces():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.2,sl_scale=1.0,pnl=0.80,exit_reason='tp')
        assert out['reason'] in ('win_reinforce','tp_reinforce')
        assert load_multipliers(store)['kappa']>=before['kappa']


def test_record_outcome_learns_and_batch_raise_when_wide_wins():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        for _ in range(6):
            record_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.4,currency='GBP',
                           exit_reason='sl',direction='BUY',entry=100,exit_level=99.5,
                           stop_distance=0.5,adverse_path=0.5,volatility=0.2)
        for _ in range(6):
            record_outcome(store,kappa=2.0,sl_scale=1.5,pnl=0.8,currency='GBP',
                           exit_reason='tp')
        promoted=evolve_from_history(store,min_samples=6)
        assert promoted is not None
        assert load_multipliers(store)['kappa']>=1.0


def test_recent_reachable_move_from_candles():
    # Upper-half median of spans — no fixed 90%ile; quiet bars + larger cluster.
    rows=[{'open':100,'high':100.2,'low':100,'close':100} for _ in range(8)]
    rows+=[{'open':100,'high':107,'low':100,'close':103} for _ in range(5)]
    got=recent_reachable_move(rows,n=60)
    assert got is not None and got>=6.0


def test_save_and_load_reject_invalid():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_params(store,1.2,1.1,reason='nudge')
        assert load_params(store)=={'kappa':1.2,'sl_scale':1.1}
        store.set('bracket_kappa',-1)
        store.set('bracket_sl_scale',0.5)
        restored=load_params(store)
        assert restored['kappa']==DEFAULT_KAPPA
        assert restored['sl_scale']==DEFAULT_SL_SCALE
