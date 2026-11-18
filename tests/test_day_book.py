"""Day book — robot equity risk, day +/BE, multi-pos, adaptive lot, anti-HOLD."""
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

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
        'size_full_balance': 0,
    }
    b = db.budgets(equity=10_000.0, params=params, daily={})
    assert b['profit_target'] == 100.0
    assert b['loss_budget'] == 200.0
    assert b['risk_per_trade'] == 50.0
    assert b['be_band'] == 10.0
    full = db.budgets(
        equity=20.0,
        params={**params, 'size_full_balance': 1},
        daily={})
    assert full['risk_per_trade'] == 20.0
    assert full['size_full_balance'] is True


def test_phase_hunt_protect_defend_done():
    params = db.load_params(None)
    # Defaults: no day profit cap; loss stop = 10% equity.
    assert float(params.get('profit_target_frac') or 0) == 0.0
    assert float(params.get('loss_budget_frac') or 0) == pytest.approx(0.10)

    daily = {'realized_pnl': 0.0, 'closes': 0}
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert b['profit_target'] is None
    assert b['loss_budget'] == pytest.approx(1000.0)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_HUNT

    # Wins do not flip PROTECT/DONE when profit target is off.
    daily = {'realized_pnl': 1500.0, 'closes': 2}
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=1) == db.PHASE_HUNT
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_HUNT

    # Optional profit target still works when explicitly enabled.
    capped = dict(params, profit_target_frac=0.01)
    daily = {'realized_pnl': 150.0, 'closes': 2}
    b = db.budgets(equity=10_000.0, params=capped, daily=daily)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=1) == db.PHASE_PROTECT
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_DONE

    daily = {'realized_pnl': -1000.0, 'closes': 3}  # hits 10% loss stop
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_DEFEND

    # BE-band after a scratch must stay HUNT (no auto DONE/PROTECT).
    daily = {'realized_pnl': 0.05, 'closes': 1}
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert abs(daily['realized_pnl']) <= float(b['be_band'] or 0)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_HUNT
    assert db.phase_for(daily=daily, budgets_map=b, open_count=1) == db.PHASE_HUNT


def test_risk_gates_off_allows_open_in_defend():
    """Admin toggle day_risk_gates=0 → DEFEND no longer blocks OPEN."""
    from app.self_params import load, save
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        # Seed a day already past the 10% loss stop.
        db.save_daily(s, {
            'date': db._utc_date(),
            'realized_pnl': -1500.0,
            'opens': 3,
            'closes': 3,
            'deal_ids': [],
        })
        blocked = db.allows_new_open(
            store=s, equity=10_000.0, positions=[],
            epic='GOLD', mandate_epic='GOLD', score_edge=0.2)
        assert blocked['allowed'] is False
        assert blocked['phase'] == db.PHASE_DEFEND
        params = load(s)
        params['day_risk_gates'] = 0
        save(s, params, reason='test_off')
        opened = db.allows_new_open(
            store=s, equity=10_000.0, positions=[],
            epic='GOLD', mandate_epic='GOLD', score_edge=0.2)
        assert opened['allowed'] is True
        assert opened['phase'] == db.PHASE_DEFEND
        assert opened['risk_gates'] is False
        assert 'day_risk_gates_off' in opened['reason']
        snap = db.snapshot(s, equity=10_000.0, positions=[])
        assert snap['risk_gates'] is False


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

        # Clear TREND_UP with BUY book — low edge is enough to ADD on rebound.
        aligned = db.allows_new_open(
            store=s, equity=10_000.0, positions=positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=0.025,
            trend_aligned=True)
        assert aligned['allowed'] is True

        ok = db.allows_new_open(
            store=s, equity=10_000.0, positions=positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=0.12)
        assert ok['allowed'] is True
        assert ok['phase'] == db.PHASE_HUNT


def test_propose_size_scales_with_equity_risk():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        daily = db.load_daily(s, equity=10_000.0)
        params = dict(db.load_params(s), size_full_balance=0)
        out = db.propose_size(
            mandate_size='1', equity=10_000.0, daily=daily, params=params,
            stop_ticks=20, tick_value=1.0, positions=[], score_edge=0.1)
        assert out['allowed'] is True
        assert out['size'] is not None
        assert isinstance(out['size'], Decimal)
        assert out['size'] > 0


