"""Bag follow-up: bracket schedule + readiness floors in self_params; policy.evolve gated."""
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.bracket_learn import adapt_after_outcome, _schedule
from app.engine import AdaptiveBrain, Store
from app import model_readiness as mr
from app.self_params import DEFAULTS, evolve_from_feedback, get, load, save


def test_bracket_schedule_lives_in_self_params_and_evolves():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        assert 'bracket_vs_vol_noise' in DEFAULTS
        assert 'bracket_shape_wide' in DEFAULTS
        sched=_schedule(store)
        assert sched['bracket_vs_vol_noise']==pytest.approx(1.15)
        assert sched['bracket_shape_wide']==pytest.approx(1.6)
        # Override Store — path analysis must use robot knobs, not module literals.
        save(store,{**DEFAULTS,'bracket_vs_vol_noise':1.5,
                    'bracket_shape_wide':2.0},reason='test')
        before_noise=load(store)['bracket_vs_vol_noise']
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.20,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=99.9,
            stop_distance=0.1,adverse_path=0.1,volatility=0.12)
        # vs_vol=0.1/0.12≈0.83 ≤ 1.5 → noise; shape 1.0 < 2.0 → raise SL.
        assert out['reason']=='sl_noise_raise_sl'
        assert out['shape_wide']==pytest.approx(2.0)
        # Teacher schedule itself nudges from adapt reason (queued trial).
        assert load(store)['bracket_vs_vol_noise']>=before_noise


def test_readiness_floors_from_self_params():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        for k in ('min_prep_candles','train_frac','min_holdout_bars',
                  'min_online_labeled','min_holdout_minute_acc'):
            assert k in DEFAULTS
        fl=mr.floors(store)
        assert fl['min_prep_candles']==96
        assert fl['min_online_labeled']==64
        save(store,{**DEFAULTS,'min_prep_candles':80,'min_online_labeled':40,
                    'min_holdout_minute_acc':0.32},reason='test')
        fl2=mr.floors(store)
        assert fl2['min_prep_candles']==80
        assert fl2['min_online_labeled']==40
        assert fl2['min_holdout_minute_acc']==pytest.approx(0.32)
        # Chronological split respects Store floor.
        from datetime import datetime, timedelta, timezone
        start=datetime(2026,10,1,8,0,tzinfo=timezone.utc)
        rows=[{'at':(start+timedelta(minutes=i)).isoformat(),
               'open':100.,'high':100.1,'low':99.9,'close':100.,'spread':0.02}
              for i in range(70)]
        # 70 < 80 → empty train/hold under Store floor (seed was 96).
        train,hold,all_rows=mr.chronological_split(rows,store=store)
        assert len(all_rows)==70
        assert train==[] and hold==[]


def test_readiness_floors_evolve_from_shadow_promote():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load(store)['min_online_labeled']
        acc0=load(store)['min_holdout_minute_acc']
        evolve_from_feedback(store,shadow_promote=True)
        assert load(store)['min_online_labeled']<=before
        assert load(store)['min_holdout_minute_acc']<=acc0
        evolve_from_feedback(store,shadow_promote=False)
        assert load(store)['min_online_labeled']>=before-2


def test_policy_evolve_gated_off_by_default():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        assert int(DEFAULTS['policy_evolve_enabled'])==0
        assert int(get(store,'policy_evolve_enabled',0))==0
        policy=AutoEvolution(store,Path(d)/'policy.py')
        policy.last_change=0
        before=policy.attempts
        assert policy.evolve(.4,20,[]) is None
        assert policy.attempts==before
        assert 'gated off' in (policy.last_reason or '').lower()
        # Opt-in restores search path.
        save(store,{**DEFAULTS,'policy_evolve_enabled':1},reason='test')
        policy.last_change=0
        policy.evolve(.4,20,[])
        assert policy.attempts==before+1


def test_engine_activate_uses_store_online_floor():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'min_online_labeled':40},reason='test')
        brain=AdaptiveBrain(store,data_dir=d)
        brain._activate('GOLD')
        # Fresh brain labeled=0 < 40 → prep reset reason recorded.
        state=mr.load_state(store,'GOLD')
        assert state['ready'] is False
        assert 'prep' in (state.get('reason') or '').lower() or 'load' in (state.get('reason') or '').lower()
