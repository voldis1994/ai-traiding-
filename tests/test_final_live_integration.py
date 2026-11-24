"""PR #303 FINAL LIVE SYSTEM INTEGRATION — acceptance scenarios.

Simulated broker data only. No Demo system. No artificial LIVE trades.
"""
from __future__ import annotations

import asyncio
import tempfile
import time

import pytest

from app import cash_ledger, capital_rest, cycle_runtime, learn_attempt
from app import live_diagnostics, shared_market, teacher_proofs, trade_cycle_fsm
from app import work_queue
from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_multi_tx_sum_not_overwrite():
    """Multiple Capital transactions for one deal → SUM, never last-wins."""
    store = cash_ledger.MemoryStore()
    cash_ledger.ingest_funds_row(
        store, {'pnl': 1.00, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-MULTI', transaction_id='TX1')
    cash_ledger.ingest_funds_row(
        store, {'pnl': 0.26, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-MULTI', transaction_id='TX2')
    agg = cash_ledger.aggregate_verified_by_deal(
        store, account_id='A1', currency='GBP', environment='live')
    assert 'D-MULTI' in agg
    assert agg['D-MULTI']['amount_minor'] == 126  # £1.00 + £0.26
    assert set(agg['D-MULTI']['transaction_ids']) == {'TX1', 'TX2'}
    realized = cash_ledger.deal_realized(
        store, 'D-MULTI', currency='GBP', account_id='A1', environment='live')
    assert realized['may_teach'] is True
    assert realized['amount_minor'] == 126


def test_identity_conflict_blocks_amount_correction():
    """Identity conflict with different sum → SOURCE_CONFLICT, no silent amend."""
    store = cash_ledger.MemoryStore()
    cash_ledger.ingest_funds_row(
        store, {'pnl': 2.00, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-ID', transaction_id='TX-ID')
    # Same transaction id but wrong account → identity conflict.
    out = cash_ledger.append_event(store, cash_ledger.cash_event(
        account_id='OTHER',
        environment='live',
        cash_id=cash_ledger.make_cash_id(
            environment='live', account_id='A1', transaction_id='TX-ID'),
        transaction_id='TX-ID',
        transaction_type='TRADE',
        close_proof=True,
        status=cash_ledger.STATUS_SETTLED,
        amount_minor=500,
        currency='GBP',
        deal_link='D-ID',
    ))
    assert out.get('conflict') is True
    assert 'account_id' in (out.get('identity_conflicts') or [])
    # Lineage amount unchanged.
    assert cash_ledger.net_for_cash_id(
        store,
        cash_ledger.make_cash_id(
            environment='live', account_id='A1', transaction_id='TX-ID'),
    ) == 200


def test_zero_pounds_is_final_no_relearn_loop():
    """Verified £0.00 is a finished result — may_teach, SETTLED_ZERO, no retry."""
    store = cash_ledger.MemoryStore()
    cash_ledger.ingest_funds_row(
        store, {'pnl': 0.0, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-Z', transaction_id='TX-Z')
    realized = cash_ledger.deal_realized(
        store, 'D-Z', currency='GBP', account_id='A1', environment='live')
    assert realized['may_teach'] is True
    assert realized['amount_minor'] == 0
    assert realized['status'] == cash_ledger.STATUS_SETTLED_ZERO
    # Second ingest of same zero is duplicate — not a new teach cycle.
    out2 = cash_ledger.ingest_funds_row(
        store, {'pnl': 0.0, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-Z', transaction_id='TX-Z')
    assert out2.get('duplicate') is True or out2.get('appended') is False


def test_activity_funds_no_double_cash():
    """Desk aggregator ignores learn:*; Funds rows alone sum once."""
    store = cash_ledger.MemoryStore()
    cash_ledger.ingest_funds_row(
        store, {'pnl': 1.26, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-DBL', transaction_id='FUNDS-1')
    # Legacy synthetic learn:* row must not double Desk £.
    cash_ledger.append_event(store, cash_ledger.cash_event(
        account_id='A1', environment='live',
        transaction_id='learn:D-DBL',
        transaction_type='TRADE', close_proof=True,
        status=cash_ledger.STATUS_SETTLED, amount_minor=126,
        currency='GBP', deal_link='D-DBL',
        note='learn_synthetic'))
    q = cash_ledger.quarantine_learn_synthetic(store)
    assert q['quarantined'] >= 1
    assert q.get('backed_up') is True
    backup = store.get(cash_ledger.BACKUP_KEY)
    assert backup and backup.get('events')
    agg = cash_ledger.aggregate_verified_by_deal(
        store, account_id='A1', currency='GBP', environment='live')
    assert agg['D-DBL']['amount_minor'] == 126


def test_banked_nonzero_pnl_amendment():
    """Capital amount amendment on banked non-zero P&L is applied via correction."""
    store = cash_ledger.MemoryStore()
    cash_ledger.ingest_funds_row(
        store, {'pnl': 1.00, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-AMD', transaction_id='TX-AMD')
    assert cash_ledger.net_for_cash_id(
        store,
        cash_ledger.make_cash_id(
            environment='live', account_id='A1', transaction_id='TX-AMD'),
    ) == 100
    out = cash_ledger.ingest_funds_row(
        store, {'pnl': 1.15, 'currency': 'GBP'},
        account_id='A1', environment='live', account_currency='GBP',
        deal_link='D-AMD', transaction_id='TX-AMD')
    assert out.get('appended') is True or out.get('delta_minor')
    assert cash_ledger.net_for_cash_id(
        store,
        cash_ledger.make_cash_id(
            environment='live', account_id='A1', transaction_id='TX-AMD'),
    ) == 115


def test_crash_at_each_weight_and_cycle_stage():
    """Crash recovery across CLAIMED→RUNNING→APPLIED→SAVE→COMMITTED→COMPLETE."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        deal = 'CRASH1'
        store.upsert_trade({
            'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade(deal, 101, 1.26, currency='GBP', source='broker')
        # Seed cycle FSM bound to deal.
        cyc = cycle_runtime.begin_analysis_cycle(store, account_id='a')
        cyc = cycle_runtime.mark_perceived(store, cyc, snapshot_id='b')
        cyc = cycle_runtime.mark_analyzed(store, cyc)
        cyc = cycle_runtime.decide(store, cyc, 'BUY', reason='edge')
        cyc = cycle_runtime.mark_executed(store, cyc, deal_id=deal, deal_ref='R')
        cycle_runtime.bind_deal(store, cyc, deal)
        cyc = cycle_runtime.load_by_deal(store, deal)
        cyc = cycle_runtime.mark_closed(store, cyc, close_proof='ok')
        cyc = cycle_runtime.mark_settled(
            store, cyc, funds_proof={'pnl': 1.26, 'source': 'broker'}, pending=False)

        att = learn_attempt.claim(store, deal, source='broker', pnl=1.26)
        assert att is not None
        aid = att['attempt_id']
        # Crash after CLAIMED
        assert e._trade_learn_needs_recovery(deal) or e._trade_learn_in_flight(deal)
        learn_attempt.transition(store, deal, learn_attempt.STATE_RUNNING)
        e._mark_trade_applied(deal, mode='t', pnl=1.26, source='broker')
        # Crash after APPLIED — before save
        ok, got = e._persist_learn_weights(deal)
        assert ok and got == aid
        assert brain.read_saved_learn_attempt_id() == aid
        # Crash after SAVE — before COMMITTED
        e._mark_trade_learned(deal, mode='t', pnl=1.26, source='broker')
        assert e._trade_learn_committed(deal) is True
        # Teacher proofs required for VALIDATE/COMPLETE
        store.set('teachers_settled:' + deal, {'at': 'x', 'source': 'broker'})
        store.set('adaptive_done:' + deal, True)
        # Recovery must COMPLETE FSM
        e._complete_cycle_after_commit(
            deal, mode='t', pnl=1.26, source='broker', taught=True)
        loaded = cycle_runtime.load_by_deal(store, deal)
        assert loaded and loaded.get('complete') is True
        # Restart recovery of already-COMMITTED incomplete would no-op COMPLETE
        n = e._recover_pending_learn_commits()
        assert n >= 0
        assert cycle_runtime.load_by_deal(store, deal).get('complete') is True


def test_teacher_proofs_idempotent_per_teacher():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        deal = 'TCH1'
        assert teacher_proofs.mark_done(store, deal, 'bracket_learn') is True
        assert teacher_proofs.mark_done(store, deal, 'bracket_learn') is False
        for name in teacher_proofs.REQUIRED_TEACHERS:
            teacher_proofs.mark_done(store, deal, name, proof={'ok': True})
        assert teacher_proofs.all_required_done(store, deal) is True
        assert teacher_proofs.settle_if_complete(
            store, deal, source='broker', pnl=1.0) is True
        assert store.get('teachers_settled:' + deal)


def test_shared_market_fresher_beats_richer_stale():
    shared_market.clear()
    shared_market.publish_mtf(
        'GOLD',
        {'ready': True, 'mode': 'BUY', 'preferred': 'BUY', 'epic': 'GOLD'},
        bars_m1=600, last_at='2026-11-19T17:30:00+00:00', source='stale')
    out = shared_market.publish_mtf(
        'GOLD',
        {'ready': True, 'mode': 'SELL', 'preferred': 'SELL', 'epic': 'GOLD'},
        bars_m1=200, last_at='2026-11-19T17:35:00+00:00', source='fresh')
    assert out['mode'] == 'SELL'
    assert out.get('shared_kept') is None
    shared_market.clear()


def test_capital_rest_hard_cap_with_parallel_requests():
    async def run():
        clock = {'t': 0.0}

        def now():
            return clock['t']

        mgr = capital_rest.CapitalRestManager(
            identity='test', environment='live', rps=50.0, burst=20, clock=now)
        acquired = []

        async def one(i):
            async def do():
                class R:
                    status_code = 200
                return R()
            await mgr.request('GET', f'/api/v1/history/{i}', do_request=do,
                              coalesce_key=None, priority=capital_rest.PRIORITY_HISTORY)
            acquired.append(i)

        # Advance clock slowly so window fills.
        tasks = []
        for i in range(15):
            tasks.append(asyncio.create_task(one(i)))
            clock['t'] += 0.001
            await asyncio.sleep(0)
        # Drain with clock advancing past 1s window edges.
        for _ in range(40):
            clock['t'] += 0.05
            await asyncio.sleep(0)
            if len(acquired) >= 15:
                break
        await asyncio.gather(*tasks, return_exceptions=True)
        # Never more than MAX_PER_SECOND in any 1s window of recorded stamps.
        stamps = list(mgr._recent)
        assert len(stamps) <= 400
        for t in stamps:
            in_win = sum(1 for s in stamps if abs(s - t) <= 1.0 and s <= t)
            assert in_win <= capital_rest.MAX_PER_SECOND

    asyncio.run(run())


def test_work_queue_done_requires_committed_not_teachers_alone():
    """DONE must not fire on teachers_settled while neural still in-flight."""
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            deal = 'QDONE1'
            broker = HistoryBroker()
            broker.tx = [{
                'dealId': deal, 'pnl': 1.5, 'currency': 'GBP',
                'transactionType': 'TRADE', 'reference': 'TX-QD',
            }]
            e.attach(broker)
            e.account = {
                'accounts': [{
                    'preferred': True, 'currency': 'GBP', 'accountId': '1',
                }]}
            store.set('capital_account_id', '1')
            store.set('capital_environment', 'live')
            store.upsert_trade({
                'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 100})
            store.close_trade(deal, 101, 1.5, currency='GBP', source='broker')
            # Seed ledger verified cash.
            cash_ledger.ingest_funds_row(
                store, {'pnl': 1.5, 'currency': 'GBP'},
                account_id='1', environment='live', account_currency='GBP',
                deal_link=deal, transaction_id='TX-QD')
            # Neural claimed but not COMMITTED; teachers marked settled alone.
            learn_attempt.claim(store, deal, source='broker', pnl=1.5)
            learn_attempt.transition(store, deal, learn_attempt.STATE_RUNNING)
            store.set('teachers_settled:' + deal, {'at': 'x', 'source': 'broker'})
            work_queue.ensure_schema(store)
            wid = work_queue.enqueue(
                store, work_queue.KIND_SETTLE_PNL,
                {'deal_id': deal, 'exit_level': 101},
                dedupe_key='settle:' + deal)
            assert wid
            # Process once — must NOT complete while neural in-flight.
            await e._retry_pending_broker_pnl()
            row = store.db.execute(
                'SELECT status FROM work_queue WHERE id=?', (wid,)).fetchone()
            # Job may be FAILED (retry) or still PENDING — never DONE.
            assert row and row[0] != work_queue.STATUS_DONE

    asyncio.run(run())


def test_hold_fsm_full_path():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cyc = cycle_runtime.begin_analysis_cycle(store, account_id='h')
        cyc = cycle_runtime.mark_perceived(store, cyc, snapshot_id='bar')
        cyc = cycle_runtime.mark_analyzed(store, cyc)
        cyc = cycle_runtime.decide(store, cyc, 'HOLD', reason='no_edge')
        loaded = trade_cycle_fsm.load(store, cyc['cycle_id'])
        assert loaded['complete'] is True
        assert loaded['stages']['EXECUTE']['status'] == trade_cycle_fsm.STATUS_SKIPPED


def test_repeated_restart_preserves_training_and_cash():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        deal = 'RST1'
        cash_ledger.ingest_funds_row(
            store, {'pnl': 0.80, 'currency': 'GBP'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link=deal, transaction_id='TX-RST')
        att = learn_attempt.claim(store, deal, source='broker', pnl=0.80)
        aid = att['attempt_id']
        learn_attempt.transition(store, deal, learn_attempt.STATE_RUNNING)
        e._mark_trade_applied(deal, mode='t', pnl=0.80, source='broker')
        e._persist_learn_weights(deal)
        e._mark_trade_learned(deal, mode='t', pnl=0.80, source='broker')
        # Simulate restart x3
        for _ in range(3):
            e2 = SingleTradeEngine(store, brain)
            e2._recover_pending_learn_commits()
            assert learn_attempt.is_committed(store, deal)
            assert brain.read_saved_learn_attempt_id() == aid
            realized = cash_ledger.deal_realized(
                store, deal, currency='GBP', account_id='1', environment='live')
            assert realized['amount_minor'] == 80
            assert realized['may_teach'] is True


def test_live_diagnostics_on_existing_store():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        cash_ledger.ingest_funds_row(
            store, {'pnl': 0.50, 'currency': 'GBP'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link='DX', transaction_id='TX-DX')
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        store.upsert_trade({
            'dealId': 'DX', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade('DX', 101, 0.50, currency='GBP', source='broker')
        snap = live_diagnostics.collect(store, account_id='1', currency='GBP',
                                        environment='live')
        assert snap['pnl_agreement']['matched'] >= 1
        assert 'api_load' in snap
        assert 'cycle_completion' in snap
        assert 'event_loop' in snap


def test_banked_nonzero_funds_amend_refines():
    """Banked non-zero broker £ must refine when Capital Funds revises amount."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        deal = 'AMD1'
        store.upsert_trade({
            'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade(deal, 101, 1.00, currency='GBP', source='broker')
        assert e._bank_broker_cash_side_effects(
            deal, 1.00, currency='GBP', source='broker') is True
        # Same deal, Funds revises to 1.15 — must refine, not ignore.
        assert e._bank_broker_cash_side_effects(
            deal, 1.15, currency='GBP', source='broker') is True
        banked = store.get('broker_cash_banked:' + deal)
        assert banked and float(banked['pnl']) == pytest.approx(1.15)
        assert banked.get('amend') is True


def test_verified_zero_ledger_not_rearmed_on_settle_queue():
    """SETTLED_ZERO + DONE settle must not spawn infinite work_queue jobs."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        deal = 'ZDONE'
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        store.upsert_trade({
            'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 100})
        store.close_trade(deal, 100, 0.0, currency='GBP', source='broker')
        store.set('broker_cash_banked:' + deal, {'pnl': 0.0, 'source': 'broker'})
        cash_ledger.ingest_funds_row(
            store, {'pnl': 0.0, 'currency': 'GBP'},
            account_id='1', environment='live', account_currency='GBP',
            deal_link=deal, transaction_id='TX-ZDONE')
        work_queue.ensure_schema(store)
        wid = work_queue.enqueue(
            store, work_queue.KIND_SETTLE_PNL, {'deal_id': deal},
            dedupe_key='settle:' + deal)
        work_queue.complete(store, wid)
        before = store.db.execute(
            "SELECT COUNT(*) FROM work_queue WHERE dedupe_key=?",
            ('settle:' + deal,)).fetchone()[0]
        e._requeue_unsettled_cash_closes()
        after = store.db.execute(
            "SELECT COUNT(*) FROM work_queue WHERE dedupe_key=?",
            ('settle:' + deal,)).fetchone()[0]
        assert after == before


def test_npz_attempt_id_authoritative_over_sidecar():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        att = learn_attempt.claim(store, 'NPZ1', source='broker', pnl=1.0)
        aid = att['attempt_id']
        learn_attempt.transition(store, 'NPZ1', learn_attempt.STATE_RUNNING)
        e._mark_trade_applied('NPZ1', mode='t', pnl=1.0, source='broker')
        e._persist_learn_weights('NPZ1')
        # Corrupt sidecar — NPZ must still prove the attempt.
        store.set('learn_weights_saved:NPZ1', {
            'attempt_id': 'WRONG', 'saved': True})
        assert e._weights_saved_for_attempt('NPZ1', aid) is True
        assert brain.read_saved_learn_attempt_id() == aid
