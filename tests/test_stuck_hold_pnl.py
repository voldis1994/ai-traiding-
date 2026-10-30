"""Stuck HOLD after labels + missing manual-close £ (Admin freeze)."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine, Store
from tests.test_broker_pnl_resolve import HistoryBroker
from tests.test_learning_integrity import new_brain


def test_online_utility_fail_falls_back_to_readiness():
    """After ≥64 labels, weak walk-forward utility must not permanent-HOLD when prep ready."""
    with tempfile.TemporaryDirectory() as d:
        p=AutoEvolution(Store(Path(d)/'db.sqlite3'),Path(d)/'pol.py')
        scores={'BUY':0.54,'SELL':0.39,'HOLD':0.07}
        # Labeled history without contiguous prices → directional_evidence fails.
        history=[{'label':0,'scores':scores} for _ in range(70)]
        ready={'ready':True,'eval_samples':40,'minute_direction_accuracy':0.4,
               'holdout_net_bps_mean':0.1}
        out=p.evaluate_neural_entry(scores,history=history,readiness=ready)
        assert out['action']=='BUY'
        assert out['proposal']=='BUY'
        assert out['economic_utility']['allowed'] is True
        assert out['economic_utility']['source']=='prep_eval_holdout_fallback'
        assert out['economic_utility'].get('online_utility_failed') is True


def test_utility_still_blocks_without_readiness():
    with tempfile.TemporaryDirectory() as d:
        p=AutoEvolution(Store(Path(d)/'db.sqlite3'),Path(d)/'pol.py')
        scores={'BUY':0.54,'SELL':0.39,'HOLD':0.07}
        history=[{'label':0,'scores':scores} for _ in range(70)]
        out=p.evaluate_neural_entry(scores,history=history,readiness=None)
        assert out['action']=='HOLD'
        assert 'utility' in out['reason'].lower() or 'economic' in out['reason'].lower()


def test_snapshot_shows_hold_reason_not_m1_wait_mask():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        from app import hard_risk
        hard_risk.save_operator(store,{
            'max_risk_per_trade':10,'daily_loss_limit':50,'equity_drawdown_limit':100},
            reason='test')
        e=SingleTradeEngine(store,brain)
        e.armed=True;e.execution_enabled=True
        e.mandate={'epic':'GOLD','size':0.02}
        e.last_closed_step=10
        e.market_context={'ready':True,'reason':''}
        e.latest_fusion_ready=True
        e.last_decision_bar='2026-10-29T18:00:00+00:00'
        e.last_entry_processed_bar=e.last_decision_bar
        e.last_policy_decision='HOLD'
        e.last_entry_reason='Argmax BUY but no cost-adjusted economic utility: test'
        e.last_signal={'action':'BUY','scores':{'BUY':0.54,'SELL':0.39,'HOLD':0.07}}
        # Fake readiness ready so snapshot reaches the M1/HOLD branch.
        brain.trading_readiness=lambda: {'ready':True,'reason':'ok'}
        snap=e.snapshot()
        assert 'economic utility' in (snap.get('entry_block_reason') or '').lower()
        assert 'Waiting for next completed M1 candle' not in (snap.get('entry_block_reason') or '')


def test_history_matches_short_tx_reference_substring():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            e=SingleTradeEngine(store,brain)
            broker=HistoryBroker()
            # Capital often uses short reference that is a suffix/prefix of dealId.
            broker.tx=[{'reference':'abcd1234','pnl':0.14,'currency':'GBP'}]
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'deal-abcd1234-xyz','epic':'GOLD','size':'.02',
                                'direction':'SELL','level':2650})
            pnl,src,ccy=await e._resolve_broker_pnl('deal-abcd1234-xyz',{})
            assert src=='broker' and pnl==pytest.approx(0.14) and ccy=='GBP'
    asyncio.run(scenario())


def test_pending_retry_uses_queued_history_window():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store,brain=new_brain(d)
            brain.tick(100,.05,.01,'GOLD')
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=HistoryBroker()
            e.attach(broker)
            e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
            store.upsert_trade({'dealId':'late1','epic':'GOLD','size':'.02','direction':'BUY','level':100})
            store.set('entry_brain:late1',{
                'epic':'GOLD','direction':'BUY','entry':100,'tick_size':.01,
                'transition':brain._json_transition(brain.core.last_transition)})
            store.set('pending_broker_pnl_deals',
                      [{'deal_id':'late1','exit_level':99.5,'queued_at':'2026-10-29T10:00:00+00:00'}])
            broker.tx=[{'reference':'late1','pnl':-0.26,'currency':'GBP'}]
            await e._retry_pending_broker_pnl()
            row=store.trades(1)[0]
            assert row['pnl']==pytest.approx(-0.26)
            assert row['pnl_source']=='broker'
            assert not (store.get('pending_broker_pnl_deals') or [])
    asyncio.run(scenario())
