"""Pending broker £ must not freeze Admin/learning forever."""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from app.runtime_hub import RuntimeHub
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_history_lookback_clamps_to_one_day():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        old=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        store.upsert_trade({'dealId':'old1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.db.execute('UPDATE trades SET opened=? WHERE deal_id=?',(old,'old1'))
        store.db.commit()
        look=e._history_lookback('old1')
        assert look['from_time']
        assert look['to_time']
        ft=datetime.fromisoformat(look['from_time']).replace(tzinfo=timezone.utc)
        assert (datetime.now(timezone.utc)-ft)<=timedelta(hours=24)


def test_parse_cash_num_strips_currency():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        assert e._parse_cash_num('£-0.14')==pytest.approx(-0.14)
        assert e._parse_cash_num('1,25')==pytest.approx(1.25)
        assert e._row_cash_pnl({'pnl':'£0.22'})==pytest.approx(0.22)
        assert e._row_cash_pnl({},{'profitAndLoss':-0.08})==pytest.approx(-0.08)


def test_vanish_waits_for_funds_then_teaches_once():
    """Vanish without Funds: pending; Funds £ teaches once."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'g1','size':'.02','direction':'SELL',
                'level':2651,'upl':-0.11,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            e.market={'epic':'GOLD','mid':2650.2,'tick':.1,'spread':.3}
            store.set('entry_brain:g1',{'epic':'GOLD','direction':'SELL','entry':2651,
                'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
            store.upsert_trade({'dealId':'g1','epic':'GOLD','size':'.02','direction':'SELL','level':2651})
            broker.open=[]
            await e.sync()
            row=store.trades(1)[0]
            assert row.get('pnl_source')=='pending_broker_pnl'
            assert not e._trade_already_learned('g1')
            pending=store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id'))=='g1' for x in pending)
            labeled=brain.trade_labeled
            broker.tx=[{'reference':'g1','pnl':-0.09,'currency':'GBP'}]
            await e._retry_pending_broker_pnl()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.09)
            assert row['pnl_source']=='broker'
            assert e._trade_already_learned('g1')
            assert brain.trade_labeled>=labeled
            assert not (store.get('pending_broker_pnl_deals') or [])
    asyncio.run(scenario())


def test_activity_scan_without_deal_id_filter_finds_profit():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            # dealId filter path empty; unfiltered window has the row.
            class Selective(HistoryBroker):
                async def activity(self,*,last_period=600,from_time=None,to_time=None,
                                   detailed=True,deal_id=None):
                    if deal_id:
                        return {'activities':[]}
                    return {'activities':[
                        {'dealId':'deal-xyz','details':{'profit':'£-0.17'},'currency':'GBP'}]}
            broker=Selective()
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'deal-xyz','epic':'GOLD','size':'.02',
                                'direction':'BUY','level':100})
            pnl,src,ccy=await e._resolve_broker_pnl('deal-xyz',{})
            assert src=='broker' and pnl==pytest.approx(-0.17) and ccy=='GBP'
    asyncio.run(scenario())


def test_admin_recent_hides_pending_until_capital_funds():
    """Pending close stays off Desk until Capital Funds £ settles — no UPL invent."""
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        store.upsert_trade({'dealId':'p1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
        store.close_trade('p1',99.5,source='pending_broker_pnl',overwrite=True)
        store.set('last_upl:p1',{'upl':-0.13,'currency':'GBP'})
        hub=RuntimeHub.__new__(RuntimeHub)
        stats=RuntimeHub._trade_stats(hub,store,'GBP')
        # Desk CLIENT CLOSES / Kopējais = Funds cash only.
        assert stats['recent_trades']==[]
        assert stats['working_pnl'] is None
        assert stats['working_pnl_source'] in ('none','empty')
        assert stats['cash_trades']==0
        assert stats['pending_trades']>=1
