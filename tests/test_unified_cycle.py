"""One LIVE cycle: perceive → decide → risk → settle → learn."""
import tempfile
from pathlib import Path

import pytest

from app import unified_cycle as uc
from app.engine import Store


def sandbox():
    return tempfile.TemporaryDirectory()


def test_perceive_decision_argmax_and_margin():
    scores = {'BUY': 0.55, 'SELL': 0.30, 'HOLD': 0.15}
    d = uc.perceive_decision(scores)
    assert d['action'] == 'BUY'
    assert d['margin'] == pytest.approx(0.25)
    assert d['confidence'] == pytest.approx(0.55)
    # No scores → margin stays unknown (not invented as 0.0 weak edge).
    bare = uc.perceive_decision(None, {'action': 'BUY'})
    assert bare['action'] == 'BUY'
    assert bare['margin'] is None


def test_compose_open_allows_explicit_action_without_scores():
    """Missing scores must not invent margin=0 and block against entry_edge_floor."""
    pkg = uc.compose_open(
        action='BUY', scores=None, evaluation={'action': 'BUY'},
        zone={'kappa': 1.5}, learned={'kappa': 1.0, 'sl_scale': 1.0},
        cycle_state=uc.default_state(), base_sl=1.0)
    assert pkg['blocked'] is False
    assert pkg['action'] == 'BUY'
    assert uc.edge_allows_open(None, {'action': 'BUY'}, uc.default_state())['allowed'] is True


def test_edge_allows_open_blocks_weak_edge_at_fixed_floor():
    state = uc.default_state()
    state['loss_streak'] = 3  # streak must NOT raise the 0.06 gate
    scores = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}  # margin 0.05 < 0.06
    gate = uc.edge_allows_open(scores, {'action': 'BUY'}, state)
    assert gate['allowed'] is False
    assert gate['action'] == 'HOLD'
    assert gate['min_margin'] == pytest.approx(0.06)
    assert 'Unified cycle' in gate['reason']


def test_edge_allows_open_when_margin_clear():
    state = uc.default_state()
    state['loss_streak'] = 2
    scores = {'BUY': 0.50, 'SELL': 0.30, 'HOLD': 0.20}  # margin 0.20
    gate = uc.edge_allows_open(scores, {'action': 'BUY'}, state)
    assert gate['allowed'] is True
    assert gate['action'] == 'BUY'


def test_compose_open_damps_with_aggression():
    zone = {'kappa': 2.0, 'range_distance': 1.0, 'max_tp': 4.0}
    learned = {'kappa': 1.0, 'sl_scale': 1.5}
    state = uc.default_state()
    state['aggression'] = 0.80
    scores = {'BUY': 0.60, 'SELL': 0.20, 'HOLD': 0.20}
    pkg = uc.compose_open(
        action='BUY', scores=scores, evaluation={'action': 'BUY'},
        zone=zone, learned=learned, cycle_state=state, base_sl=1.0)
    assert pkg['blocked'] is False
    assert pkg['action'] == 'BUY'
    # κ = zone × learned × aggression = 2 × 1 × 0.8
    assert pkg['kappa'] == pytest.approx(1.6)
    # sl_scale = max(1, 1.5 × 0.8 × edge_scale) with edge_scale in [0.85, 1.10]
    assert pkg['sl_scale'] >= 1.0
    assert pkg['sl_scale'] < 1.5 * 1.0 * 1.10  # damped vs full learned


def test_compose_open_hold_when_edge_too_weak():
    state = uc.default_state()
    state['loss_streak'] = 5  # still need fixed 0.06
    scores = {'BUY': 0.42, 'SELL': 0.40, 'HOLD': 0.18}  # margin 0.02 < 0.06
    pkg = uc.compose_open(
        action='BUY', scores=scores, evaluation={'action': 'BUY'},
        zone={'kappa': 1.5}, learned={'kappa': 1.0, 'sl_scale': 1.0},
        cycle_state=state, base_sl=1.0)
    assert pkg['blocked'] is True
    assert pkg['action'] == 'HOLD'
    assert pkg['kappa'] is None
    assert pkg['min_margin'] == pytest.approx(0.06)


