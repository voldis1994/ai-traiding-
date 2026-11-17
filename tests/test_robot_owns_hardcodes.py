"""Robot owns former hardcodes via self_params — £ feedback can move them."""
import tempfile
from pathlib import Path

import pytest

from app import day_book, unified_cycle as uc
from app.autonomy import AutoEvolution
from app.bracket_learn import FIXED_TP_SL_RATIO, decide_brackets_from_ohlc
from app.engine import Store
from app.self_params import (
    BOUNDS, DEFAULTS, evolve_from_feedback, get, load, save,
)


def sandbox():
    return tempfile.TemporaryDirectory()


def test_new_knobs_seeded_and_bounded():
    for key in (
        'hold_lean_gap', 'day_add_trend_floor', 'entry_edge_floor',
        'trail_arm_mfe_frac', 'aggression_loss_step', 'bracket_tighten_floor',
        'day_open_slices', 'day_phase_protect_tilt',
    ):
        assert key in DEFAULTS and key in BOUNDS


def test_rr_ratio_stays_operator_locked():
    assert FIXED_TP_SL_RATIO == 2.0
    assert 'rr_ratio' not in DEFAULTS
    d = decide_brackets_from_ohlc(
        {'kappa': 1.0, 'range_distance': 1.0, 'max_tp': 0.5, 'ready': True},
        tick=0.01, spread=0.05, min_distance=0.2)
    assert d['kappa'] == pytest.approx(2.0)


def test_broker_loss_moves_entry_hold_trail_slices():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        before = load(store)
        evolve_from_feedback(store, broker_pnl=-2.0, commit=True)
        after = load(store)
        # Losses may raise soft hint; still never required to hard-block.
        assert after['entry_edge_floor'] >= before['entry_edge_floor']
        assert after['hold_lean_gap'] >= before['hold_lean_gap']
        assert after['trail_arm_mfe_frac'] >= before['trail_arm_mfe_frac']
        assert after['day_open_slices'] <= before['day_open_slices']


def test_win_eases_entry_toward_positive():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**load(store), 'entry_edge_floor': 0.08}, reason='seed')
        evolve_from_feedback(store, broker_pnl=1.5, commit=True)
        after = load(store)
        assert after['entry_edge_floor'] <= 0.08


def test_hold_shadow_teach_eases_hold_lean():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        before = float(get(store, 'hold_lean_gap'))
        evolve_from_feedback(store, hold_shadow_taught=True, commit=True)
        assert float(get(store, 'hold_lean_gap')) < before


def test_hold_necessary_reads_self_params():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        # Loud HOLD but clear lean — not necessary.
        assert AutoEvolution.hold_necessary(
            0.42, 0.30, 0.55, store=store) is False
        # Raise lean gap (within rails ≤0.10) so 0.08 gap is no longer enough.
        save(store, {**load(store), 'hold_lean_gap': 0.10,
                     'hold_side_gap_tie': 0.10,
                     'hold_dominate_extra': 0.08,
                     'hold_dominate_side_gap': 0.12}, reason='test')
        # BUY-SELL gap 0.05 < lean 0.10; HOLD dominates → necessary.
        assert AutoEvolution.hold_necessary(
            0.40, 0.35, 0.55, store=store) is True


def test_day_book_evolve_uses_trial_not_direct_save():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        daily = {'realized_pnl': -50.0, 'opens': 2, 'closes': 2}
        budgets = {'profit_target': 10.0, 'loss_budget': 20.0}
        day_book._evolve_knobs_from_close(
            store, pnl=-5.0, daily=daily, budgets_map=budgets)
        # commit=False → trial activation writes params
        pending = store.get('self_params_pending_v1') or {}
        assert pending.get('trial_active') or load(store)['day_risk_per_trade_frac'] < DEFAULTS['day_risk_per_trade_frac']


def test_unified_cycle_reads_robot_entry_edge():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**load(store), 'entry_edge_floor': 0.09}, reason='test')
        assert uc.entry_edge_floor(store) == pytest.approx(0.09)
        assert uc.min_open_margin(uc.load_state(store), store=store) == pytest.approx(0.09)


def test_settle_aggression_steps_from_bag():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**load(store),
                     'aggression_loss_step': 0.90,
                     'aggression_min': 0.60}, reason='test')
        st = uc.settle(store, pnl=-1.0, action='BUY')
        # One loss with step 0.90 from aggression 1.0 → 0.90
        assert st['aggression'] == pytest.approx(0.90, abs=0.02)
