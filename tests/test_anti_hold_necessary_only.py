"""HOLD only when necessary — not when softmax HOLD is merely loud."""
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import Store
from app import day_book as db


def test_hold_necessary_false_when_side_leans():
    # Classic GOLD trap: HOLD 56% but BUY already beats SELL.
    assert AutoEvolution.hold_necessary(0.247, 0.191, 0.562) is False
    # Ambiguous chop — HOLD necessary.
    assert AutoEvolution.hold_necessary(0.33, 0.33, 0.34) is True
    # Clear tape overrides loud HOLD.
    assert AutoEvolution.hold_necessary(
        0.25, 0.20, 0.55, trend='UP', structure='TREND_UP') is False


def test_evaluate_opens_when_hold_loud_but_side_leans():
    with tempfile.TemporaryDirectory() as d:
        p = AutoEvolution(Store(Path(d) / 'db.sqlite3'), Path(d) / 'pol.py')
        # Need readiness so utility/policy path can confirm.
        ready = {'ready': True, 'eval_samples': 40, 'minute_direction_accuracy': 0.4,
                 'holdout_net_bps_mean': 0.2}
        scores = {'BUY': 0.42, 'SELL': 0.28, 'HOLD': 0.30}
        # BUY−SELL=0.14 ≥ default edge floor → should OPEN even if HOLD≈BUY.
        out = p.evaluate_neural_entry(
            scores, readiness=ready, trend='UP', structure='TREND_UP',
            returns_bps={'1': 5, '5': 8})
        assert out['action'] == 'BUY'
        assert out['proposal'] == 'BUY'


def test_evaluate_holds_when_sides_tied():
    with tempfile.TemporaryDirectory() as d:
        p = AutoEvolution(Store(Path(d) / 'db.sqlite3'), Path(d) / 'pol.py')
        ready = {'ready': True, 'eval_samples': 40}
        out = p.evaluate_neural_entry(
            {'BUY': 0.33, 'SELL': 0.33, 'HOLD': 0.34}, readiness=ready)
        assert out['action'] == 'HOLD'
        assert 'necessary' in out['reason'].lower() or 'tied' in out['reason'].lower()


def test_lean_anti_hold_nudge_drains_hold_mass():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / 't.sqlite3')
        scores = {'BUY': 0.36, 'SELL': 0.24, 'HOLD': 0.40}
        nudged, meta = db.soft_nudge_conviction(
            scores, phase=db.PHASE_HUNT, store=s)
        assert meta.get('nudged') is True
        assert meta.get('reason') in ('lean_anti_hold', 'conviction_anti_hold',
                                      'clear_trend_anti_hold')
        assert nudged['BUY'] > nudged['HOLD']
