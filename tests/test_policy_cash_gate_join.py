"""GOLD close → profit row with scores → policy cash_gate path."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution, TEMPLATES
from app.engine import Store
from app.profit_learning import ProfitLearning


def test_gold_close_record_carries_scores_into_cash_rows():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        pl = ProfitLearning(store)
        scores = {'BUY': 0.62, 'SELL': 0.18, 'HOLD': 0.20}
        pl.record_closed(
            deal_id='g1', epic='GOLD', direction='BUY',
            entry=2650.0, exit_level=2655.0, broker_pnl=4.5,
            currency='GBP', spread=0.3, scores=scores,
            bars=None, stop_distance=2.0, profit_distance=4.0, tick=0.1)
        pl.record_closed(
            deal_id='g2', epic='GOLD', direction='SELL',
            entry=2660.0, exit_level=2655.0, broker_pnl=3.0,
            currency='GBP', spread=0.3,
            scores={'BUY': 0.2, 'SELL': 0.55, 'HOLD': 0.25})
        # Enough rows for gate — pad with scored copies of different deals.
        for i in range(10):
            pl.record_closed(
                deal_id=f'g{i+3}', epic='GOLD',
                direction='BUY' if i % 2 == 0 else 'SELL',
                entry=2650.0 + i, exit_level=2652.0 + i,
                broker_pnl=1.0 if i % 3 else -0.5,
                currency='GBP', spread=0.3,
                scores={'BUY': 0.5 + (i % 3) * 0.05, 'SELL': 0.3, 'HOLD': 0.2})
        cash = AutoEvolution.policy_cash_rows(pl.history)
        assert len(cash) >= 12
        assert all(r.get('scores') and r.get('direction') and r.get('actual_pnl') is not None
                   for r in cash)
        # brain.history alone would not activate cash_gate
        assert AutoEvolution.policy_cash_rows([
            {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'label': 0}
        ]) == []


def test_evolve_receives_cash_trades_not_brain_history_alone():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.06),
            encoding='utf8')
        evo = AutoEvolution(store, path)
        # Label history (M1) — enough for label gate to run.
        hist = []
        for i in range(30):
            hist.append({
                'label': i % 3,
                'scores': {'BUY': 0.45 + (i % 5) * 0.02, 'SELL': 0.30, 'HOLD': 0.25},
                'spread_bps': 2.0,
            })
        # Cash rows with scores+direction — what engine must pass.
        cash = []
        for i in range(14):
            side = 'BUY' if i % 2 == 0 else 'SELL'
            cash.append({
                'scores': {'BUY': 0.5, 'SELL': 0.35, 'HOLD': 0.15},
                'direction': side,
                'actual_pnl': 1.0 if side == 'BUY' else -0.4,
                'simulated': {'buy': 1.0, 'sell': -0.4, 'hold': 0.0, 'method': 'path'},
            })
        # Without cash_trades, joined from history-without-pnl → cash_gate off.
        evo.evolve(0.5, 30, hist, force=True, cash_trades=[])
        # With cash — gate arms (may or may not promote; must not crash).
        evo.evolve(0.5, 40, hist, force=True, cash_trades=cash)
        assert store.get('evolution_attempts') is not None