def test_settle_cools_aggression_on_losses_and_warms_on_wins():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        s1 = uc.settle(store, pnl=-1.2, action='BUY')
        assert s1['loss_streak'] == 1
        assert s1['win_streak'] == 0
        assert s1['aggression'] < 1.0
        s2 = uc.settle(store, pnl=-0.8, action='SELL')
        assert s2['loss_streak'] == 2
        assert s2['aggression'] < s1['aggression']
        # Wins clear loss streak and slowly raise aggression.
        s3 = uc.settle(store, pnl=1.5, action='BUY')
        assert s3['loss_streak'] == 0
        assert s3['win_streak'] == 1
        assert s3['aggression'] > s2['aggression']


def test_note_open_increments_counter():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        a = uc.note_open(store, action='BUY')
        b = uc.note_open(store, action='SELL')
        assert a['opens'] == 1
        assert b['opens'] == 2
        assert b['last_action'] == 'SELL'


def test_min_open_margin_stays_fixed_at_006():
    # Streak / stored floor must not move the gate — always 0.06.
    assert uc.min_open_margin({'loss_streak': 0}) == pytest.approx(0.06)
    assert uc.min_open_margin({'loss_streak': 1}) == pytest.approx(0.06)
    assert uc.min_open_margin({'loss_streak': 20}) == pytest.approx(0.06)
    assert uc.min_open_margin({'loss_streak': 0, 'entry_edge_floor': 0.03}) == pytest.approx(0.06)
    assert uc.min_open_margin({'entry_edge_floor': 0.18}) == pytest.approx(0.06)


def test_settle_and_weight_update_do_not_move_entry_edge_floor():
    """£ / ΔW may move aggression; entry floor stays 0.06."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        before = uc.load_state(store)['entry_edge_floor']
        assert before == pytest.approx(0.06)
        uc.settle(store, pnl=1.5, action='BUY')
        assert uc.load_state(store)['entry_edge_floor'] == pytest.approx(0.06)
        assert uc.load_state(store)['aggression'] > 0.99
        uc.note_weight_update(store, won=True, delta_norm=0.20)
        assert uc.load_state(store)['entry_edge_floor'] == pytest.approx(0.06)
        uc.settle(store, pnl=-1.0, action='SELL')
        assert uc.load_state(store)['entry_edge_floor'] == pytest.approx(0.06)
        assert uc.load_state(store)['aggression'] < 1.0


def test_policy_live_uses_same_fixed_edge_floor_as_cycle():
    """Policy MIN_EDGE override == fixed 0.06 cycle floor."""
    with sandbox() as d:
        from app.autonomy import AutoEvolution, TEMPLATES
        store = Store(Path(d) / 'db.sqlite3')
        # Stale raised floor in Store must be ignored.
        st = uc.load_state(store)
        st['entry_edge_floor'] = 0.12
        uc.save_state(store, st)
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.02),
            encoding='utf8')
        policy = AutoEvolution(store, path)
        scores = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}  # edge 0.05 < 0.06
        out = policy.evaluate_neural_entry(scores, readiness={'ready': True})
        assert out['action'] == 'HOLD'
        assert out['evidence']['evolved_policy']['entry_edge_floor'] == pytest.approx(0.06)
        gate = uc.edge_allows_open(scores, {'action': 'BUY'}, uc.load_state(store))
        assert gate['allowed'] is False
        assert gate['min_margin'] == pytest.approx(0.06)


def test_machine_spec_documents_unified_cycle():
    from app import machine_spec
    assert machine_spec.UNIFIED_CYCLE_STORE == 'unified_cycle_v1'
    assert 'compose_open' in machine_spec.UNIFIED_CYCLE
    assert 'unified_cycle.settle' in machine_spec.UNIFIED_CYCLE
    assert 'policy_evolve' in machine_spec.UNIFIED_CYCLE
    assert 'fixed' in machine_spec.UNIFIED_CYCLE_EDGE_BUS
