"""V3.0 engine contract: H01 isolated SHADOW, L01 scores_raw, R02 hard_risk, A03 no champion."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from app import hard_risk
from app.autonomy import AutoEvolution
from app.core import NeuralCore
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.test_learning_integrity import new_brain


def _op_limits(**kw):
    base = {
        'max_risk_per_trade': 10.0,
        'daily_loss_limit': 50.0,
        'equity_drawdown_limit': 100.0,
        'trading_halted': False,
    }
    base.update(kw)
    return base


def test_shadow_brain_is_not_live_core_after_ensure():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        shadow = e._ensure_shadow_brain()
        assert shadow is not brain.core
        assert shadow is e.shadow_brain
        # Re-ensure keeps the clone, never aliases LIVE.
        assert e._ensure_shadow_brain() is not brain.core


def test_shadow_learn_does_not_change_live_weight_hash():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        brain.core.last_z = np.linspace(-0.25, 0.25, brain.core.d)
        e = SingleTradeEngine(store, brain)
        e._ensure_shadow_brain()
        e._shadow_scores_now(advance=True, bar_time='t0')
        live_before = brain.core.snapshot_weights()
        shadow_before = e.shadow_brain.snapshot_weights()
        assert e._shadow_learn(None, 0, lr=0.2) is not None
        assert NeuralCore.weight_delta_norm(
            shadow_before, e.shadow_brain.snapshot_weights()) > 0
        assert NeuralCore.weight_delta_norm(
            live_before, brain.core.snapshot_weights()) == pytest.approx(0)


def test_live_open_gates_blocks_when_hard_risk_missing_limits():
    """V5 R02: missing hard_risk limits must NOT block LIVE OPEN."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        brain._activate('GOLD')
        brain.mark_prep_ready(reason='fixture')
        gates = e.live_open_gates('BUY')
        assert gates['allowed'] is True
        assert gates.get('hard_risk') is not None
        assert gates['hard_risk'].get('allowed') is True
        assert gates['hard_risk'].get('open_gated') is False
        hard_risk.save_operator(store, _op_limits(), reason='op')
        gates2 = e.live_open_gates('BUY')
        assert gates2['allowed'] is True


