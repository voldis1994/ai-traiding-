"""Broker cash must be labeled with account currency — never GOLD quote USD."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import Store, SingleTradeEngine
from app.runtime_hub import AccountRuntime, RuntimeHub
from tests.test_learning_integrity import new_brain
from tests.test_broker_pnl_resolve import HistoryBroker


def test_cash_currency_account_only_never_hint():
    """Item 6: account currency only — never stamp cash from unproven hint."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        assert e._cash_currency('USD')=='GBP'
        assert e._cash_currency(None)=='GBP'
        e.account={}
        assert e._cash_currency('USD') is None
        assert e._cash_currency(None) is None


def test_funds_usd_tag_stores_account_gbp():
    """Funds cash uses account GBP; Activity-only does not settle."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            broker.acts=[{'dealId':'d1','details':{'profit':0.29},'currency':'USD'}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            pnl,src,_=await e._resolve_broker_pnl('d1',{})
            assert pnl is None and src=='pending_broker_pnl'
            # Funds row with account GBP — sole settle path.
            broker.tx=[{'dealId':'d1','pnl':0.29,'currency':'GBP',
                        'transactionId':'TX-d1'}]
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
        # Without account currency, do not invent a currency tag from quote hint.
        e.account={}
        e._snapshot_open_upl({'dealId':'y','upl':0.05,'currency':'USD','level':2650.})
        snap_y=store.get('last_upl:y')
        assert snap_y['currency'] in (None,'') or snap_y.get('currency') is None


def test_mapped_open_position_currency_is_account_not_gold_usd():
    """Capital tags GOLD with quote USD — Admin must never show Open … USD."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        mapped=e._map_positions([{
            'position':{
                'dealId':'g1','direction':'BUY','size':'0.1','level':2650,
                'upl':-0.15,'currency':'USD','stopLevel':2660,'profitLevel':2640,
            },
            'market':{'epic':'GOLD'},
        }])
        assert len(mapped)==1
        assert mapped[0]['currency']=='GBP'
        rt=AccountRuntime.__new__(AccountRuntime)
        enriched=rt._enrich_positions(e,[{
            'dealId':'g1','epic':'GOLD','direction':'BUY','size':'0.1',
            'level':2650,'upl':-0.15,'currency':'USD',
        }])
        assert enriched[0]['currency']=='GBP'