def test_full_balance_uses_account_and_splits_multi_lot():
    """£20 account → OPEN pack cash-risk ≈ £20, split across day_open_slices."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        market = {
            'dealingRules': {
                'minDealSize': {'value': '0.01'},
                'minSizeIncrement': {'value': '0.01'},
                'maxDealSize': {'value': '50'},
            },
            'instrument': {},
            'snapshot': {'decimalPlacesFactor': 2},
        }
        params = db.load_params(s)
        assert int(params.get('size_full_balance') or 0) == 1
        daily = db.load_daily(s, equity=20.0)
        # Strong signal + stop_ticks=10, tick_value=1 → size = 20/10 = 2.0
        out = db.propose_size(
            mandate_size='0.25', equity=20.0, daily=daily, params=params,
            stop_ticks=10, tick_value=1.0, positions=[], score_edge=0.25,
            market=market)
        assert out['allowed'] is True
        assert out['size_full_balance'] is True
        assert float(out['size']) == pytest.approx(2.0)
        # Estimated cash risk ≈ full £20
        assert float(out['estimated_risk']) == pytest.approx(20.0, rel=0.05)
        slices = db.split_planned_sizes(
            out['size'], store=s, positions=[], size_grid={
                'ok': True, 'min': Decimal('0.01'), 'step': Decimal('0.01'),
                'max': Decimal('50')},
            action='BUY', structure='TREND_UP', score_edge=0.25)
        assert len(slices) == 5
        assert sum(slices) == Decimal('2.00')
        # Strong edge front-loads: lead ticket > last ticket.
        assert slices[0] > slices[-1]


def test_full_balance_no_tick_value_deploys_min_pack_not_mandate():
    """Missing Capital pip value + full_balance must not glue size to mandate."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        market = {
            'dealingRules': {
                'minDealSize': {'value': '0.01'},
                'minSizeIncrement': {'value': '0.01'},
                'maxDealSize': {'value': '50'},
            },
        }
        params = db.load_params(s)
        assert int(params.get('size_full_balance') or 0) == 1
        # Weak/zero edge → open_slices(5)×min (not mandate glue).
        out = db.propose_size(
            mandate_size='0.1', equity=15.0, daily={}, params=params,
            stop_ticks=500, tick_value=None, positions=[], score_edge=0.0,
            market=market)
        assert out['allowed'] is True
        assert out['reason'] == 'full_balance_no_tick_value_min_pack'
        assert float(out['size']) == pytest.approx(0.05)
        # Strong edge grows the no-tick pack (signal strength before submit).
        strong = db.propose_size(
            mandate_size='0.1', equity=15.0, daily={}, params=params,
            stop_ticks=500, tick_value=None, positions=[], score_edge=0.20,
            market=market)
        assert float(strong['size']) == pytest.approx(0.10)
        slices = db.split_planned_sizes(
            out['size'], store=s, positions=[], size_grid={
                'ok': True, 'min': Decimal('0.01'), 'step': Decimal('0.01'),
                'max': Decimal('50')},
            action='BUY')
        assert len(slices) == 5
        assert all(s == Decimal('0.01') for s in slices)


def test_gold_like_full_balance_splits_three_min_lots():
    """£15 + wide GOLD stop → ~0.03 total must fire 3×0.01, not one 0.03."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        market = {
            'dealingRules': {
                'minDealSize': {'value': '0.01'},
                'minSizeIncrement': {'value': '0.01'},
                'maxDealSize': {'value': '50'},
            },
        }
        params = db.load_params(s)
        # stop_ticks=500, tick_value=1 → size = 15/500 = 0.03 (unknown edge = full £)
        out = db.propose_size(
            mandate_size='0.1', equity=15.0, daily={}, params=params,
            stop_ticks=500, tick_value=1.0, positions=[], market=market)
        assert float(out['size']) == pytest.approx(0.03)
        grid = {'ok': True, 'min': Decimal('0.01'), 'step': Decimal('0.01'),
                'max': Decimal('50')}
        slices = db.split_planned_sizes(
            out['size'], store=s, positions=[], size_grid=grid, action='BUY')
        assert slices == [Decimal('0.01'), Decimal('0.01'), Decimal('0.01')]
        # Ghost sibling in positions must not collapse when force_flat (submit).
        forced = db.split_planned_sizes(
            out['size'], store=s,
            positions=[{'dealId': 'ghost', 'epic': 'GOLD', 'size': '0.01'}],
            size_grid=grid, action='BUY', force_flat=True)
        assert forced == [Decimal('0.01'), Decimal('0.01'), Decimal('0.01')]
        # Without force_flat, ADD stays one ticket.
        add = db.split_planned_sizes(
            out['size'], store=s,
            positions=[{'dealId': 'live', 'epic': 'GOLD', 'size': '0.01'}],
            size_grid=grid, action='BUY')
        assert add == [Decimal('0.03')]


def test_signal_strength_scales_full_balance_pack():
    """Stronger score_edge → larger full-balance pack (before order submit)."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        market = {
            'dealingRules': {
                'minDealSize': {'value': '0.01'},
                'minSizeIncrement': {'value': '0.01'},
                'maxDealSize': {'value': '50'},
            },
        }
        params = db.load_params(s)
        weak = db.propose_size(
            mandate_size='0.1', equity=20.0, daily={}, params=params,
            stop_ticks=10, tick_value=1.0, positions=[], score_edge=0.0,
            market=market)
        strong = db.propose_size(
            mandate_size='0.1', equity=20.0, daily={}, params=params,
            stop_ticks=10, tick_value=1.0, positions=[], score_edge=0.20,
            market=market)
        assert float(weak['size']) == pytest.approx(1.0)   # 50% of £20 / 10
        assert float(strong['size']) == pytest.approx(2.0)  # 100% of £20 / 10
        assert float(strong['size']) > float(weak['size'])


