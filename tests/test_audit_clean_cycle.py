"""Audit: clean OPEN→PROTECT(TP)→close→£ cycle invariants."""
import math
import tempfile
import time
from pathlib import Path

import pytest

from app import bracket_learn, day_book, hold_shadow as hs
from app.bracket_learn import FIXED_TP_SL_RATIO, decide_brackets_from_ohlc
from app.engine import SingleTradeEngine, Store
from app.math_rules import stop_take
from app.profit_learning import label_from_profit
from app import unified_cycle as uc


def test_decide_brackets_always_half_sl_vs_tp():
    d = decide_brackets_from_ohlc(
        {'kappa': 0.7, 'range_distance': 2.0, 'max_tp': 0.5, 'ready': True},
        tick=0.01, spread=0.05, min_distance=0.2)
    assert d['rr_locked'] is True
    assert d['profit_distance'] / d['stop_distance'] == pytest.approx(
        FIXED_TP_SL_RATIO, abs=0.05)


def test_stop_take_relocks_tp_after_broker_min_raise():
    out = stop_take(100, 'BUY', 0.1, 0.05, 1.0, kappa=2, sl_scale=1,
                    range_distance=0.2)
    assert float(out['profit_distance']) / float(out['stop_distance']) == pytest.approx(
        2.0, abs=0.05)


def test_hold_shadow_teaches_opposite_tp_without_trail_poison():
    bars = []
    px = 100.0
    for i in range(20):
        o, c = px, px - 0.5
        bars.append({'mid': c, 'high': o + 0.05, 'low': c - 0.05, 'spread': 0.2})
        px = c
    out = hs.evaluate_sides(
        proposal='BUY', bars=bars, spread=0.2,
        stop_distance=1.0, profit_distance=1.0, tick=0.1, min_distance=0.2)
    assert out['opposite_hit_tp'] is True
    assert out['teach_opposite'] is True


def test_reverse_loss_teaches_sell_not_hold_when_structure_says_sell():
    bars = [
        {'open': 100., 'high': 100.8, 'low': 99.9, 'close': 100.5, 'spread': 0.2},
        {'open': 100.5, 'high': 100.6, 'low': 98.2, 'close': 98.4, 'spread': 0.2},
    ]
    out = label_from_profit(
        'BUY', -1.5, entry=100., exit_level=98.4, spread=0.2,
        bars=bars, stop_distance=1.0, profit_distance=1.5, tick=0.01,
        path_mfe=0.8, path_mae=1.6,
        structure_frame_advice={'advice': 'SELL', 'confidence': 0.7})
    assert out['label'] == 1


def test_pending_pnl_due_when_upl_shown():
    e = object.__new__(SingleTradeEngine)
    base = {'attempts': 6, 'last_attempt_at': time.time(), 'upl_shown': True}
    assert SingleTradeEngine._pending_pnl_due(e, base) is True


def test_edge_reason_no_false_loss_streak():
    gate = uc.edge_allows_open(
        {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25},
        {'action': 'BUY'}, uc.default_state())
    assert gate['allowed'] is False
    assert 'loss streak' not in gate['reason']
    assert 'calibrated floor' in gate['reason']


def test_needs_profit_restore_for_robot_open_without_tp():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        e = SingleTradeEngine(store, brain=None)
        store.set('entry_brain:d1', {
            'robot_open': True, 'profit_distance': 2.0, 'bracket_source': 'ohlc_zone'})
        p = {'dealId': 'd1', 'stopLevel': 99.0, 'profitLevel': None}
        assert e._needs_profit_restore(p) is True
        store.set('adaptive_protection:d1', {'be_locked': True, 'phase': 'TRAIL'})
        assert e._needs_profit_restore(p) is False


def test_day_book_reconcile_heals_opens():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({'dealId': 'a', 'epic': 'GOLD', 'size': '0.1',
                            'direction': 'BUY', 'level': 100})
        store.upsert_trade({'dealId': 'b', 'epic': 'GOLD', 'size': '0.1',
                            'direction': 'SELL', 'level': 100})
        store.close_trade('a', 101, 0.5, currency='GBP', source='broker')
        daily = day_book.reconcile_daily_from_trades(store, equity=10000)
        assert int(daily.get('opens') or 0) >= 1
        assert int(daily.get('closes') or 0) >= 1
