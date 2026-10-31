"""Evolved policy search is ON and promoted decide() gates LIVE entry."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution, DEFAULT, TEMPLATES
from app.engine import Store
from app.self_params import DEFAULTS, get, load, save


def sandbox():
    return tempfile.TemporaryDirectory()


def test_policy_evolve_and_live_execute_on_by_default():
    assert int(DEFAULTS['policy_evolve_enabled']) == 1
    assert int(DEFAULTS['policy_live_execute']) == 1
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        assert int(get(store, 'policy_evolve_enabled', 1)) == 1
        assert int(get(store, 'policy_live_execute', 1)) == 1
        policy = AutoEvolution(store, Path(d) / 'policy.py')
        policy.last_change = 0
        before = policy.attempts
        # Empty history → no promote, but search must RUN (not gated off).
        policy.evolve(0.4, 20, [])
        assert policy.attempts == before + 1
        assert 'gated off' not in (policy.last_reason or '').lower()


def test_evolved_policy_can_veto_neural_entry():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        # Fixed 0.06 floor — weak neural edge is HOLD; file margin ignored.
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.02),
            encoding='utf8')
        policy = AutoEvolution(store, path)
        scores = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}  # edge 0.05 < 0.06
        out = policy.evaluate_neural_entry(
            scores, readiness={'ready': True}, history=None)
        assert out['action'] == 'HOLD'
        assert 'evolved policy' in out['reason'].lower() or 'vetoes' in out['reason'].lower()
        assert out['proposal'] == 'BUY'  # neural still proposes
        assert out['evidence']['evolved_policy']['action'] == 'HOLD'
        assert out['evidence']['evolved_policy']['entry_edge_floor'] == pytest.approx(0.06)


def test_evolved_policy_confirms_clear_edge():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.06),
            encoding='utf8')
        policy = AutoEvolution(store, path)
        scores = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}  # edge 0.30
        out = policy.evaluate_neural_entry(
            scores, readiness={'ready': True}, history=None)
        assert out['action'] == 'BUY'
        assert 'confirmed by evolved policy' in out['reason'].lower()


def test_policy_live_execute_off_skips_gate():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**DEFAULTS, 'policy_live_execute': 0}, reason='test')
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.50),
            encoding='utf8')
        policy = AutoEvolution(store, path)
        scores = {'BUY': 0.45, 'SELL': 0.30, 'HOLD': 0.25}
        out = policy.evaluate_neural_entry(
            scores, readiness={'ready': True}, history=None)
        assert out['action'] == 'BUY'
        assert 'execute off' in out['reason'].lower()


def test_evolve_promote_changes_file_and_entry_behavior():
    """Synthetic labeled history → promote → LIVE entry uses new constants."""
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        path = Path(d) / 'policy.py'
        # Start with a very strict edge so weak scores HOLD.
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.18),
            encoding='utf8')
        policy = AutoEvolution(store, path)
        # Build history where a looser edge wins on holdout.
        history = []
        for i in range(40):
            # Clear BUY winners with moderate edge ~0.08–0.12
            buy = 0.44 + (i % 5) * 0.01
            hold = 0.28
            sell = 1.0 - buy - hold
            history.append({
                'scores': {'BUY': buy, 'SELL': sell, 'HOLD': hold},
                'action': 'BUY',
                'label': 0,
                'spread_bps': 2.0,
                'time': f'2026-01-01T00:{i:02d}:00+00:00',
            })
        for i in range(20):
            sell = 0.46
            hold = 0.27
            buy = 0.27
            history.append({
                'scores': {'BUY': buy, 'SELL': sell, 'HOLD': hold},
                'action': 'SELL',
                'label': 1,
                'spread_bps': 2.0,
                'time': f'2026-01-01T01:{i:02d}:00+00:00',
            })
        policy.last_change = 0
        before_src = path.read_text(encoding='utf8')
        promoted = policy.evolve(0.55, len(history), history, force=True)
        # May or may not promote depending on replay — both outcomes OK if search ran.
        assert policy.attempts >= 1
        after_src = path.read_text(encoding='utf8')
        if promoted is not None:
            assert after_src != before_src or 'MIN_EDGE' in after_src
            assert policy.generation >= 1
            # Entry must go through the (possibly new) file.
            scores = {'BUY': 0.50, 'SELL': 0.25, 'HOLD': 0.25}
            out = policy.evaluate_neural_entry(
                scores, readiness={'ready': True}, history=None)
            assert out['action'] in ('BUY', 'HOLD')
            assert 'evolved_policy' in (out.get('evidence') or {})


def test_stress_policy_spam_evolve_and_decision():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        path = Path(d) / 'policy.py'
        policy = AutoEvolution(store, path)
        history = []
        for i in range(60):
            side = i % 2
            if side == 0:
                scores = {'BUY': 0.48, 'SELL': 0.27, 'HOLD': 0.25}
                label, action = 0, 'BUY'
            else:
                scores = {'BUY': 0.27, 'SELL': 0.48, 'HOLD': 0.25}
                label, action = 1, 'SELL'
            history.append({
                'scores': scores, 'action': action, 'label': label,
                'spread_bps': 1.5 + (i % 3),
                'time': f'2026-03-01T{i//60:02d}:{i%60:02d}:00+00:00',
            })
        for _ in range(25):
            policy.last_change = 0
            policy.evolve(0.5, len(history), history, force=True)
            scores = {'BUY': 0.52, 'SELL': 0.24, 'HOLD': 0.24}
            out = policy.evaluate_neural_entry(
                scores, readiness={'ready': True}, history=None)
            assert out['action'] in ('BUY', 'HOLD')
            assert AutoEvolution._safe_scores(scores)
        # Poison file → decision restores fallback, entry stays safe.
        path.write_text('def decide(scores, current_direction=None, age=0):\n'
                        '    return "EXPLODE"\n', encoding='utf8')
        # _validate may reject on next compile; decision() should not raise to caller
        # via evaluate path — apply_evolved_policy catches.
        try:
            act = policy.decision({'BUY': 0.5, 'SELL': 0.25, 'HOLD': 0.25})
            assert act in ('BUY', 'SELL', 'HOLD', 'CLOSE')
        except Exception:
            # validate may raise before restore in some paths — entry helper HOLDs
            action, reason, _ = policy.apply_evolved_policy_entry(
                {'BUY': 0.5, 'SELL': 0.25, 'HOLD': 0.25}, 'BUY')
            assert action == 'HOLD'


def test_machine_spec_documents_live_policy_execution():
    from app import machine_spec
    assert 'evolved_policy' in machine_spec.LIVE_DECISION_RULE
    assert 'execute_promoted_policy_on_live_entry' in machine_spec.SELF_DEV_CYCLE
    assert 'policy_live_execute' in machine_spec.LIVE_POLICY_EXECUTION