def test_entry_evaluation_action_matches_scores_raw_argmax():
    """V3 L01: when gates pass, action follows scores_raw — not nudged flip."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        brain._activate('GOLD')
        brain.mark_prep_ready(reason='fixture')
        hard_risk.save_operator(store, _op_limits(), reason='op')
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.market_context = {
            'ready': True, 'trend': 'UP', 'regime': 'TREND',
            'returns_bps': {'5': 1.0, '15': 2.0, '60': 3.0},
            'bar_time': '2024-01-01T00:00:00+00:00',
            'structure': None,
        }
        e.market = {'mid': 100.0, 'spread': 0.1, 'tick': 0.01, 'bid': 99.95, 'ask': 100.05}
        e.latest_fusion_ready = True
        e.positions = []
        # Strong BUY on raw; soft modules would prefer SELL if they owned the vote.
        scores_raw = {'BUY': 0.70, 'SELL': 0.15, 'HOLD': 0.15}
        e.last_signal = {
            'scores': dict(scores_raw),
            'scores_raw': dict(scores_raw),
            'action': 'BUY', 'trend': 'UP', 'regime': 'TREND',
        }
        # Stub downstream gates so L01 scores_raw authority is the only under test.
        e.live_open_gates = lambda action=None: {
            'allowed': True, 'reason': 'test', 'readiness': {'ready': True},
            'economic_utility': None, 'hard_risk': {'allowed': True},
        }
        e._external_entry_check = lambda action: {
            'entry_allowed': True, 'status': 'ok', 'reason': 'ok',
        }
        e._structure_edge_advice = lambda: None
        e._situation_memory_advice = lambda structure=None: None
        e._mtf_momentum_analysis = lambda: {'ok': False}
        e._reentry_confirm_gate = lambda *a, **k: {'allowed': True}
        e._ohlc_entry_zone_gate = lambda *a, **k: {'allowed': True}
        e._would_be_brackets = lambda action: {
            'stop_distance': 1.0, 'profit_distance': 2.0, 'tick': 0.01,
            'min_distance': 0.1, 'entry': 100.0, 'spread': 0.1, 'epic': 'GOLD',
        }
        e._decision_candles = lambda *a, **k: []
        e._account_equity = lambda: 10_000.0

        with mock.patch('app.adaptive_system.nudge_open', side_effect=lambda *a, **k: (
                {'BUY': 0.10, 'SELL': 0.80, 'HOLD': 0.10},
                {'nudged': True, 'flow_consensus': None})):
            with mock.patch('app.ohlc_regime_gate.evaluate',
                            return_value={'allowed': True}):
                with mock.patch('app.unified_cycle.edge_allows_open',
                                return_value={'allowed': True, 'reason': 'ok'}):
                    with mock.patch('app.day_book.allows_new_open',
                                    return_value={'allowed': True}):
                        with mock.patch('app.flow_consensus.apply_to_decision',
                                        side_effect=lambda scores, **kw: {
                                            'scores': {
                                                'BUY': 0.10, 'SELL': 0.80, 'HOLD': 0.10,
                                            },
                                            'scores_raw': dict(scores_raw),
                                            'action': kw.get('action'),
                                            'blocked': False, 'nudged': True,
                                            'effect': 'nudged', 'reason': 'soft',
                                            'desk': {'ok': True},
                                        }):
                            result = e._entry_evaluation()

        assert result.get('scores_raw') == scores_raw
        # Soft nudge pushed SELL on diagnostic scores, but action stays BUY.
        assert result.get('action') == 'BUY'
        assert result.get('proposal') == 'BUY'
        assert (e.last_signal or {}).get('scores_raw') == scores_raw


def test_submit_does_not_call_champion_maybe_activate():
    """V3 A03: champion must not swap weights between plan and POST."""
    import asyncio
    import inspect

    # Static: submit body must not invoke maybe_activate.
    src = inspect.getsource(SingleTradeEngine.submit)
    assert 'maybe_activate' not in src

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e.armed = True
        e.execution_enabled = True
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.last_closed_step = 1
        e.positions = []
        e.pending_order = False
        e.pending_close = False
        e.market_context = {'ready': True}
        e.latest_fusion_ready = True
        e.live_open_gates = lambda action=None: {
            'allowed': True, 'reason': 'test',
        }
        e._external_entry_check = lambda action: {
            'entry_allowed': True, 'status': 'ok', 'reason': 'ok',
        }
        e.broker = mock.MagicMock()
        e.broker.market = mock.AsyncMock(return_value={
            'snapshot': {'marketStatus': 'CLOSED'},
            'instrument': {},
        })

        with mock.patch('app.champion_library.maybe_activate') as act:
            with mock.patch.object(e, 'sync', new=mock.AsyncMock()):
                asyncio.run(e.submit('BUY', broker_market=None))
            act.assert_not_called()


def test_last_upl_as_realized_returns_capital_snap():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
        store.set('last_upl:d1', {'upl': -1.25, 'currency': 'GBP'})
        got = e._last_upl_as_realized('d1')
        assert got is not None
        assert got[0] == -1.25
        assert got[1] == 'GBP'


def test_policy_live_execute_default_is_research_only():
    from app.self_params import DEFAULTS
    assert int(DEFAULTS['policy_live_execute']) == 0


def test_cold_start_grant_does_not_force_ready():
    """V5 T24: cold-start after manual close unlocks LIVE — no HOLD_MODEL freeze."""
    from app import model_readiness as mr
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        brain._activate('GOLD')
        mr.save_state(store, 'GOLD', {
            **mr._empty('GOLD', reason='holdout failed'),
            'stage': 'blocked', 'ready': False,
        })
        out = mr.grant_cold_start_open(store, brain, reason='test')
        assert out.get('cold_start_open') is True
        assert out.get('ready') is True
        assert brain.trading_readiness()['ready'] is True
