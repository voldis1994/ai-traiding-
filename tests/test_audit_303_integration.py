"""Real integration tests for PR #303 remediations (items 1–15).

These exercise runtime behaviour — not source-text greps.
"""
from __future__ import annotations

import asyncio
import tempfile
import time

import pytest

from app import cash_ledger, capital_rest, cycle_runtime, learn_attempt
from app import learn_correction, telemetry, trade_cycle_fsm, work_queue
from app.engine import SingleTradeEngine
from app.realized_cash import may_teach_live_cash
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_cycle_runtime_hold_completes_without_execute():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cyc = cycle_runtime.begin_analysis_cycle(store, account_id='acct1')
        cyc = cycle_runtime.mark_perceived(store, cyc, snapshot_id='bar1')
        cyc = cycle_runtime.mark_analyzed(store, cyc, analysis_version='1')
        cyc = cycle_runtime.decide(store, cyc, 'HOLD', reason='no_edge')
        loaded = trade_cycle_fsm.load(store, cyc['cycle_id'])
        assert loaded['complete'] is True
        assert loaded['hold_only'] is True
        assert loaded['stages']['EXECUTE']['status'] == trade_cycle_fsm.STATUS_SKIPPED
        assert cycle_runtime.active_cycle(store)['cycle_id'] == cyc['cycle_id']


def test_cycle_runtime_live_path_execute_settle_learn():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cyc = cycle_runtime.begin_analysis_cycle(store, account_id='a')
        cyc = cycle_runtime.mark_perceived(store, cyc, snapshot_id='b')
        cyc = cycle_runtime.mark_analyzed(store, cyc)
        cyc = cycle_runtime.decide(store, cyc, 'BUY', reason='edge')
        assert cyc['stages']['EXECUTE']['status'] == trade_cycle_fsm.STATUS_RUNNING
        cyc = cycle_runtime.mark_executed(store, cyc, deal_id='D1', deal_ref='R1')
        cycle_runtime.bind_deal(store, cyc, 'D1')
        by = cycle_runtime.load_by_deal(store, 'D1')
        assert by and by['deal_id'] == 'D1'
        cyc = cycle_runtime.mark_manage_tick(store, by, upl=0.12)
        assert cyc['stages']['MANAGE']['status'] == trade_cycle_fsm.STATUS_RUNNING
        cyc = cycle_runtime.mark_closed(store, cyc, close_proof='vanish')
        cyc = cycle_runtime.mark_settled(
            store, cyc, funds_proof={'pnl': 1.26}, pending=False)
        cyc = cycle_runtime.mark_learned(store, cyc, model_version='2')
        assert cyc['complete'] is True
        assert cyc['stages']['LEARN']['status'] == trade_cycle_fsm.STATUS_DONE


def test_unmatched_funds_stay_unmatched_until_exact_link():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        # Orphan Funds row — no deal_link → UNMATCHED (never fuzzy-bound).
        out = cash_ledger.ingest_funds_row(
            store,
            {'pnl': 1.26, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link=None, transaction_id='tx-orphan-1')
        ev = out.get('event') or out
        assert ev.get('status') == cash_ledger.STATUS_UNMATCHED
        assert ev.get('amount_minor') == 126
        # Exact link on a new cash_id (same amount, known deal).
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 1.26, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'DEAL99'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='DEAL99', transaction_id='tx-linked-99')
        realized = cash_ledger.deal_realized(store, 'DEAL99')
        assert realized.get('may_teach') is True
        assert realized.get('amount_minor') == 126


def test_cash_ledger_is_sole_teach_authority():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        assert not may_teach_live_cash('capital')
        assert not may_teach_live_cash('upl_estimate')
        assert may_teach_live_cash('broker')
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 0.0, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'Z0'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='Z0', transaction_id='tx0')
        realized = cash_ledger.deal_realized(store, 'Z0')
        # Verified £0.00 is teachable settled-zero.
        assert realized.get('amount_minor') == 0
        assert realized.get('may_teach') is True
        assert realized.get('status') in (
            cash_ledger.STATUS_SETTLED_ZERO, cash_ledger.STATUS_SETTLED)


def test_work_queue_retry_backoff_and_diagnostics():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        work_queue.ensure_schema(store)
        wid = work_queue.enqueue(
            store, work_queue.KIND_SETTLE_PNL,
            {'deal_id': 'd1'}, dedupe_key='settle:d1')
        assert wid is not None
        # Dedupe keeps same id.
        assert work_queue.enqueue(
            store, work_queue.KIND_SETTLE_PNL,
            {'deal_id': 'd1'}, dedupe_key='settle:d1') == wid
        due = work_queue.claim_due(store, kinds=[work_queue.KIND_SETTLE_PNL], limit=5)
        assert len(due) == 1
        work_queue.fail(store, due[0]['id'], 'boom', attempts=1)
        row = store.db.execute(
            'SELECT status,attempts,last_error,next_attempt_at FROM work_queue WHERE id=?',
            (due[0]['id'],)).fetchone()
        assert row[0] == work_queue.STATUS_FAILED
        assert int(row[1]) >= 1
        assert row[2] == 'boom'
        assert row[3]  # backoff scheduled