def test_edge_weighted_split_front_loads_strong_signal():
    """Strong edge → uneven multi lots (0.08/0.05/0.03…), not five equal chunks."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        grid = {'ok': True, 'min': Decimal('0.01'), 'step': Decimal('0.01'),
                'max': Decimal('50')}
        strong = db.split_planned_sizes(
            Decimal('0.19'), store=s, positions=[], size_grid=grid,
            score_edge=0.25, action='BUY', structure='TREND_UP')
        assert len(strong) == 5
        assert sum(strong) == Decimal('0.19')
        assert strong[0] >= Decimal('0.05')
        assert strong[0] > strong[-1]
        assert min(strong) == Decimal('0.01')
        weak = db.split_planned_sizes(
            Decimal('0.19'), store=s, positions=[], size_grid=grid,
            score_edge=0.0, action='BUY', structure='TREND_UP')
        assert len(weak) == 5
        assert sum(weak) == Decimal('0.19')
        # Weak stays near-equal (round-robin bumps), not steeply front-loaded.
        assert max(weak) - min(weak) <= Decimal('0.01')


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


def test_clear_trend_breaks_hold_dominance():
    """GOLD rebound case: HOLD 56% > BUY 25% but TREND_UP → anti-HOLD flip."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        scores = {'BUY': 0.247, 'SELL': 0.191, 'HOLD': 0.562}
        nudged, meta = db.soft_nudge_conviction(
            scores, phase=db.PHASE_HUNT, store=s,
            structure='TREND_UP', live_trend='MIXED',
            returns_bps={'1': 12.0, '5': 8.0})
        assert meta['nudged'] is True
        assert meta['reason'] == 'clear_trend_anti_hold'
        assert meta['target'] == 'BUY'
        assert nudged['BUY'] > nudged['HOLD']
        assert AutoEvolution._safe_scores(nudged)
        # Argmax side after nudge — no more HOLD-dominates forever.
        assert nudged['BUY'] >= nudged['SELL']
        assert nudged['BUY'] > nudged['HOLD']


def test_clear_trend_does_not_fight_strong_opposite():
    scores = {'BUY': 0.10, 'SELL': 0.55, 'HOLD': 0.35}
    nudged, meta = db.soft_nudge_conviction(
        scores, phase=db.PHASE_HUNT, store=None,
        structure='TREND_UP')
    # Strong SELL vote — do not force BUY from TREND_UP alone.
    assert meta.get('reason') != 'clear_trend_anti_hold'
    assert meta.get('reason') in (
        'clear_tape_dampen_wrong_side', 'clear_disagrees_skip_amplify')
    assert nudged['SELL'] >= nudged['BUY']
    # Soft damp may trim sticky SELL — never amplify it against UP tape.
    assert nudged['SELL'] <= scores['SELL'] + 1e-9


def test_clear_tape_does_not_amplify_sticky_sell_on_up_move():
    """Market turns UP while neural still leans SELL — do not lean_anti_hold SELL."""
    # HOLD already below BUY so clear_trend_anti_hold does not fire; without the
    # clear-disagrees guard, conviction_anti_hold would amplify sticky SELL.
    scores = {'BUY': 0.34, 'SELL': 0.43, 'HOLD': 0.23}
    nudged, meta = db.soft_nudge_conviction(
        scores, phase=db.PHASE_HUNT, store=None,
        live_trend='UP', returns_bps={'1': 8.0, '5': 5.0})
    assert meta.get('reason') not in ('lean_anti_hold', 'conviction_anti_hold')
    assert nudged['SELL'] <= scores['SELL'] + 1e-9
    assert meta.get('reason') in (
        'clear_disagrees_skip_amplify', 'clear_tape_dampen_wrong_side')


