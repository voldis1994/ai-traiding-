"""Forward-live diagnostics; neural teach waits for Capital Funds £."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app import forward_live
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_forward_live_flags_hairline_and_scratch():
    with tempfile.TemporaryDirectory() as d:
        store,_=new_brain(d)
        forward_live.record_close(
            store,deal_id='h1',epic='GOLD',direction='BUY',pnl=0.01,
            pnl_source='broker',entry=3679,exit_level=3679.2,
            spread=0.3,stop_distance=2.0,profit_distance=3.0,atr=4.65,
            hold_bars=1,scratch_band=0.05)
        snap=forward_live.snapshot(store)
        assert snap['ok'] is True
        assert snap['n']==1
        assert snap['hairline_sl_rate']==pytest.approx(1.0)
        assert snap['scratch_rate']==pytest.approx(1.0)
        assert snap['false_signal_rate']==pytest.approx(1.0)
        assert snap['tips']


def test_close_waits_for_funds_before_neural_teach():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            broker.open=[{'position':{'dealId':'learn1','size':'.02','direction':'SELL',
                'level':2651,'upl':-0.11,'currency':'USD'},'market':{'epic':'GOLD'}}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            await e.sync()
            e.market={'epic':'GOLD','mid':2650.2,'tick':.1,'spread':.3}
            store.set('entry_brain:learn1',{
                'epic':'GOLD','direction':'SELL','entry':2651,
                'tick_size':.1,
                'transition':brain._json_transition(brain.core.last_transition),
                'scores':{'BUY':0.2,'HOLD':0.3,'SELL':0.5}})
            store.upsert_trade({'dealId':'learn1','epic':'GOLD','size':'.02',
                                'direction':'SELL','level':2651})
            labeled_before=brain.trade_labeled
            broker.open=[]
            await e.sync()
            row=store.trades(1)[0]
            # Last UPL before close = Capital £ — neural teaches now.
            assert row['pnl']==pytest.approx(-0.11)
            assert row['pnl_source']=='capital'
            assert e._trade_already_learned('learn1')
            assert brain.trade_labeled>=labeled_before
            pending=store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id') if isinstance(x,dict) else x)=='learn1'
                       for x in pending)
            labeled_mid=brain.trade_labeled
            broker.tx=[{'reference':'learn1','pnl':'-0.11','currency':'GBP',
                        'transactionType':'TRADE'}]
            await e._retry_pending_broker_pnl()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.11)
            assert row['pnl_source']=='broker'
            assert brain.trade_labeled==labeled_mid
    asyncio.run(scenario())
