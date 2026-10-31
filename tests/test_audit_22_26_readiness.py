"""Audit #22–#26: prep/eval readiness, utility vs score edge, metric labels, CLOSE diagnostic."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app import model_readiness as mr


def _candles(n=120, *, start=None, trend=0.02, spread=0.02):
    start = start or datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    rows = []
    px = 100.0
    for i in range(n):
        o = px
        c = px + trend
        h = max(o, c) + 0.05
        l = min(o, c) - 0.05
        rows.append({
            'at': (start + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c, 'spread': spread,
        })
        px = c
    return rows


def test_chronological_split_no_future_leak():
    rows = _candles(120)
    train, hold, all_rows = mr.chronological_split(rows)
    assert len(all_rows) == 120
    assert train and hold
    assert train[-1]['_dt'] < hold[0]['_dt']
    # Holdout starts strictly after train — no shuffle / reverse.
    assert [r['_dt'] for r in train] == sorted(r['_dt'] for r in train)
    assert [r['_dt'] for r in hold] == sorted(r['_dt'] for r in hold)


def test_candle_load_stage_does_not_train():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain._activate('GOLD')
        steps_before = brain.core.steps
        state = mr.mark_candles_loaded(store, 'GOLD', _candles(100))
        assert state['stage'] == 'candles_loaded'
        assert state['ready'] is False
        assert 'training not started' in state['reason'].lower() or 'Need' in state['reason']
        assert brain.core.steps == steps_before  # load ≠ train


def test_prep_eval_blocks_until_holdout_passes_and_separates_stages():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain._activate('GOLD')
        # Too few candles → not ready.
        short = mr.run_prep_eval(store, brain, _candles(40), tick_size=0.01, spread_fallback=0.02)
        assert short['ready'] is False
        assert short['stage'] in ('candles_loaded', 'blocked', 'idle')
        assert short['data_load_separate_from_train'] is True
        assert short['future_leak'] is False


def test_argmax_kept_but_score_edge_is_not_utility_without_readiness():
    with tempfile.TemporaryDirectory() as d:
        p = AutoEvolution(Store(Path(d) / 'db.sqlite3'), Path(d) / 'pol.py')
        # Small edge 0.05 < default MIN_EDGE 0.06 → evolved policy HOLDs.
        weak = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}
        weak_out = p.evaluate_neural_entry(weak)
        assert weak_out['action'] == 'HOLD' and weak_out['proposal'] == 'BUY'
        assert weak_out['evidence']['score_edge_is_not_trading_advantage'] is True
        scores = {'BUY': 0.48, 'SELL': 0.30, 'HOLD': 0.22}  # edge clears policy
        bare = p.evaluate_neural_entry(scores)
        assert bare['action'] == 'BUY'
        assert bare['evidence']['score_edge_is_not_trading_advantage'] is True
        # Explicit not-ready blocks LIVE OPEN despite argmax.
        blocked = p.evaluate_neural_entry(
            scores, readiness={'ready': False, 'reason': 'prep pending'})
        assert blocked['action'] == 'HOLD'
        assert blocked['proposal'] == 'BUY'
        assert 'prep/eval' in blocked['reason'].lower() or 'not ready' in blocked['reason'].lower()
        # Ready via prep, thin history → prep holdout stands in for utility.
        ok = p.evaluate_neural_entry(
            scores, history=[], readiness={
                'ready': True, 'eval_samples': 30,
                'minute_direction_accuracy': 0.4, 'holdout_net_bps_mean': 0.1,
            })
        assert ok['action'] == 'BUY'
        assert ok['economic_utility']['source'] == 'prep_eval_holdout'
        # History passed but empty of labels and no readiness → block (score edge ≠ edge).
        denied = p.evaluate_neural_entry(scores, history=[{'label': None}], readiness=None)
        assert denied['action'] == 'HOLD'
        assert 'score edge is not enough' in denied['reason'].lower() or 'utility' in denied['reason'].lower()


def test_zone_shift_still_not_entry_bans():
    with tempfile.TemporaryDirectory() as d:
        p = AutoEvolution(Store(Path(d) / 'db.sqlite3'), Path(d) / 'pol.py')
        scores = {'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1}
        ready = {'ready': True, 'eval_samples': 40, 'holdout_net_bps_mean': 1.0}
        # Against trend / SHIFT — still argmax direction when readiness ok.
        out = p.evaluate_neural_entry(
            scores, returns_bps={'5': -2, '15': -3, '60': -4},
            trend='DOWN', regime='SHIFT', history=[], readiness=ready)
        assert out['action'] == 'BUY'


def test_live_open_gates_and_submit_helper():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        brain._activate('SIM')
        gates = engine.live_open_gates('BUY')
        assert gates['allowed'] is False
        brain.mark_prep_ready(reason='test fixture')
        gates2 = engine.live_open_gates('BUY')
        assert gates2['allowed'] is True
        assert gates2['readiness']['ready'] is True


def test_snapshot_separates_minute_accuracy_trade_win_rate_and_close_diagnostic():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        engine = SingleTradeEngine(store, brain)
        brain._activate('SIM')
        brain.correct = 40
        brain.labeled = 100
        brain.mark_prep_ready()
        # One winning closed cash trade.
        store.db.execute(
            '''INSERT INTO trades(deal_id,epic,direction,size,entry,exit,pnl,pnl_source,pnl_currency,opened,closed)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            ('d1', 'SIM', 'BUY', '1', 1, 2, 1.5, 'broker', 'GBP', 't0', 't1'))
        store.db.commit()
        snap = engine.snapshot()
        assert snap['minute_direction_accuracy'] == pytest.approx(0.4)
        assert snap['trade_win_rate'] == pytest.approx(1.0)
        assert snap['model_score_is_calibrated_profit_probability'] is False
        assert snap['neural_close_diagnostic_only'] is True
        assert snap['neural_close_executable'] is False
        assert snap['neural_market_exit'] is False
        assert 'evolved policy' in snap['neural_entry_rule'].lower()
        assert 'policy_live_execute' in snap['neural_entry_rule']
        assert snap['metric_labels']['minute_direction_accuracy']
        assert 'win rate' in snap['metric_labels']['trade_win_rate'].lower() or 'Win' in snap['metric_labels']['trade_win_rate']


def test_runtime_hub_exposes_separated_metrics():
    from app.runtime_hub import RuntimeHub
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        # Two closed broker cash rows.
        for i, pnl in enumerate((2.0, -1.0)):
            store.db.execute(
                '''INSERT INTO trades(deal_id,epic,direction,size,entry,exit,pnl,pnl_source,pnl_currency,opened,closed)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (f'd{i}', 'SIM', 'BUY', '1', 1, 2, pnl, 'broker', 'GBP', 't0', 't1'))
        store.db.commit()
        # Build a minimal hub-like stats call.
        class _Fake:
            platform = None
        hub = RuntimeHub.__new__(RuntimeHub)
        hub.platform = None
        stats = RuntimeHub._trade_stats(hub, store, 'GBP')
        assert stats['wins'] == 1 and stats['losses'] == 1
        assert stats['trade_win_rate'] == pytest.approx(0.5)
        assert stats['metric'] == 'closed_trade_win_rate'


def test_ui_copy_no_longer_claims_zone_shift_entry_bans():
    ui = Path('app/web/ui.js').read_text(encoding='utf8')
    assert 'not mid-chop, not against 15m/60m, not SHIFT' not in ui
    assert 'zone/SHIFT are evidence only' in ui or 'not bans' in ui
    assert 'diagnostic' in ui.lower()
    assert 'not profit probability' in ui.lower()
    assert 'trade win rate' in ui.lower() or 'Closed-trade win rate' in ui
