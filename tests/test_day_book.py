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

    # BE-band after a scratch must stay HUNT (no auto DONE/PROTECT).
    daily = {'realized_pnl': 0.05, 'closes': 1}
    b = db.budgets(equity=10_000.0, params=params, daily=daily)
    assert abs(daily['realized_pnl']) <= float(b['be_band'] or 0)
    assert db.phase_for(daily=daily, budgets_map=b, open_count=0) == db.PHASE_HUNT
    assert db.phase_for(daily=daily, budgets_map=b, open_count=1) == db.PHASE_HUNT


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
    assert meta.get('target') != 'BUY' or meta.get('reason') != 'clear_trend_anti_hold'
    assert nudged['SELL'] >= nudged['BUY']


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
        # Calibrate like submit: mandate ≈ risk_per_trade at stop.
        params = db.load_params(store)
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
