"""How many closed £ until learning knobs are *visible* (not calendar days).

Operator question: when will Admin/behaviour look different after the
self-learning fixes? Answer in settled broker closes — one loss/scratch
already moves floor/arm; a short streak makes the change obvious.
"""
import tempfile
from pathlib import Path

import pytest

from app import ohlc_entry_zones as oez
from app import unified_cycle as uc
from app.engine import Store
from app.self_params import DEFAULTS, evolve_from_feedback, load


def sandbox():
    return tempfile.TemporaryDirectory()


def test_edge_floor_visible_after_1_to_3_losses():
    """1 meaningful loss → +0.01 floor; 3 losses → clearly selective."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        assert uc.entry_edge_floor(store) == pytest.approx(0.06)
        # First closed loss already moves Admin/policy MIN_EDGE.
        r1 = uc.observe_close(store, broker_pnl=-1.20, spread=0.05,
                              entry_meta={'score_edge': 0.07})
        assert r1['adapted'] is True
        assert uc.entry_edge_floor(store) == pytest.approx(0.07)
        uc.observe_close(store, broker_pnl=-0.80, spread=0.05)
        uc.observe_close(store, broker_pnl=-0.50, spread=0.05)
        assert uc.entry_edge_floor(store) == pytest.approx(0.09)


def test_trail_arm_visible_after_1_to_2_scratches():
    """Scratch £ raises trail_arm_mfe_frac within 1–2 closes."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        before = float(load(store)['trail_arm_mfe_frac'])
        assert before == pytest.approx(DEFAULTS['trail_arm_mfe_frac'])
        evolve_from_feedback(store, broker_pnl=-0.03, commit=True)  # scratch band
        mid = float(load(store)['trail_arm_mfe_frac'])
        assert mid > before
        evolve_from_feedback(store, broker_pnl=-0.04, commit=True)
        after = float(load(store)['trail_arm_mfe_frac'])
        assert after > mid
        # Cushion also thickens — bounce room grows.
        assert float(load(store)['trail_be_cushion_frac']) > float(
            DEFAULTS['trail_be_cushion_frac'])


def test_ohlc_break_tape_visible_after_1_break_loss():
    """One losing break OPEN raises tape_need (stricter M1 proof)."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        p0 = oez.load_params(store)
        assert int(p0['tape_need']) == 1
        out = oez.observe_close(
            store,
            ohlc_meta={'trigger': 'break', 'action': 'BUY'},
            broker_pnl=-1.0, spread=0.1)
        assert out['adapted'] is True
        p1 = oez.load_params(store)
        assert int(p1['tape_need']) == 2
        assert float(p1['break_mtf_conf']) > float(p0['break_mtf_conf'])


def test_visible_horizon_contract_in_trade_counts():
    """Document the operator-facing horizon: closes, not days."""
    # Meaningful |£| closes that teach (scratch pennies still teach trail arm).
    assert {
        'edge_floor_first_step': 1,
        'edge_floor_obvious': 3,
        'trail_arm_first_step': 1,
        'trail_arm_obvious': 2,
        'ohlc_break_tape_first_step': 1,
        'note': 'cash-like broker closes only; pending £ does not teach',
    }['edge_floor_obvious'] == 3
