"""Day book — robot equity risk, day +/BE, multi-pos, adaptive lot, anti-HOLD."""
import tempfile
from decimal import Decimal
from pathlib import Path

from app.engine import Store
from app import day_book as db
from app.autonomy import AutoEvolution


def _store(tmp):
    return Store(Path(tmp) / 't.sqlite3')


def test_budgets_from_equity():
    params = {
        'profit_target_frac': 0.01,
        'loss_budget_frac': 0.02,
        'risk_per_trade_frac': 0.005,
        'be_band_frac': 0.001,
    }
    b = db.budgets(equity=10_000.0, params=params, daily={})
    assert b['profit_target'] == 100.0
    assert b['loss_budget'] == 200.0
    assert b['risk_per_trade'] == 50.0
    assert b['be_band'] == 10.0


def test_phase_hunt_protect_defend_done():
    params = db.load_params(None)
    daily = {'realized_pnl': 0.0, 'closes': 0}
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_HUNT

    daily = {'realized_pnl': 150.0, 'closes': 2}  # above ~1% target
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=1) == db.PHASE_PROTECT
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_DONE

    daily = {'realized_pnl': -250.0, 'closes': 3}
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_DEFEND


def test_allows_multi_when_edge_strong():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        positions = [
            {'dealId': '1', 'epic': 'GOLD', 'size': '1', 'direction': 'BUY'},
        ]
        denied = db.allows_new_open(
            store=s, equity=10_000.0, positions=positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=0.02)
        assert denied['allowed'] is False
        assert denied['reason'] == 'add_needs_stronger_edge'

        ok = db.allows_new_open(
            store=s, equity=10_000.0, positions=positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=0.12)
        assert ok['allowed'] is True
        assert ok['phase'] == db.PHASE_HUNT


def test_propose_size_scales_with_equity_risk():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        daily = db.load_daily(s, equity=10_000.0)
        params = db.load_params(s)
        out = db.propose_size(
            mandate_size='1', equity=10_000.0, daily=daily, params=params,
            stop_ticks=20, tick_value=1.0, positions=[], score_edge=0.1)
        assert out['allowed'] is True
        assert out['size'] is not None
        assert isinstance(out['size'], Decimal)
        assert out['size'] > 0


def test_soft_nudge_conviction_renormalizes():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        scores = {'BUY': 0.55, 'SELL': 0.20, 'HOLD': 0.25}
        nudged, meta = db.soft_nudge_conviction(
            scores, phase=db.PHASE_HUNT, store=s)
        assert meta['nudged'] is True
        assert nudged['BUY'] > scores['BUY']
        assert nudged['HOLD'] < scores['HOLD']
        assert abs(sum(nudged.values()) - 1.0) < 1e-9
        assert AutoEvolution._safe_scores(nudged)


def test_day_phase_protect_tilts_hold():
    scores = {'BUY': 0.45, 'SELL': 0.35, 'HOLD': 0.20}
    nudged, meta = db.soft_nudge_day_phase(scores, phase=db.PHASE_PROTECT)
    assert meta['nudged'] is True
    assert nudged['HOLD'] > scores['HOLD']


def test_record_close_updates_phase():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        db.record_close(s, pnl=120.0, equity=10_000.0, open_count=0)
        snap = db.snapshot(s, equity=10_000.0, positions=[])
        assert snap['daily']['realized_pnl'] == 120.0
        assert snap['phase'] == db.PHASE_DONE


def test_foreign_epic_blocks_add():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        positions = [{'dealId': '1', 'epic': 'SILVER', 'size': '1'}]
        out = db.allows_new_open(
            store=s, equity=10_000.0, positions=positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=0.2)
        assert out['allowed'] is False
        assert out['reason'] == 'foreign_epic_open'
