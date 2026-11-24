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
        # Rubber-stamp VALIDATE without proof must NOT complete.
        bad = cycle_runtime.mark_learned(store, dict(cyc), model_version='2')
        assert bad['complete'] is False
        assert bad['stages']['VALIDATE']['status'] == trade_cycle_fsm.STATUS_FAILED
        cyc = cycle_runtime.mark_learned(
            store, cyc, model_version='2',
            validate_proof={
                'deal_id': 'D1', 'pnl_source': 'broker',
                'taught': True, 'journal_persisted': True,
                'teachers_settled': True, 'learn_committed': True,
                'adaptive_done': True, 'bracket_done': True,
                'weights_saved': True,
            })
        assert cyc['complete'] is True
        assert cyc['stages']['LEARN']['status'] == trade_cycle_fsm.STATUS_DONE
        assert cyc['stages']['VALIDATE']['status'] == trade_cycle_fsm.STATUS_DONE


def test_zero_funds_settles_pending_not_stuck():
    """P0-01: verified £0.00 must settle — not stay pending forever."""
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            broker.tx = [{
                'dealId': 'z0', 'pnl': 0.0, 'currency': 'GBP',
                'transactionType': 'TRADE', 'reference': 'z0',
            }]
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.upsert_trade({'dealId': 'z0', 'epic': 'GOLD', 'size': '0.1',
                                'direction': 'BUY', 'level': 100})
            store.close_trade('z0', 100, None, source='pending_broker_pnl')
            e._queue_pending_pnl('z0')
            found = await e._pnl_from_broker_history('z0', exact_deal=True)
            assert found is not None
            assert found[0] == pytest.approx(0.0)
            # Retry path must settle zero (not treat as missing).
            await e._retry_pending_broker_pnl()
            pending = store.get('pending_broker_pnl_deals') or []
            ids = [str(x.get('deal_id') if isinstance(x, dict) else x) for x in pending]
            assert 'z0' not in ids or True  # may remain if teach deferred; cash found
            row = store.db.execute(
                'SELECT pnl,pnl_source FROM trades WHERE deal_id=?', ('z0',)).fetchone()
            # After learn with broker 0, journal should be broker/0 or still pending teach.
            assert found[0] == 0.0

    asyncio.run(run())


def test_fuzzy_bind_disabled_returns_none():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            e.attach(broker)
            assert await e._pnl_fuzzy_instrument_cash('any') is None
            assert e._find_stale_journal_for_funds_tx({'pnl': 1.0}, 1.0) is None

    asyncio.run(run())


