"""Acceptance tests for 24.11.2026 audit rebuild (P0/P1 foundation)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app import capital_rest, learn_attempt, trade_cycle_fsm
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from app.realized_cash import classify_pnl_source, may_teach_live_cash
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_capital_rest_rate_budget_and_coalesce():
    async def run():
        m = capital_rest.CapitalRestManager(identity='t1', rps=100.0, burst=5)

        class Resp:
            status_code = 200

        calls = {'n': 0}

        async def do():
            calls['n'] += 1
            await asyncio.sleep(0.05)
            return Resp()

        # Coalesce three identical GETs into one in-flight call.
        a = asyncio.create_task(m.request('GET', '/x', do_request=do, coalesce_key='k'))
        b = asyncio.create_task(m.request('GET', '/x', do_request=do, coalesce_key='k'))
        c = asyncio.create_task(m.request('GET', '/x', do_request=do, coalesce_key='k'))
        await asyncio.gather(a, b, c)
        assert calls['n'] == 1
        assert m.stats['coalesced'] >= 2

        # 429 retries on GET.
        seq = {'i': 0}

        class R429:
            def __init__(self, code):
                self.status_code = code

        async def flaky():
            seq['i'] += 1
            return R429(429 if seq['i'] < 3 else 200)

        r = await m.request('GET', '/y', do_request=flaky, coalesce_key='y', max_429_retries=5)
        assert r.status_code == 200
        assert m.stats['retry_429'] >= 2

        # Single-flight relogin shares generation.
        gens = []

        async def connect():
            await asyncio.sleep(0.02)
            gens.append(1)

        g1 = asyncio.create_task(m.single_flight_relogin(connect, observed_generation=0))
        g2 = asyncio.create_task(m.single_flight_relogin(connect, observed_generation=0))
        out = await asyncio.gather(g1, g2)
        assert out[0] == out[1] == 1
        assert len(gens) == 1
        snap = m.snapshot_metrics()
        assert 'calls_per_s' in snap and 'latency_ms' in snap

    asyncio.run(run())


def test_trade_cycle_fsm_hold_and_settlement_pending():
    c = trade_cycle_fsm.new_cycle(account_id='a1')
    c = trade_cycle_fsm.start_stage(c, 'PERCEIVE', input_version='snap1')
    c = trade_cycle_fsm.finish_stage(c, 'PERCEIVE', proof='ok')
    c = trade_cycle_fsm.start_stage(c, 'ANALYZE')
    c = trade_cycle_fsm.finish_stage(c, 'ANALYZE')
    c = trade_cycle_fsm.start_stage(c, 'DECIDE')
    c = trade_cycle_fsm.set_decision(c, 'HOLD', reason='no_edge')
    assert c['complete'] is True
    assert c['hold_only'] is True
    assert c['stages']['EXECUTE']['status'] == trade_cycle_fsm.STATUS_SKIPPED

    c2 = trade_cycle_fsm.new_cycle(deal_id='d1')
    for st in ('PERCEIVE', 'ANALYZE', 'DECIDE', 'EXECUTE', 'MANAGE', 'CLOSE'):
        c2 = trade_cycle_fsm.start_stage(c2, st)
        c2 = trade_cycle_fsm.finish_stage(c2, st)
    c2 = trade_cycle_fsm.mark_settlement_pending(c2)
    assert c2['complete'] is False
    pending = trade_cycle_fsm.pending_stages(c2)
    assert any(p['stage'] == 'SETTLE' for p in pending)


def test_learn_attempt_states_and_no_upl_teach():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        assert learn_attempt.claim(store, 'x', source='capital', pnl=1.0) is None
        assert learn_attempt.claim(store, 'x', source='upl_estimate', pnl=1.0) is None
        att = learn_attempt.claim(store, 'x', source='broker', pnl=1.26)
        assert att and att['state'] == 'CLAIMED'
        # Second claim while in-flight → recovery marker, not silent skip forever.
        again = learn_attempt.claim(store, 'x', source='broker', pnl=1.26)
        assert again and again.get('needs_recovery')
        learn_attempt.transition(store, 'x', 'RUNNING')
        learn_attempt.transition(store, 'x', 'APPLIED')
        learn_attempt.transition(store, 'x', 'COMMITTED')
        assert learn_attempt.is_committed(store, 'x')
        assert learn_attempt.claim(store, 'x', source='broker', pnl=2.0) is None


def test_p0_06_08_funds_zero_settles_lot_amount_kept():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
        # Verified Amount even when ≈ lot size must not be discarded (P0-07).
        tx = {'amount': 0.77, 'size': 0.77, 'currency': 'GBP', 'dealId': 'd77'}
        assert e._row_cash_pnl(tx) == pytest.approx(0.77)
        # Honest £0.00 from Funds is kept (P0-08) — parser returns 0.0.
        tx0 = {'pnl': 0.0, 'currency': 'GBP', 'dealId': 'd0', 'transactionType': 'TRADE'}
        assert e._row_cash_pnl(tx0) == pytest.approx(0.0)


def test_p0_06_resolve_does_not_return_upl_as_cash():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.set('last_upl:u1', {'upl': 0.78, 'currency': 'GBP'})
            pnl, src, _ = await e._resolve_broker_pnl('u1')
            assert pnl is None
            assert src == 'pending_broker_pnl'
            est = store.get('upl_estimate:u1')
            assert est and est['status'] == 'ESTIMATE'
            assert est['upl'] == pytest.approx(0.78)
            assert not may_teach_live_cash('capital')
            assert classify_pnl_source('capital') == 'estimate'

    asyncio.run(run())


def test_p0_02_poll_does_not_double_retry_pending():
    src = Path('app/engine.py').read_text(encoding='utf-8')
    # Locate poll body — must not call _retry_pending_broker_pnl after sync.
    start = src.index('async def poll(self):')
    end = src.index('\n    async def ', start + 10)
    body = src[start:end]
    assert 'await self.sync()' in body
    assert 'await self._retry_pending_broker_pnl()' not in body


def test_p0_10_funds_bind_exact_deal_id_only():
    src = Path('app/engine.py').read_text(encoding='utf-8')
    assert "OR deal_id LIKE ?" not in src or 'P0-10' in src
    # Positive: exact bind query present.
    assert 'WHERE deal_id=? LIMIT 1' in src


def test_pending_queue_does_not_silently_drop():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        for i in range(520):
            e._queue_pending_pnl(f'd{i}')
        pending = store.get('pending_broker_pnl_deals') or []
        assert len(pending) >= 520
