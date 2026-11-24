"""Last registered UPL before close = Capital £; refresh UPL on quote ticks."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_last_upl_as_realized_returns_snap():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
        store.set('last_upl:d1', {'upl': -1.25, 'currency': 'GBP'})
        got = e._last_upl_as_realized('d1')
        assert got is not None
        assert got[0] == pytest.approx(-1.25)
        assert got[1] == 'GBP'


def test_resolve_last_upl_is_capital_and_teachable():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            e.attach(HistoryBroker())
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.set('last_upl:d2', {'upl': 0.45, 'currency': 'GBP'})
            pnl, src, ccy = await e._resolve_broker_pnl('d2', {})
            assert pnl == pytest.approx(0.45)
            assert src == 'capital'
            assert ccy == 'GBP'
            from app.realized_cash import may_teach_live_cash
            assert may_teach_live_cash(src) is True
    asyncio.run(scenario())


def test_vanish_teaches_from_last_upl_as_capital():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(100, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = HistoryBroker()
            broker.open = [{'position': {
                'dealId': 'g01', 'size': '.02', 'direction': 'SELL',
                'level': 2651, 'upl': -0.32, 'currency': 'USD'},
                'market': {'epic': 'GOLD'}}]
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            await e.sync()
            e.market = {'epic': 'GOLD', 'mid': 2650.2, 'tick': .1, 'spread': .3}
            store.set('entry_brain:g01', {
                'epic': 'GOLD', 'direction': 'SELL', 'entry': 2651,
                'tick_size': .1,
                'transition': brain._json_transition(brain.core.last_transition),
                'scores': {'BUY': 0.2, 'HOLD': 0.3, 'SELL': 0.5}})
            store.upsert_trade({
                'dealId': 'g01', 'epic': 'GOLD', 'size': '.02',
                'direction': 'SELL', 'level': 2651})
            labeled_before = brain.trade_labeled
            broker.open = []
            await e.sync()
            row = store.trades(1)[0]
            assert row['pnl'] == pytest.approx(-0.32)
            assert row['pnl_source'] == 'capital'
            assert e._trade_already_learned('g01')
            assert brain.trade_labeled >= labeled_before
            # Funds later may refine without double-teaching.
            broker.tx = [{'reference': 'g01', 'pnl': '-0.35', 'currency': 'GBP',
                          'transactionType': 'TRADE'}]
            labeled_mid = brain.trade_labeled
            await e._retry_pending_broker_pnl()
            row = store.trades(1)[0]
            assert row['pnl'] == pytest.approx(-0.35)
            assert row['pnl_source'] == 'broker'
            assert brain.trade_labeled == labeled_mid
            banked = store.get('broker_cash_banked:g01')
            assert banked.get('source') == 'broker'
            assert banked.get('refined_from') in ('capital', 'upl_estimate')
    asyncio.run(scenario())


def test_quote_upl_tick_snapshots_fresh_upl():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            broker = HistoryBroker()
            broker.open = [{'position': {
                'dealId': 't1', 'size': '.05', 'direction': 'BUY',
                'level': 100, 'upl': 0.10, 'currency': 'GBP'},
                'market': {'epic': 'GOLD'}}]
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            e.mandate = {'epic': 'GOLD', 'size': '0.05'}
            await e.sync()
            assert store.get('last_upl:t1')['upl'] == pytest.approx(0.10)
            # Fresh tick with updated UPL.
            broker.open = [{'position': {
                'dealId': 't1', 'size': '.05', 'direction': 'BUY',
                'level': 100, 'upl': -0.07, 'currency': 'GBP'},
                'market': {'epic': 'GOLD'}}]
            e._upl_tick_mono = 0  # force REST refresh
            e.quote_stream = type('QS', (), {
                'current': staticmethod(lambda epic: {
                    'bid': 99.9, 'ask': 100.1})})()
            await e.quote_upl_tick()
            snap = store.get('last_upl:t1')
            assert snap['upl'] == pytest.approx(-0.07)
            assert snap.get('exit_hint') == pytest.approx(99.9)
    asyncio.run(scenario())


def test_quote_upl_tick_vanished_settles_capital():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(100, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = HistoryBroker()
            broker.open = [{'position': {
                'dealId': 'v1', 'size': '.02', 'direction': 'BUY',
                'level': 100, 'upl': 0.18, 'currency': 'GBP'},
                'market': {'epic': 'GOLD'}}]
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            e.mandate = {'epic': 'GOLD', 'size': '0.02'}
            await e.sync()
            store.set('entry_brain:v1', {
                'epic': 'GOLD', 'direction': 'BUY', 'entry': 100,
                'tick_size': .01,
                'transition': brain._json_transition(brain.core.last_transition),
                'scores': {'BUY': 0.5, 'HOLD': 0.3, 'SELL': 0.2}})
            broker.open = []
            e._upl_tick_mono = 0
            e.quote_stream = type('QS', (), {
                'current': staticmethod(lambda epic: {
                    'bid': 100.2, 'ask': 100.4})})()
            await e.quote_upl_tick()
            row = store.trades(1)[0]
            assert row['pnl'] == pytest.approx(0.18)
            assert row['pnl_source'] == 'capital'
            assert e._trade_already_learned('v1')
            assert e.positions == []
    asyncio.run(scenario())
