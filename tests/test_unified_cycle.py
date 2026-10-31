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


def test_edge_allows_open_blocks_after_loss_streak():
    state = uc.default_state()
    state['loss_streak'] = 3  # need margin ≥ 0.06
    scores = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}  # margin 0.05
    gate = uc.edge_allows_open(scores, {'action': 'BUY'}, state)
    assert gate['allowed'] is False
    assert gate['action'] == 'HOLD'
    assert 'Unified cycle' in gate['reason']


def test_edge_allows_open_when_margin_clear():
    state = uc.default_state()
    state['loss_streak'] = 2  # need 0.04
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
    state['loss_streak'] = 5  # need 0.10
    scores = {'BUY': 0.42, 'SELL': 0.40, 'HOLD': 0.18}  # margin 0.02
    pkg = uc.compose_open(
        action='BUY', scores=scores, evaluation={'action': 'BUY'},
        zone={'kappa': 1.5}, learned={'kappa': 1.0, 'sl_scale': 1.0},
        cycle_state=state, base_sl=1.0)
    assert pkg['blocked'] is True
    assert pkg['action'] == 'HOLD'
    assert pkg['kappa'] is None


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


def test_min_open_margin_scales_with_streak():
    assert uc.min_open_margin({'loss_streak': 0}) == 0.0
    assert uc.min_open_margin({'loss_streak': 1}) == pytest.approx(0.02)
    assert uc.min_open_margin({'loss_streak': 20}) == pytest.approx(0.20)


def test_machine_spec_documents_unified_cycle():
    from app import machine_spec
    assert machine_spec.UNIFIED_CYCLE_STORE == 'unified_cycle_v1'
    assert 'compose_open' in machine_spec.UNIFIED_CYCLE
    assert 'unified_cycle.settle' in machine_spec.UNIFIED_CYCLE
