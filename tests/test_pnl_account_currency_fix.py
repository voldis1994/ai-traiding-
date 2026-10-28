"""Broker cash must be labeled with account currency — never GOLD quote USD."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import Store, SingleTradeEngine
from app.runtime_hub import RuntimeHub
from tests.test_learning_integrity import new_brain
from tests.test_broker_pnl_resolve import HistoryBroker


def test_cash_currency_ignores_gold_usd_quote_tag():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        assert e._cash_currency('USD')=='GBP'
        assert e._cash_currency(None)=='GBP'
        e.account={}
        assert e._cash_currency('USD') is None  # never trust quote tag alone


def test_activity_usd_tag_stores_account_gbp():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            # Capital often tags GOLD activity with USD even though cash is GBP.
            broker.acts=[{'dealId':'d1','details':{'profit':0.29},'currency':'USD'}]
            e.attach(broker)
            # attach() clears account — restore after, as live poll does via accounts().
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            pnl,src,ccy=await e._resolve_broker_pnl('d1',{})
            assert pnl==pytest.approx(0.29)
            assert src=='broker' and ccy=='GBP'
    asyncio.run(scenario())


def test_upl_snapshot_uses_account_not_position_usd():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        e.market={'mid':2650.}
        e._snapshot_open_upl({'dealId':'x','upl':0.12,'currency':'USD','level':2650.})
        snap=store.get('last_upl:x')
        assert snap['currency']=='GBP'
        assert snap['upl']==pytest.approx(0.12)
        # Without account currency, do not stamp USD quote onto cash UPL.
        e.account={}
        e._snapshot_open_upl({'dealId':'y','upl':0.05,'currency':'USD','level':2650.})
        assert store.get('last_upl:y')['currency'] is None


def test_repair_relabels_usd_broker_rows_to_gbp():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'a','epic':'GOLD','size':'0.1','direction':'BUY','level':100})
        store.close_trade('a',101,0.26,currency='USD',source='broker')
        store.upsert_trade({'dealId':'b','epic':'GOLD','size':'0.1','direction':'BUY','level':100})
        store.close_trade('b',99,-0.30,currency='USD',source='broker')
        store.upsert_trade({'dealId':'c','epic':'GOLD','size':'0.1','direction':'BUY','level':100})
        store.close_trade('c',100.5,0.05,currency='GBP',source='broker')
        n=e.repair_broker_pnl_currencies()
        assert n==2
        rows={r['deal_id']:r for r in store.trades(10)}
        assert rows['a']['pnl_currency']=='GBP'
        assert rows['b']['pnl_currency']=='GBP'
        assert rows['c']['pnl_currency']=='GBP'


def test_admin_trade_stats_sum_gbp_not_split_usd():
    with tempfile.TemporaryDirectory() as d:
        # Minimal hub without full platform DB — call _trade_stats directly.
        store=Store(Path(d)/'db.sqlite3')
        for i,pnl in enumerate([0.26,-0.30,0.14,0.17,0.20]):
            store.upsert_trade({'dealId':f't{i}','epic':'GOLD','size':'0.1',
                                'direction':'BUY','level':100})
            # Mis-tagged as USD (the live bug).
            store.close_trade(f't{i}',101,pnl,currency='USD',source='broker')
        hub=RuntimeHub.__new__(RuntimeHub)
        stats=hub._trade_stats(store,'GBP')
        assert stats['pnl_currency']=='GBP'
        assert stats['realized_pnl']==pytest.approx(0.47)
        assert stats['wins']==4 and stats['losses']==1
        assert all(t['pnl_currency']=='GBP' for t in stats['recent_trades'])
        assert 'USD' not in (stats.get('pnl_by_currency') or {})


def test_shadow_rollback_without_prior_snapshot_not_vnone():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        # Fill enough broker £ that recent window looks worse — but only one
        # baseline on the stack after ensure, so previous_version is None.
        for i in range(30):
            e.profit_learning.history.append({
                'actual_pnl':0.5 if i<15 else -0.4,
                'direction':'BUY','entry':100.,'exit':100.5 if i<15 else 99.6})
        e.profit_learning._save()
        for i in range(24):
            e.shadow_paper.history.append({
                'paper_pnl':0.1,'price_pnl':0.1,'unit':'account_currency',
                'direction':'BUY','source':'shadow_paper'})
        e.shadow_paper._persist()
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        out=e.policy.shadow_self_dev_tick(e)
        reason=e.policy.last_reason or ''
        assert 'vNone' not in reason
        if out and out.get('rollback'):
            assert out.get('previous',{}).get('model_version') is not None
        else:
            assert 'snapshot' in reason.lower() or 'waiting' in reason.lower() or out is not None