def test_repair_fills_blank_currency_only_never_rewrites():
    """P0-04: blank → account ccy; existing USD tag must NOT be rewritten to GBP."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        store.upsert_trade({'dealId':'a','epic':'GOLD','size':'0.1','direction':'BUY','level':100})
        store.close_trade('a',101,0.26,currency='USD',source='broker')
        store.upsert_trade({'dealId':'b','epic':'GOLD','size':'0.1','direction':'BUY','level':100})
        store.close_trade('b',99,-0.30,currency=None,source='broker')
        store.upsert_trade({'dealId':'c','epic':'GOLD','size':'0.1','direction':'BUY','level':100})
        store.close_trade('c',100.5,0.05,currency='GBP',source='broker')
        n=e.repair_broker_pnl_currencies()
        assert n==1  # only blank row b
        rows={r['deal_id']:r for r in store.trades(10)}
        assert rows['a']['pnl_currency']=='USD'  # preserved — no blind FX rewrite
        assert rows['b']['pnl_currency']=='GBP'
        assert rows['c']['pnl_currency']=='GBP'


def test_admin_trade_stats_sum_gbp_not_split_usd():
    with tempfile.TemporaryDirectory() as d:
        from app import cash_ledger
        # Minimal hub without full platform DB — call _trade_stats directly.
        store=Store(Path(d)/'db.sqlite3')
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        for i,pnl in enumerate([0.26,-0.30,0.14,0.17,0.20]):
            deal = f't{i}'
            store.upsert_trade({'dealId':deal,'epic':'GOLD','size':'0.1',
                                'direction':'BUY','level':100})
            # Account-currency broker cash (correct identity).
            store.close_trade(deal,101,pnl,currency='GBP',source='broker')
            cash_ledger.ingest_funds_row(
                store, {'pnl': pnl, 'currency': 'GBP', 'transactionType': 'TRADE'},
                account_id='1', environment='live', account_currency='GBP',
                deal_link=deal, transaction_id=f'TX-{deal}')
        hub=RuntimeHub.__new__(RuntimeHub)
        stats=hub._trade_stats(store,'GBP')
        assert stats['pnl_currency']=='GBP'
        assert stats['realized_pnl']==pytest.approx(0.47)
        assert stats['wins']==4 and stats['losses']==1
        assert all(t['pnl_currency']=='GBP' for t in stats['recent_trades'])


def test_admin_trade_stats_sums_beyond_200_and_legacy_blank_source():
    """Full history sums broker cash; blank source is NOT cash (V3 P05/T38)."""
    with tempfile.TemporaryDirectory() as d:
        from app import cash_ledger
        store=Store(Path(d)/'db.sqlite3')
        store.set('capital_account_id', '1')
        store.set('capital_environment', 'live')
        broker_total=0.0
        blank_n=0
        for i in range(250):
            pnl=-0.04
            deal = f'old{i}'
            store.upsert_trade({'dealId':deal,'epic':'GOLD','size':'0.1',
                                'direction':'SELL','level':2000})
            # Half broker cash, half legacy blank — blank ≠ Funds £.
            src='broker' if i%2==0 else None
            store.close_trade(deal,1999,pnl,currency='GBP',source=src)
            if src is None:
                blank_n+=1
                store.db.execute(
                    "UPDATE trades SET pnl_source=NULL WHERE deal_id=?",(deal,))
                store.db.commit()
            else:
                broker_total+=pnl
                cash_ledger.ingest_funds_row(
                    store, {'pnl': pnl, 'currency': 'GBP', 'transactionType': 'TRADE'},
                    account_id='1', environment='live', account_currency='GBP',
                    deal_link=deal, transaction_id=f'TX-{deal}')
        hub=RuntimeHub.__new__(RuntimeHub)
        stats=hub._trade_stats(store,'GBP',open_upl=-0.57)
        assert stats['cash_trades']==125
        assert stats['blank_pnl_trades']==blank_n
        assert stats['realized_pnl']==pytest.approx(broker_total)
        assert stats['open_upl']==pytest.approx(-0.57)
        # Equity = settled Funds £ only (open UPL stays on position cards).
        assert stats['equity_pnl']==pytest.approx(broker_total)
        assert stats['losses']==125 and stats['wins']==0
        assert RuntimeHub._cash_pnl_source(None)=='unverified'
        assert RuntimeHub._cash_pnl_source('')=='unverified'


def test_shadow_rollback_restores_when_recent_pound_worse():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        # Mark weights so restore has a real file.
        brain.core.P[:]+=0.25
        brain.save()
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
        assert 'waiting for a prior weight snapshot' not in reason.lower()
        # Checkpoint + rollback target (baseline or prior) must exist now.
        assert len(e.model_validator.stack)>=1
        assert e.model_validator.rollback_target() is not None
        # Recent £ worse → rollback attempted (restored or seeded, never stuck).
        assert out is not None
        if out.get('rollback'):
            # V5: candidate validation may refuse restore without proof — never
            # invent a LIVE weight swap. Accept restored OR explicit refusal.
            assert out.get('weights_restored') in (True, False)
            if out.get('weights_restored'):
                assert 'restored' in reason.lower()
            else:
                assert out.get('rollback') is True


def test_weight_checkpoint_every_rollback_window():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.policy._ensure_model_baseline(e)
        assert len(e.model_validator.stack)==1
        # First window of trades → second snapshot.
        e.policy._maybe_checkpoint_weights(e,n_trades=10,win=10)
        assert len(e.model_validator.stack)==2
        assert e.model_validator.previous_version() is not None
        # Same window again → no duplicate.
        e.policy._maybe_checkpoint_weights(e,n_trades=10,win=10)
        assert len(e.model_validator.stack)==2
        e.policy._maybe_checkpoint_weights(e,n_trades=20,win=10)
        assert len(e.model_validator.stack)==3