def test_day_phase_protect_tilts_hold():
    scores = {'BUY': 0.45, 'SELL': 0.35, 'HOLD': 0.20}
    nudged, meta = db.soft_nudge_day_phase(scores, phase=db.PHASE_PROTECT)
    assert meta['nudged'] is True
    assert nudged['HOLD'] > scores['HOLD']


def test_record_close_updates_phase():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        # No day profit cap → big win stays HUNT (loss stop only).
        db.record_close(s, pnl=120.0, equity=10_000.0, open_count=0)
        snap = db.snapshot(s, equity=10_000.0, positions=[])
        assert snap['daily']['realized_pnl'] == 120.0
        assert snap['phase'] == db.PHASE_HUNT
        assert snap['budgets']['profit_target'] is None
        assert snap['budgets']['loss_budget'] == pytest.approx(1000.0)


def test_foreign_epic_blocks_add():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        positions = [{'dealId': '1', 'epic': 'SILVER', 'size': '1'}]
        out = db.allows_new_open(
            store=s, equity=10_000.0, positions=positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=0.2)
        assert out['allowed'] is False
        assert out['reason'] == 'foreign_epic_open'


def test_tick_value_missing_calibrated_lot_not_stuck_on_mandate():
    """Without Capital pip value, lot must still scale with stop/risk — not glue to mandate."""
    from app.engine import SingleTradeEngine, AdaptiveBrain
    from app.autonomy import AutoEvolution
    from tests.test_learning_integrity import new_brain
    import tempfile
    from pathlib import Path
    from decimal import Decimal

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e.mandate = {'epic': 'GOLD', 'size': '0.1'}
        market = {
            'dealingRules': {
                'minDealSize': {'value': '0.01'},
                'minSizeIncrement': {'value': '0.01'},
                'maxDealSize': {'value': '50'},
            },
            'instrument': {},  # no valueOfOnePip
            'snapshot': {'decimalPlacesFactor': 2, 'marketStatus': 'TRADEABLE'},
        }
        # Calibrate like submit (fractional mode): mandate ≈ risk_per_trade at stop.
        params = dict(db.load_params(store), size_full_balance=0)
        b = db.budgets(equity=10_000.0, params=params, daily={})
        rpt = float(b['risk_per_trade'])
        stop_ticks = 50
        msize = 0.1
        tick_val = rpt / (msize * stop_ticks)
        tight = db.propose_size(
            mandate_size='0.1', equity=10_000.0, daily={'realized_pnl': 0},
            params=params, stop_ticks=stop_ticks, tick_value=tick_val,
            positions=[], score_edge=0.0, market=market)
        wide = db.propose_size(
            mandate_size='0.1', equity=10_000.0, daily={'realized_pnl': 0},
            params=params, stop_ticks=10, tick_value=tick_val,
            positions=[], score_edge=0.2, market=market)
        assert tight['allowed'] and wide['allowed']
        # Wider stop room / stronger edge → not forced identical to mandate forever.
        assert float(wide['size']) >= float(tight['size'])
        assert float(wide['size']) != float(msize) or float(tight['size']) != float(msize)


def test_manage_scores_allow_multi_add_when_open_frozen_hold():
    """Frozen OPEN HOLD must not block ADD when manage vote has clear edge."""
    from app.engine import SingleTradeEngine
    from app.autonomy import AutoEvolution
    from tests.test_learning_integrity import new_brain
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.positions = [{'dealId': 'live1', 'epic': 'GOLD', 'direction': 'BUY',
                        'size': '0.02'}]
        # Frozen OPEN looks like HOLD-dominated (Admin freeze).
        e.last_signal = {
            'scores': {'BUY': 0.44, 'HOLD': 0.53, 'SELL': 0.03},
            'scores_raw': {'BUY': 0.44, 'HOLD': 0.53, 'SELL': 0.03},
            'action': 'HOLD', 'scores_frozen': True,
        }
        e._locked_entry_signal = dict(e.last_signal)
        # Live manage vote is a clear BUY.
        e._manage_signal = {
            'scores': {'BUY': 0.62, 'HOLD': 0.10, 'SELL': 0.28},
            'action': 'BUY', 'bar_time': 't1',
        }
        scores, src = e._entry_vote_scores()
        assert src == 'manage'
        edge = e.policy.score_edge(scores)
        book = db.allows_new_open(
            store=store, equity=10_000.0, positions=e.positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=edge)
        assert book['allowed'] is True
        # Frozen OPEN edge alone would deny.
        frozen_edge = e.policy.score_edge(e.last_signal['scores'])
        denied = db.allows_new_open(
            store=store, equity=10_000.0, positions=e.positions,
            epic='GOLD', mandate_epic='GOLD', score_edge=frozen_edge)
        assert denied['allowed'] is False
        assert denied['reason'] == 'add_needs_stronger_edge'


