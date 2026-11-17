"""GOLD close → profit row with scores → policy cash_gate path."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution, TEMPLATES
from app.engine import Store
from app.profit_learning import ProfitLearning


def _scored_cash(n=24, *, epic='GOLD', currency='GBP', pnl_buy=1.0, pnl_sell=-0.4):
    """≥20 rows so 70/30 split yields ≥6 train and ≥6 val (cash_gate arms)."""
    cash = []
    for i in range(n):
        side = 'BUY' if i % 2 == 0 else 'SELL'
        entry = 2650.0 + i
        # Price move matches side so cash_per_price_unit can scale.
        exit_px = entry + (2.0 if side == 'BUY' else -2.0)
        cash.append({
            'scores': {'BUY': 0.55, 'SELL': 0.30, 'HOLD': 0.15},
            'direction': side,
            'actual_pnl': pnl_buy if side == 'BUY' else pnl_sell,
            'epic': epic,
            'currency': currency,
            'entry': entry,
            'exit': exit_px,
            'simulated': {'buy': 2.0, 'sell': -2.0, 'hold': 0.0, 'method': 'path'},
        })
    return cash


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
        for i in range(20):
            pl.record_closed(
                deal_id=f'g{i+2}', epic='GOLD',
                direction='BUY' if i % 2 == 0 else 'SELL',
                entry=2650.0 + i, exit_level=2652.0 + i,
                broker_pnl=1.0 if i % 3 else -0.5,
                currency='GBP', spread=0.3,
                scores={'BUY': 0.5 + (i % 3) * 0.05, 'SELL': 0.3, 'HOLD': 0.2})
        cash = AutoEvolution.policy_cash_rows(pl.history, epic='GOLD', currency='GBP')
        assert len(cash) >= 20
        assert all(r.get('epic') == 'GOLD' and r.get('currency') == 'GBP' for r in cash)
        assert AutoEvolution.policy_cash_rows([
            {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'label': 0}
        ]) == []


def test_policy_cash_rows_isolates_epic_and_currency():
    mixed = [
        {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'direction': 'BUY',
         'actual_pnl': 1.0, 'epic': 'GOLD', 'currency': 'GBP', 'entry': 1, 'exit': 2},
        {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'direction': 'BUY',
         'actual_pnl': 9.0, 'epic': 'SILVER', 'currency': 'GBP', 'entry': 1, 'exit': 2},
        {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'direction': 'SELL',
         'actual_pnl': -0.5, 'epic': 'GOLD', 'currency': 'USD', 'entry': 1, 'exit': 2},
        {'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}, 'direction': 'SELL',
         'actual_pnl': 0.5, 'epic': 'GOLD', 'currency': 'GBP', 'entry': 1, 'exit': 2},
    ]
    gold_gbp = AutoEvolution.policy_cash_rows(mixed, epic='GOLD', currency='GBP')
    assert len(gold_gbp) == 2
    assert all(r['epic'] == 'GOLD' and r['currency'] == 'GBP' for r in gold_gbp)
    # Modal bucket without filters → GOLD/GBP (2 rows) wins over singles.
    auto = AutoEvolution.policy_cash_rows(mixed)
    assert len(auto) == 2
    assert all(r['epic'] == 'GOLD' and r['currency'] == 'GBP' for r in auto)


def test_evolve_arms_cash_gate_with_20_plus_trades():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        path = Path(d) / 'policy.py'
        path.write_text(
            TEMPLATES['edge'].format(
                confidence=0.60, reversal=0.30, min_hold=3, margin=0.06),
            encoding='utf8')
        evo = AutoEvolution(store, path)
        hist = []
        for i in range(40):
            hist.append({
                'label': i % 3,
                'scores': {'BUY': 0.45 + (i % 5) * 0.02, 'SELL': 0.30, 'HOLD': 0.25},
                'spread_bps': 2.0,
            })
        cash14 = _scored_cash(14)
        evo.evolve(0.5, 40, hist, force=True, cash_trades=cash14)
        assert evo.last_cash_gate is False  # 14 → val≈4 < 6

        cash24 = _scored_cash(24)
        evo.last_change = 0  # clear trade-timed debounce between force runs
        evo.evolve(0.5, 50, hist, force=True, cash_trades=cash24)
        assert evo.last_cash_gate is True
        assert store.get('evolution_cash_gate') is True
        assert int(store.get('evolution_cash_rows') or 0) == 24


def test_cash_gate_rejects_worse_account_pound_candidate():
    """Opposite-side price units must not beat same-side £ via unit mix-up."""
    def always_buy(scores, threshold=None):
        return 'BUY'

    def always_sell(scores, threshold=None):
        return 'SELL'

    # BUY trades made +£; raw simulated sell=-3 price points would look
    # "worse" numerically than +12 only by accident — with unit mix, -3 vs +12
    # wrongly compared. After fix, opposite without £ scale is skipped.
    trades = []
    for i in range(12):
        trades.append({
            'scores': {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1},
            'direction': 'BUY',
            'actual_pnl': 12.0,
            'entry': 2650.0 + i,
            'exit': 2652.0 + i,  # +2 price → £12 ⇒ scale ≈ 6 £/pt
            'epic': 'GOLD', 'currency': 'GBP',
            'simulated': {'buy': 2.0, 'sell': -3.0, 'hold': 0.0, 'method': 'path'},
        })
    buy_mean, buy_n = AutoEvolution._replay_trade_cash(always_buy, trades, min_trades=6)
    sell_mean, sell_n = AutoEvolution._replay_trade_cash(always_sell, trades, min_trades=6)
    assert buy_n == 12
    assert buy_mean == pytest.approx(12.0)
    # Opposite scaled: -3 price × (~6 £/pt) ≈ -18 £ — worse than +12, not -3.
    assert sell_n == 12
    assert sell_mean == pytest.approx(-18.0, rel=0.05)
    assert sell_mean < buy_mean