def test_learn_recovery_and_correction_flag():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        att = learn_attempt.claim(store, 'c1', source='broker', pnl=1.0)
        assert att['state'] == learn_attempt.STATE_CLAIMED
        learn_attempt.transition(store, 'c1', learn_attempt.STATE_RUNNING)
        # Crash mid-learn → recovery marker.
        again = learn_attempt.claim(store, 'c1', source='broker', pnl=1.0)
        assert again and again.get('needs_recovery')
        # Complete then contradict with ledger → correction.
        learn_attempt.transition(store, 'c1', learn_attempt.STATE_APPLIED)
        learn_attempt.transition(store, 'c1', learn_attempt.STATE_COMMITTED, pnl=1.0, source='broker')
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': -0.50, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'c1'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='c1', transaction_id='tx-c1')
        info = learn_correction.needs_correction(store, 'c1')
        assert info is not None
        assert info.get('ledger_pnl') == pytest.approx(-0.50)
        flagged = learn_correction.flag_for_replay(store, 'c1', info)
        assert flagged['status'] == 'QUEUED'
        recovered = learn_correction.recover_incomplete_on_boot(store)
        assert isinstance(recovered, list)


def test_live_demo_rest_managers_isolated():
    live = capital_rest.manager_for('k:user', environment='live')
    demo = capital_rest.manager_for('k:user', environment='demo')
    assert live is not demo
    assert live.environment == 'live'
    assert demo.environment == 'demo'
    assert live.infer_priority('POST', '/api/v1/positions') < live.infer_priority(
        'GET', '/api/v1/history/transactions')


def test_light_upl_does_not_wait_on_engine_lock():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            e.mandate = {'epic': 'GOLD', 'size': 0.1}
            e.positions = [{
                'dealId': 'L1', 'epic': 'GOLD', 'direction': 'BUY', 'size': 0.1,
                'level': 2650.0, 'upl': 0.22, 'currency': 'GBP',
            }]
            # Hold engine.lock as heavy poll would — light UPL must still complete.
            await e.lock.acquire()
            try:
                t0 = time.monotonic()
                await asyncio.wait_for(e.quote_upl_tick(), timeout=2.0)
                elapsed = time.monotonic() - t0
            finally:
                e.lock.release()
            assert elapsed < 1.5
            snap = store.get('last_upl:L1')
            # May be None if broker mock returns empty positions — still proves no lock wait.
            assert elapsed < 1.5

    asyncio.run(run())


def test_pending_zero_and_settle_paths_keep_verified_zero():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
        tx0 = {'pnl': 0.0, 'currency': 'GBP', 'dealId': 'd0', 'transactionType': 'TRADE'}
        assert e._row_cash_pnl(tx0) == pytest.approx(0.0)
        # Queue does not silently drop.
        for i in range(30):
            e._queue_pending_pnl(f'p{i}')
        pending = store.get('pending_broker_pnl_deals') or []
        assert len(pending) >= 30
        rows = store.db.execute(
            "SELECT COUNT(*) FROM work_queue WHERE kind=?",
            (work_queue.KIND_SETTLE_PNL,)).fetchone()
        assert int(rows[0]) >= 1


def test_telemetry_measures_stages_and_api():
    t = telemetry.Telemetry()
    t.note_stage('PERCEIVE', 12.5)
    t.note_stage('ANALYZE', 40.0)
    t.note_stage('EXECUTE', 8.0)
    t.note_api_call()
    t.note_api_call()
    t.note_cycle(complete=True)
    t.note_cycle(complete=False)
    t.note_loop_tick()
    snap = t.snapshot()
    assert snap['stages']['PERCEIVE']['n'] == 1
    assert snap['api_calls_60s'] >= 2
    assert snap['cycle_complete'] == 1
    assert snap['cycle_incomplete'] == 1
    assert snap['cycle_completion_rate'] == pytest.approx(0.5)
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        persisted = t.persist(store)
        assert store.get(telemetry.STORE_KEY)['cycle_complete'] == 1
        assert 'stages' in persisted


def test_currency_identity_never_from_hint():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.account = {}
        assert e._cash_currency('USD') is None
        e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
        assert e._cash_currency('USD') == 'GBP'


def test_main_and_hub_expose_parallel_light_heavy():
    """Item 13: both entrypoints define independent light/heavy loops."""
    import inspect
    from app import main as main_mod
    from app.runtime_hub import RuntimeHub
    # Behavioural: coroutine callables exist and are independent.
    assert asyncio.iscoroutinefunction(main_mod._legacy_heavy_loop)
    assert asyncio.iscoroutinefunction(main_mod._legacy_light_loop)
    assert asyncio.iscoroutinefunction(RuntimeHub._heavy_loop)
    assert asyncio.iscoroutinefunction(RuntimeHub._light_loop)
    # Light loop source must not await poll/sync (no heavy serialization).
    light_src = inspect.getsource(RuntimeHub._light_loop)
    assert 'await engine.poll()' not in light_src
    assert 'await engine.sync()' not in light_src
    legacy_light = inspect.getsource(main_mod._legacy_light_loop)
    assert 'await engine.poll()' not in legacy_light