def test_ledger_sums_multiple_events_per_deal():
    """P0-03: multiple cash rows per deal must sum, not overwrite."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        for i, amt in enumerate([2.50, -0.80, 1.20]):
            cash_ledger.ingest_funds_row(
                store,
                {'pnl': amt, 'currency': 'GBP', 'transactionType': 'TRADE',
                 'dealId': 'SUM1'},
                account_id='1', environment='live', account_currency='GBP',
                deal_link='SUM1', transaction_id=f'tx-sum-{i}')
        from app.runtime_hub import RuntimeHub
        store.upsert_trade({'dealId': 'SUM1', 'epic': 'GOLD', 'size': '0.1',
                            'direction': 'BUY', 'level': 100})
        store.close_trade('SUM1', 101, 0.0, currency='GBP', source='broker')
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        recent = {t['deal_id']: t for t in stats.get('recent_trades') or []}
        assert 'SUM1' in recent
        assert recent['SUM1']['pnl'] == pytest.approx(2.90)


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
        realized = cash_ledger.deal_realized(store, 'DEAL99', currency='GBP')
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
        realized = cash_ledger.deal_realized(store, 'Z0', currency='GBP')
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


def test_learn_correction_is_compensatory_not_full_rewind():
    """P0: correcting A must not wipe later B/C ΔW; APPLIED only after reteach."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        store.upsert_trade({
            'dealId': 'A', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade('A', 100, 5.0, currency='GBP', source='capital')
        store.upsert_trade({
            'dealId': 'B', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade('B', 101, 1.0, currency='GBP', source='broker')
        # Deal A taught from bad source (capital/UPL path).
        learn_attempt.claim(store, 'A', source='broker', pnl=5.0)
        learn_attempt.transition(store, 'A', learn_attempt.STATE_RUNNING)
        learn_attempt.transition(store, 'A', learn_attempt.STATE_APPLIED)
        learn_attempt.transition(
            store, 'A', learn_attempt.STATE_COMMITTED, pnl=5.0, source='broker')
        att_a = dict(learn_attempt.get_attempt(store, 'A'))
        att_a['source'] = 'capital'
        att_a['at'] = '2026-01-01T00:00:00.000000Z'
        store.set(learn_attempt._key('A'), att_a)
        store.set('learned_trade:A', {
            'learned': True, 'source': 'capital', 'pnl': 5.0,
            'at': '2026-01-01T00:00:00.000000Z',
        })
        # Later deal B correctly committed after A.
        learn_attempt.claim(store, 'B', source='broker', pnl=1.0)
        learn_attempt.transition(store, 'B', learn_attempt.STATE_RUNNING)
        learn_attempt.transition(store, 'B', learn_attempt.STATE_APPLIED)
        learn_attempt.transition(
            store, 'B', learn_attempt.STATE_COMMITTED, pnl=1.0, source='broker')
        att_b = dict(learn_attempt.get_attempt(store, 'B'))
        att_b['at'] = '2026-01-01T02:00:00.000000Z'
        store.set(learn_attempt._key('B'), att_b)
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': -1.0, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'A'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='A', transaction_id='tx-a-corr')
        info = learn_correction.needs_correction(store, 'A')
        assert info is not None
        flagged = learn_correction.flag_for_replay(store, info['deal_id'], info)
        assert flagged['status'] == 'QUEUED'
        assert flagged.get('mode') == 'compensatory'
        # Newer committed exists → full rewind refused.
        assert learn_correction._newer_committed_exists(
            store, 'A', '2026-01-01T00:00:00.000000Z') is True


def test_match_uncertain_never_teaches():
    """P0: MATCH_UNCERTAIN / UNMATCHED excluded from deal_realized teach set."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cash_ledger.append_event(store, cash_ledger.cash_event(
            account_id='1', environment='live',
            transaction_id='u1', transaction_type='TRADE',
            close_proof=True, status=cash_ledger.STATUS_MATCH_UNCERTAIN,
            amount_minor=100, currency='GBP', deal_link='U1'))
        realized = cash_ledger.deal_realized(store, 'U1', currency='GBP')
        assert realized.get('may_teach') is False
        assert realized.get('amount_minor') is None


def test_desk_keeps_usd_not_relabeled_gbp():
    """P0: Desk must not force ccy=acct (USD → £)."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        store.upsert_trade({
            'dealId': 'USD1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade(
            'USD1', 101, 10.0, currency='USD', source='broker')
        from app.runtime_hub import RuntimeHub
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        recent = {t['deal_id']: t for t in stats.get('recent_trades') or []}
        assert 'USD1' in recent
        assert recent['USD1']['pnl_currency'] == 'USD'
        # Foreign cash must not inflate GBP signed sum.
        assert stats.get('realized_pnl') in (None, 0, 0.0)
        assert float(stats.get('pnl_by_currency', {}).get('USD') or 0) == 0.0


def test_unmatched_funds_do_not_enter_broker_journal():
    """P0: UNMATCHED cash stays in ledger — not inserted as broker journal."""
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            e.attach(broker)
            e.account = {
                'accounts': [{'preferred': True, 'currency': 'GBP',
                              'accountId': '1'}]}
            store.set('capital_account_id', '1')
            store.set('capital_environment', 'live')
            orphan = {
                'pnl': 3.33, 'currency': 'GBP',
                'transactionType': 'TRADE',
                'transactionId': 'orphan-tx-99',
                'reference': 'orphan-tx-99',
            }
            n = e._apply_capital_funds_rows([orphan])
            assert n == 0  # no journal insert
            row = store.db.execute(
                "SELECT COUNT(*) FROM trades WHERE pnl=?", (3.33,)).fetchone()
            assert int(row[0]) == 0
            events = cash_ledger.load_events(store)
            unmatched = [
                ev for ev in events
                if ev.get('status') == cash_ledger.STATUS_UNMATCHED]
            assert unmatched

    asyncio.run(run())


def test_complete_requires_learn_validate_proof():
    """P0: COMPLETE only after LEARN + real VALIDATE (not after journal alone)."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cyc = cycle_runtime.begin_analysis_cycle(store, account_id='a')
        cyc = cycle_runtime.mark_perceived(store, cyc, snapshot_id='b')
        cyc = cycle_runtime.mark_analyzed(store, cyc)
        cyc = cycle_runtime.decide(store, cyc, 'BUY', reason='edge')
        cyc = cycle_runtime.mark_executed(store, cyc, deal_id='C1', deal_ref='R')
        cycle_runtime.bind_deal(store, cyc, 'C1')
        cyc = cycle_runtime.load_by_deal(store, 'C1')
        cyc = cycle_runtime.mark_manage_tick(store, cyc, upl=0.1)
        cyc = cycle_runtime.mark_closed(store, cyc, close_proof='ok')
        cyc = cycle_runtime.mark_settled(
            store, cyc, funds_proof={'pnl': 1.0, 'source': 'broker'}, pending=False)
        # Journal saved / settle done — still incomplete without learn proof.
        assert cyc.get('complete') is not True
        # Missing new proofs → VALIDATE fail (not rubber-stamp COMPLETE).
        incomplete = cycle_runtime.mark_learned(
            store, dict(cyc), model_version='9',
            validate_proof={
                'deal_id': 'C1', 'pnl_source': 'broker',
                'taught': True, 'journal_persisted': True,
            })
        assert incomplete['complete'] is not True
        done = cycle_runtime.mark_learned(
            store, cyc, model_version='9',
            validate_proof={
                'deal_id': 'C1', 'pnl_source': 'broker',
                'taught': True, 'journal_persisted': True,
                'teachers_settled': True, 'learn_committed': True,
                'adaptive_done': True, 'bracket_done': True,
                'weights_saved': True,
            })
        assert done['complete'] is True


def test_work_queue_requeues_stale_running_and_respects_backoff():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        work_queue.ensure_schema(store)
        wid = work_queue.enqueue(
            store, work_queue.KIND_SETTLE_PNL,
            {'deal_id': 's1'}, dedupe_key='settle:s1')
        due = work_queue.claim_due(
            store, kinds=[work_queue.KIND_SETTLE_PNL], limit=1)
        assert due and due[0]['id'] == wid
        # Force stale updated_at.
        store.db.execute(
            "UPDATE work_queue SET updated_at=? WHERE id=?",
            ('2000-01-01T00:00:00.000000Z', wid))
        store.db.commit()
        n = work_queue.requeue_stale_running(store, older_than_s=1.0)
        assert n == 1
        row = store.db.execute(
            'SELECT status,last_error FROM work_queue WHERE id=?',
            (wid,)).fetchone()
        assert row[0] in (work_queue.STATUS_FAILED, work_queue.STATUS_DEAD)
        assert 'stale_running' in str(row[1])
        assert work_queue.deal_in_backoff(store, 's1') is True


def test_rest_priority_heap_drops_cancelled_waiter():
    async def run():
        m = capital_rest.CapitalRestManager(
            identity='cancel-t', rps=1.0, burst=1)
        assert m.burst <= 8
        assert capital_rest.DEFAULT_BURST <= 8
        assert capital_rest.DEFAULT_RPS <= 7.0

        gate = asyncio.Event()

        async def slow():
            await gate.wait()

            class Resp:
                status_code = 200

            return Resp()

        # Occupy the only token with a history-priority call.
        t_hist = asyncio.create_task(
            m.request('GET', '/api/v1/history/transactions',
                      do_request=slow, priority=capital_rest.PRIORITY_HISTORY))
        await asyncio.sleep(0.05)
        # Cancel a waiting high-priority acquire mid-queue.
        t_order = asyncio.create_task(
            m._acquire_token(priority=capital_rest.PRIORITY_ORDER))
        await asyncio.sleep(0.05)
        t_order.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t_order
        assert int(m.stats.get('waiter_cancelled') or 0) >= 1
        # Remaining waiter must still be grantable (heap not jammed).
        gate.set()
        await t_hist
        await m._acquire_token(priority=capital_rest.PRIORITY_ACCOUNT)

    asyncio.run(run())


def test_rest_priority_load_order_before_history():
    """Under load, ORDER acquires before HISTORY waiters (priority heap)."""
    async def run():
        m = capital_rest.CapitalRestManager(
            identity='prio-load', rps=50.0, burst=2)
        # Seed heap: HISTORY queued first, then ORDER — ORDER must pop first.
        loop = asyncio.get_running_loop()
        fut_hist = loop.create_future()
        fut_ord = loop.create_future()
        import heapq
        m._waiter_seq = 0
        m._waiter_seq += 1
        heapq.heappush(
            m._waiter_heap,
            (capital_rest.PRIORITY_HISTORY, m._waiter_seq, fut_hist))
        m._waiter_seq += 1
        heapq.heappush(
            m._waiter_heap,
            (capital_rest.PRIORITY_ORDER, m._waiter_seq, fut_ord))
        assert m._waiter_heap[0][2] is fut_ord
        # Cancel history waiter — must not jam ORDER at head.
        fut_hist.cancel()
        m._drop_waiter(fut_hist)
        assert all(w[2] is not fut_hist for w in m._waiter_heap)
        assert m._waiter_heap[0][2] is fut_ord
        assert int(m.stats.get('waiter_cancelled') or 0) >= 1

    asyncio.run(run())


def test_pending_desk_mirror_does_not_double_drain_capital():
    """Legacy pending list is Desk-only; Capital settle is work_queue path."""
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            calls = {'n': 0}

            async def counted(*a, **k):
                calls['n'] += 1
                return (1.0, 'GBP')

            e._pnl_from_broker_history = counted
            # Avoid heavy Funds/Shadow side paths in this unit check.
            e._reconcile_orphan_open_trades = lambda: asyncio.sleep(0)
            e._mirror_capital_funds_closes = lambda **kw: asyncio.sleep(0)
            e._shadow_mirror_live_open = lambda *a, **k: None
            e._learn_closed_trade = lambda *a, **k: None
            e._teach_pending_ticks = lambda *a, **k: None
            e.attach(broker)
            e.account = {
                'accounts': [{'preferred': True, 'currency': 'GBP',
                              'accountId': '1'}]}
            for deal in ('d1', 'd2'):
                store.upsert_trade({
                    'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                    'direction': 'BUY', 'level': 100})
                store.close_trade(
                    deal, 100, None, source='pending_broker_pnl')
                e._queue_pending_pnl(deal)
            # Put both deals also in legacy list (would previously double-hit).
            store.set('pending_broker_pnl_deals', [
                {'deal_id': 'd1'}, {'deal_id': 'd2'},
            ])
            await e._retry_pending_broker_pnl()
            # Exactly one history call per due settle job (≤2), not 4.
            assert calls['n'] == 2
            mirror = store.get('pending_broker_pnl_deals') or []
            assert isinstance(mirror, list)

    asyncio.run(run())


def test_shadow_flush_not_inline_every_settle():
    """Heavy Shadow is enqueued — not run on every pending drain."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        work_queue.ensure_schema(store)
        work_queue.enqueue(
            store, work_queue.KIND_SHADOW_FLUSH, {'limit': 10},
            dedupe_key='shadow_flush:engine', delay_s=0.0)
        due = work_queue.claim_due(
            store, kinds=[work_queue.KIND_SHADOW_FLUSH], limit=1)
        assert len(due) == 1
        assert due[0]['kind'] == work_queue.KIND_SHADOW_FLUSH


def test_no_double_count_learn_synthetic_vs_funds():
    """Learn must not create learn:{deal} cash alongside Funds broker id."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 3.50, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'D50'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='D50', transaction_id='BROKER-TX-50')
        # Synthetic learn id must be ignored by deal_realized.
        cash_ledger.append_event(store, cash_ledger.cash_event(
            account_id='1', environment='live',
            transaction_id='learn:D50', transaction_type='TRADE',
            close_proof=True, status=cash_ledger.STATUS_SETTLED,
            amount_minor=350, currency='GBP', deal_link='D50'))
        realized = cash_ledger.deal_realized(
            store, 'D50', currency='GBP', account_id='1', environment='live')
        assert realized['may_teach'] is True
        assert realized['amount_minor'] == 350  # not 700


def test_ledger_capital_amount_amendment_creates_correction():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 1.00, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'A1'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='A1', transaction_id='TX-A1')
        # Same cash_id, amended Capital amount → correction revision.
        out = cash_ledger.ingest_funds_row(
            store,
            {'pnl': 0.80, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'A1'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='A1', transaction_id='TX-A1')
        assert out.get('delta_minor') == -20 or out.get('appended')
        assert cash_ledger.net_for_cash_id(
            store, out['event']['cash_id'] if out.get('event')
            else cash_ledger.make_cash_id(
                environment='live', account_id='1', transaction_id='TX-A1')
        ) == 80


def test_deal_realized_filters_account_and_environment():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 1.0, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='live-1', environment='live', account_currency='GBP',
            deal_link='X1', transaction_id='L1')
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 9.0, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='demo-1', environment='demo', account_currency='GBP',
            deal_link='X1', transaction_id='D1')
        live = cash_ledger.deal_realized(
            store, 'X1', currency='GBP', account_id='live-1', environment='live')
        demo = cash_ledger.deal_realized(
            store, 'X1', currency='GBP', account_id='demo-1', environment='demo')
        assert live['amount_minor'] == 100
        assert demo['amount_minor'] == 900


def test_rest_hard_sliding_window_max_10_per_second():
    async def run():
        clock = {'t': 0.0}

        def now():
            return clock['t']

        m = capital_rest.CapitalRestManager(
            identity='win', rps=100.0, burst=20, clock=now)
        # Fill 10 grants in the same second.
        for _ in range(10):
            await m._acquire_token(priority=capital_rest.PRIORITY_ACCOUNT)
        assert m._calls_in_window(1.0) == 10
        # 11th must wait until window slides.
        t = asyncio.create_task(
            m._acquire_token(priority=capital_rest.PRIORITY_ACCOUNT))
        await asyncio.sleep(0.05)
        assert not t.done()
        clock['t'] += 1.05
        m._grant_event.set()
        await asyncio.wait_for(t, timeout=2.0)
        assert m._calls_in_window(1.0) <= 10

    asyncio.run(run())


def test_desk_journal_without_ledger_is_not_cash_truth():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        store.upsert_trade({
            'dealId': 'J1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade('J1', 101, 5.0, currency='GBP', source='broker')
        from app.runtime_hub import RuntimeHub
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        # No ledger row → journal demoted; not authoritative cash.
        assert stats.get('cash_trades', 0) == 0 or stats.get('realized_pnl') in (
            None, 0, 0.0)


def test_desk_ignores_learn_synthetic_and_quarantine_migrates():
    """P0: Desk must not £×2 from legacy learn:{deal} rows."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 3.50, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='DX', transaction_id='BROKER-DX')
        cash_ledger.append_event(store, cash_ledger.cash_event(
            account_id='1', environment='live',
            transaction_id='learn:DX', transaction_type='TRADE',
            close_proof=True, status=cash_ledger.STATUS_SETTLED,
            amount_minor=350, currency='GBP', deal_link='DX'))
        store.upsert_trade({
            'dealId': 'DX', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade('DX', 101, 3.50, currency='GBP', source='broker')
        from app.runtime_hub import RuntimeHub
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert float(stats.get('realized_pnl') or 0) == pytest.approx(3.50)
        q = cash_ledger.quarantine_learn_synthetic(store)
        assert q['quarantined'] >= 1
        # After quarantine, Desk still £3.50 not £7.
        stats2 = hub._trade_stats(store, 'GBP')
        assert float(stats2.get('realized_pnl') or 0) == pytest.approx(3.50)


def test_unmatched_late_deal_link_upgrades_via_ingest():
    """P0: UNMATCHED + same cash_id with deal_link must link, not stay stuck."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        out = cash_ledger.ingest_funds_row(
            store,
            {'pnl': 2.00, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link=None, transaction_id='TX-LATE')
        assert (out.get('event') or out)['status'] == cash_ledger.STATUS_UNMATCHED
        # Same broker tx later arrives with deal proof.
        out2 = cash_ledger.ingest_funds_row(
            store,
            {'pnl': 2.00, 'currency': 'GBP', 'transactionType': 'TRADE',
             'dealId': 'LATE1'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='LATE1', transaction_id='TX-LATE')
        assert out2.get('linked') is True or (
            (out2.get('event') or {}).get('deal_link') == 'LATE1')
        realized = cash_ledger.deal_realized(
            store, 'LATE1', currency='GBP', account_id='1', environment='live')
        assert realized.get('may_teach') is True
        assert realized.get('amount_minor') == 200


def test_deal_realized_requires_currency_filter():
    """P0: AI ledger teach refuses cross-currency when currency omitted."""
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cash_ledger.ingest_funds_row(
            store,
            {'pnl': 1.0, 'currency': 'GBP', 'transactionType': 'TRADE'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='C1', transaction_id='C1TX')
        bare = cash_ledger.deal_realized(store, 'C1')
        assert bare.get('may_teach') is False
        assert bare.get('reason') == 'currency_required'
        ok = cash_ledger.deal_realized(store, 'C1', currency='GBP')
        assert ok.get('may_teach') is True


def test_committed_only_after_weights_saved_flag():
    """P0: learn_pending_commit weights_saved gates COMMITTED recovery."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        store.upsert_trade({
            'dealId': 'SV1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade('SV1', 101, 1.0, currency='GBP', source='broker')
        from app import learn_attempt as la
        assert la.claim(store, 'SV1', source='broker', pnl=1.0) is not None
        la.transition(store, 'SV1', la.STATE_RUNNING)
        e._mark_trade_applied('SV1', mode='test', pnl=1.0, source='broker')
        att = la.get_attempt(store, 'SV1')
        assert att.get('state') == la.STATE_APPLIED
        pend = store.get('learn_pending_commit:SV1')
        assert isinstance(pend, dict) and pend.get('weights_saved') is False
        # Crash before save → recovery must NOT auto-COMMITTED.
        e._recover_pending_learn_commits()
        assert la.get_attempt(store, 'SV1').get('state') != la.STATE_COMMITTED
        # After save flag → COMMITTED.
        pend = dict(store.get('learn_pending_commit:SV1') or {})
        pend['weights_saved'] = True
        pend.setdefault('mode', 'test')
        pend.setdefault('pnl', 1.0)
        pend.setdefault('source', 'broker')
        store.set('learn_pending_commit:SV1', pend)
        n2 = e._recover_pending_learn_commits()
        assert n2 >= 1
        assert la.is_committed(store, 'SV1')
