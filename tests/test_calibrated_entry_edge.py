"""Holdout/spread-calibrated entry edge (not £ ratchet, not lot size)."""
import tempfile
from pathlib import Path

import pytest

from app import unified_cycle as uc
from app.autonomy import AutoEvolution, TEMPLATES
from app.engine import Store


def sandbox():
    return tempfile.TemporaryDirectory()


def _policy(store, d, margin=0.02):
    path = Path(d) / 'policy.py'
    path.write_text(
        TEMPLATES['edge'].format(
            confidence=0.60, reversal=0.30, min_hold=3, margin=margin),
        encoding='utf8')
    return AutoEvolution(store, path)


def test_default_calibration_seeds_near_006():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        policy = _policy(store, d)
        floor, detail = policy.calibrate_live_entry_floor(
            'BUY', history=[], spread_bps=0.0, accuracy=None, samples=0)
        assert floor == pytest.approx(0.06)
        assert uc.min_open_margin(uc.load_state(store)) == pytest.approx(0.06)
        assert detail['method'] in ('policy_default', 'adaptive_threshold',
                                    'holdout_calibrated')


def test_spread_raises_calibrated_floor_within_rails():
    """Costlier instrument → higher required score edge (still ≤ MAX)."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        policy = _policy(store, d)
        # spread_bps=10 → cost bump ≈ 0.02 → ~0.08 (snapped to 0.01)
        floor, detail = policy.calibrate_live_entry_floor(
            'BUY', history=[], spread_bps=10.0, accuracy=None, samples=0)
        assert floor > 0.06
        assert floor <= uc.ENTRY_EDGE_MAX
        assert abs(round(floor / uc.ENTRY_EDGE_STEP) * uc.ENTRY_EDGE_STEP - floor) < 1e-9
        assert uc.load_state(store)['entry_edge_floor'] == pytest.approx(floor)
        meta = uc.calibration_meta(store)
        assert meta['floor'] == pytest.approx(floor)
        assert meta['detail'].get('spread_bps') == pytest.approx(10.0)


def test_poor_accuracy_with_many_samples_tightens_floor():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        policy = _policy(store, d)
        floor, _ = policy.calibrate_live_entry_floor(
            'SELL', history=[], spread_bps=0.0, accuracy=0.35, samples=500)
        # Weak model → positive adjustment → floor ≥ default
        assert floor >= 0.06
        assert floor <= uc.ENTRY_EDGE_MAX


def test_set_calibrated_floor_shared_by_policy_and_compose():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        uc.set_calibrated_floor(store, 0.09, method='manual_test')
        policy = _policy(store, d)
        # evaluate recalibrates from empty history → may move; force floor after
        uc.set_calibrated_floor(store, 0.09, method='manual_test')
        scores = {'BUY': 0.42, 'SELL': 0.34, 'HOLD': 0.24}  # edge 0.08 < 0.09
        cycle = uc.load_state(store)
        assert uc.min_open_margin(cycle) == pytest.approx(0.09)
        gate = uc.edge_allows_open(scores, {'action': 'BUY'}, cycle)
        assert gate['allowed'] is False
        assert gate['min_margin'] == pytest.approx(0.09)
        # Clear edge clears the bar
        strong = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
        assert uc.edge_allows_open(
            strong, {'action': 'BUY'}, cycle)['allowed'] is True


def test_live_evaluate_exposes_calibration_and_uses_floor():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        policy = _policy(store, d)
        scores = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
        out = policy.evaluate_neural_entry(
            scores, readiness={'ready': True, 'eval_samples': 40,
                               'minute_direction_accuracy': 0.5},
            spread_bps=0.0)
        assert out['action'] == 'BUY'
        assert out['threshold'] is not None
        assert out['calibration'] is not None
        assert out['evidence']['evolved_policy']['entry_edge_floor'] == pytest.approx(
            out['threshold'])
        assert uc.ENTRY_EDGE_MIN <= out['threshold'] <= uc.ENTRY_EDGE_MAX


def test_rails_clip_extreme_calibration():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        low = uc.set_calibrated_floor(store, 0.001, method='too_low')
        assert low['floor'] == pytest.approx(uc.ENTRY_EDGE_MIN)
        high = uc.set_calibrated_floor(store, 0.99, method='too_high')
        assert high['floor'] == pytest.approx(uc.ENTRY_EDGE_MAX)


def test_entry_edge_only_whole_hundredths():
    """0.06 → next 0.07; never 0.065 / 0.086."""
    assert uc.quantize_entry_edge(0.06) == pytest.approx(0.06)
    assert uc.quantize_entry_edge(0.065) in (0.06, 0.07)
    assert uc.quantize_entry_edge(0.065) == round(uc.quantize_entry_edge(0.065), 2)
    assert uc.quantize_entry_edge(0.086) == pytest.approx(0.09)
    assert uc.quantize_entry_edge(0.084) == pytest.approx(0.08)
    assert uc.quantize_entry_edge(0.071) == pytest.approx(0.07)
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        meta = uc.set_calibrated_floor(store, 0.086, method='dust')
        assert meta['floor'] == pytest.approx(0.09)
        assert str(meta['floor']).rstrip('0').endswith('9') or meta['floor'] == 0.09
        # Persist + reload stays on hundredth grid.
        assert uc.load_state(store)['entry_edge_floor'] == pytest.approx(0.09)
        # Allowed grid only.
        for x in (0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12):
            assert uc.quantize_entry_edge(x) == pytest.approx(x)