def test_reconcile_daily_from_trades_fills_zero_day_book():
    """day_book_daily_v1 at 0 closes must catch up from trades closed today."""
    from datetime import datetime, timezone

    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        today = datetime.now(timezone.utc).date().isoformat()
        # Empty day book (the Admin alert state).
        daily = db.load_daily(s, equity=10_000.0)
        assert daily['closes'] == 0
        assert daily['realized_pnl'] == 0.0
        # Trades table already has closed cash for today.
        for i, pnl in enumerate((1.2, -0.4, 0.8)):
            s.db.execute(
                '''INSERT INTO trades(deal_id,epic,direction,size,entry,exit,pnl,
                   pnl_source,pnl_currency,opened,closed)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (f'd{i}', 'GOLD', 'BUY', '0.02', 2000, 2001, pnl, 'broker', 'GBP',
                 f'{today}T08:00:00+00:00', f'{today}T09:0{i}:00+00:00'))
        s.db.commit()
        out = db.reconcile_daily_from_trades(s, equity=10_000.0)
        assert out['closes'] == 3
        assert out['wins'] == 2
        assert out['losses'] == 1
        assert abs(out['realized_pnl'] - 1.6) < 1e-9
        snap = db.snapshot(s, equity=10_000.0, positions=[])
        assert snap['daily']['closes'] == 3
        # Idempotent record_close for an already-reconciled deal.
        again = db.record_close(s, pnl=1.2, equity=10_000.0, deal_id='d0')
        assert again['closes'] == 3
        assert abs(again['realized_pnl'] - 1.6) < 1e-9

def test_split_planned_sizes_into_five_when_advantageous():
    """One planned lot splits into up to 5 Capital-grid slices when flat + BUY/SELL."""
    from decimal import Decimal
    from app.self_params import DEFAULTS, save
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        grid = {'ok': True, 'min': Decimal('0.01'), 'step': Decimal('0.01'),
                'max': Decimal('50')}
        # Flat + BUY → 5 tickets preferring N×minDealSize + remainder bumps
        slices = db.split_planned_sizes(
            Decimal('0.10'), store=s, positions=[], size_grid=grid,
            score_edge=0.02, action='BUY', structure='TREND_UP')
        assert len(slices) == 5
        assert sum(slices) == Decimal('0.10')
        assert all(s >= Decimal('0.01') for s in slices)
        # Prefer min-lot base: every ticket starts at min before remainder.
        assert min(slices) == Decimal('0.01') or min(slices) == Decimal('0.02')
        # Already open → single ADD slice
        one = db.split_planned_sizes(
            Decimal('0.10'), store=s,
            positions=[{'dealId': '1', 'epic': 'GOLD', 'direction': 'BUY'}],
            size_grid=grid, score_edge=0.20, action='BUY', structure='TREND_UP')
        assert len(one) == 1
        assert one[0] == Decimal('0.10')
        # Weak edge / CHOP still multi when LIVE action is BUY (follow signal)
        weak = db.split_planned_sizes(
            Decimal('0.10'), store=s, positions=[], size_grid=grid,
            score_edge=0.01, action='BUY', structure='CHOP', live_trend='MIXED')
        assert len(weak) == 5
        # Tiny lot that cannot cover 2× broker min → stays one ticket
        tiny = db.split_planned_sizes(
            Decimal('0.01'), store=s, positions=[], size_grid=grid,
            score_edge=0.20, action='BUY', structure='TREND_UP')
        assert len(tiny) == 1
        # HOLD action never multi-slices
        hold = db.split_planned_sizes(
            Decimal('0.10'), store=s, positions=[], size_grid=grid,
            action='HOLD')
        assert len(hold) == 1
        # Evolved max_positions=1 must NOT freeze flat pack to one 0.03 ticket.
        low = dict(DEFAULTS)
        low['day_max_positions'] = 1
        low['day_open_slices'] = 1
        save(s, low, reason='test_low')
        forced = db.split_planned_sizes(
            Decimal('0.03'), store=s, positions=[], size_grid=grid, action='BUY')
        assert len(forced) == 3
        assert sum(forced) == Decimal('0.03')


def test_default_max_positions_is_five():
    params = db.load_params(None)
    assert params['max_positions'] == 5
    assert params['open_slices'] == 5
