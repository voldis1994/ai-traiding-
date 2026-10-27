"""SL/TP from OHLC at open; cash P&L only nudges — no κ grid lottery."""
import tempfile
from pathlib import Path

from app.bracket_learn import (
    DEFAULT_KAPPA,DEFAULT_SL_SCALE,evolve_from_history,load_params,
    propose_for_open,recent_reachable_move,record_outcome,save_params,zone_at_open,
)
from app.engine import Store
from app.math_rules import stop_take


def sandbox():
    return tempfile.TemporaryDirectory()


def _candles(n=40, up=1.0, down=1.0, span=None):
    span=span if span is not None else up+down
    rows=[]
    for i in range(n):
        o=100.0
        # Alternate so median up/down match targets.
        if i%2==0:
            rows.append({'open':o,'high':o+up,'low':o-down*0.2,'close':o+up*0.5})
        else:
            rows.append({'open':o,'high':o+up*0.2,'low':o-down,'close':o-down*0.5})
    # Ensure span coverage
    rows[-1]={'open':100,'high':100+span,'low':100,'close':100+span*0.4}
    return rows


def test_stop_take_broker_min_only_no_20_tick_invent():
    bare=stop_take(100,'BUY',0.01,0.01,0,kappa=2,sl_scale=1)
    assert bare['stop_ticks']==2
    assert bare['profit_ticks']==4
    wide=stop_take(100,'BUY',0.01,0.01,0.15,kappa=1.5,sl_scale=2)
    assert wide['stop_ticks']>=17
    assert wide['sl_scale']==2.0
    assert wide['kappa']==1.5


def test_zone_at_open_computes_kappa_from_moves_not_grid():
    # Upside wick 3.0, downside wick 1.0 → κ=3 from data, not a table pick.
    rows=[{'open':100,'high':103,'low':99,'close':100} for _ in range(30)]
    z=zone_at_open(rows,n=30,reach_n=30)
    assert z['ready'] is True
    assert z['median_up']==3.0 and z['median_down']==1.0
    assert z['kappa']==3.0
    assert z['max_tp'] is not None


def test_propose_uses_ohlc_not_random_fantasy_kappa():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        rows=_candles(50,up=1.2,down=1.0,span=2.0)
        # Cap reachable move small so fantasy TP cannot appear.
        for r in rows:
            r['high']=min(r['high'],100.5);r['low']=max(r['low'],99.5)
        p=propose_for_open(store,candles=rows,base_sl=1.0,max_tp=1.2)
        assert p['explored'] is False
        assert p['kappa']*p['sl_scale']*1.0 <= 1.2*1.05+1e-9
        assert p['kappa']<5.0  # no κ=6/8 lottery


def test_cash_nudge_evolve_widens_when_wide_wins():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        for _ in range(6):
            record_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.4,currency='GBP')
        for _ in range(6):
            record_outcome(store,kappa=2.0,sl_scale=1.5,pnl=0.8,currency='GBP')
        promoted=evolve_from_history(store,min_samples=6)
        assert promoted is not None
        assert promoted['kappa']>=1.05


def test_recent_reachable_move_from_candles():
    rows=[{'open':100,'high':100+i*0.1,'low':100,'close':100} for i in range(10)]
    rows.append({'open':100,'high':108,'low':100,'close':104})
    got=recent_reachable_move(rows,n=60)
    assert got is not None and got>=6.8


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
