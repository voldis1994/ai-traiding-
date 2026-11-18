"""Adaptive re-entry confirmation: raw neural edge + tape proof + learn floors."""
import tempfile
from pathlib import Path

import pytest

from app import reentry_confirm as rc
from app.engine import Store


def test_raw_disagrees_nudged_holds():
    scores_raw = {'BUY': 0.20, 'SELL': 0.55, 'HOLD': 0.25}
    # Modules nudged to BUY but neuron still prefers SELL.
    out = rc.evaluate(
        action='BUY', scores_raw=scores_raw,
        market_context={'trend': 'UP'},
        mtf_analysis={'ready': True, 'structure_break': 'UP',
                      'stop_hint_atr14': 2.0, 'preferred': 'BUY', 'mode': 'BUY'},
        candles=[{'open': 100, 'close': 101}] * 3)
    assert out['allowed'] is False
    assert out['reason'] == 'raw_disagrees_nudged'


def test_weak_raw_holds():
    scores_raw = {'BUY': 0.34, 'SELL': 0.33, 'HOLD': 0.33}
    out = rc.evaluate(action='BUY', scores_raw=scores_raw)
    assert out['allowed'] is False
    assert out['tier'] == 'weak'


def test_strong_with_price_confirm_enters_immediately():
    scores_raw = {'BUY': 0.55, 'SELL': 0.20, 'HOLD': 0.25}  # edge 0.30
    out = rc.evaluate(
        action='BUY', scores_raw=scores_raw,
        market_context={'trend': 'UP'},
        mtf_analysis={'ready': True, 'structure_break': 'UP',
                      'stop_hint_atr14': 1.5, 'preferred': 'BUY', 'mode': 'BUY'})
    assert out['allowed'] is True
    assert out['tier'] == 'strong'
    assert out.get('fast_entry') is True


def test_medium_needs_two_m1_closes():
    scores_raw = {'BUY': 0.42, 'SELL': 0.28, 'HOLD': 0.30}  # edge 0.12
    one = rc.evaluate(
        action='BUY', scores_raw=scores_raw,
        market_context={'trend': 'MIXED'},
        mtf_analysis={'ready': True, 'preferred': 'HOLD', 'mode': 'HOLD'},
        candles=[{'open': 100, 'close': 99}, {'open': 99, 'close': 100}])
    assert one['allowed'] is False
    assert one['reason'] == 'medium_need_m1_confirm'
    two = rc.evaluate(
        action='BUY', scores_raw=scores_raw,
        market_context={'trend': 'MIXED'},
        mtf_analysis={'ready': True, 'preferred': 'HOLD', 'mode': 'HOLD'},
        candles=[
            {'open': 98, 'close': 99},
            {'open': 99, 'close': 100},
            {'open': 100, 'close': 101},
        ])
    assert two['allowed'] is True
    assert two['reason'] == 'medium_m1_confirm'


def test_same_dir_reentry_needs_new_atr_move():
    scores_raw = {'SELL': 0.50, 'BUY': 0.20, 'HOLD': 0.30}  # strong SELL
    last = {'direction': 'SELL', 'exit_mid': 2650.0, 'pnl': 0.1}
    blocked = rc.evaluate(
        action='SELL', scores_raw=scores_raw,
        last_outcome=last,
        market_context={'trend': 'DOWN', 'close': 2649.9},
        mtf_analysis={'ready': True, 'stop_hint_atr14': 4.0,
                      'preferred': 'SELL', 'mode': 'SELL'},
        candles=[{'open': 2651, 'close': 2650}, {'open': 2650, 'close': 2649.9}])
    assert blocked['allowed'] is False
    assert 'need_new' in blocked['reason']
    ok = rc.evaluate(
        action='SELL', scores_raw=scores_raw,
        last_outcome=last,
        market_context={'trend': 'DOWN', 'close': 2648.0},
        mtf_analysis={'ready': True, 'stop_hint_atr14': 4.0, 'structure_break': 'DOWN',
                      'preferred': 'SELL', 'mode': 'SELL'},
        candles=[{'open': 2651, 'close': 2649}, {'open': 2649, 'close': 2648}])
    assert ok['allowed'] is True


def test_observe_close_raises_strong_after_fast_loss():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        before = rc.load_params(store)['raw_strong']
        out = rc.observe_close(
            store,
            reentry_meta={'tier': 'strong', 'fast_entry': True, 'same_dir': False},
            broker_pnl=-0.40, spread=0.2)
        assert out['adapted'] is True
        after = rc.load_params(store)['raw_strong']
        assert after > before


def test_engine_reentry_gate_blocks_amplified_buy():
    """Engine helper: raw SELL vs nudged BUY → HOLD."""
    from app.autonomy import AutoEvolution
    from app.engine import SingleTradeEngine
    from tests.test_learning_integrity import new_brain

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        brain.tick(100, .05, .01, 'GOLD')
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.market_context = {'trend': 'UP', 'close': 2655}
        e.last_signal = {
            'scores_raw': {'BUY': 0.22, 'SELL': 0.50, 'HOLD': 0.28},
        }
        gate = e._reentry_confirm_gate(
            'BUY',
            scores_raw={'BUY': 0.22, 'SELL': 0.50, 'HOLD': 0.28},
            mtf_analysis={'ready': True, 'structure_break': 'UP',
                          'stop_hint_atr14': 2.0, 'preferred': 'BUY', 'mode': 'BUY'})
        assert gate['allowed'] is False
        assert gate['reason'] == 'raw_disagrees_nudged'
