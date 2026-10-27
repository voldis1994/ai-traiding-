"""Out-of-sample margin calibration: never blindly unblock LIVE orders."""
import asyncio
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from tests.market_helpers import install_synthetic_context, priced_horizon_history
from tests.test_integration import BrokerSimulation


def scored_history(*, action='SELL', fail_holdout=False):
    """240 consecutive, ALREADY-LABELED closed bars; 8-bar horizons.

    Directional score lead is 3 pp, below the old 6 pp hard margin.
    """
    sell={'BUY':.32,'SELL':.35,'HOLD':.33}
    buy={'BUY':.35,'SELL':.32,'HOLD':.33}
    rows=[]
    for block in range(30):
        direction='SELL' if block%2==0 else 'BUY'
        scores=sell if direction=='SELL' else buy
        label=1 if direction=='SELL' else 0
        if fail_holdout and block>=21 and direction==action:
            label=2
        rows.extend({'scores':scores.copy(),'label':label,'spread_bps':1.8} for _ in range(8))
    return priced_horizon_history(rows)


def test_new_gate_allows_validated_three_point_sell_and_buy():
    with tempfile.TemporaryDirectory() as d:
        p=AutoEvolution(Store(Path(d)/'data.sqlite3'),Path(d)/'policy.py')
        for action,scores in [('SELL',{'BUY':.32,'SELL':.35,'HOLD':.33}),
                              ('BUY', {'BUY':.35,'SELL':.32,'HOLD':.33})]:
            assert p.decision(scores,threshold=.06)=='HOLD'
            verdict=p.evaluate_entry(scores,scored_history(),accuracy=.43,samples=240,spread_bps=1.8)
            assert verdict['action']==action,verdict
            assert .01<=verdict['threshold']<.03
            assert verdict['calibration']['method']=='holdout_calibrated'
            assert verdict['calibration']['train_samples']>=8
            assert verdict['calibration']['validation_samples']>=4
            assert verdict['evidence']['allowed']


def test_new_gate_rejects_unvalidated_lower_margin_from_future_holdout():
    with tempfile.TemporaryDirectory() as d:
        p=AutoEvolution(Store(Path(d)/'data.sqlite3'),Path(d)/'policy.py')
        verdict=p.evaluate_entry({'BUY':.32,'SELL':.35,'HOLD':.33},
            scored_history(fail_holdout=True),accuracy=.43,samples=240,spread_bps=1.8)
        assert verdict['action']=='HOLD',verdict
        assert verdict['calibration']['method']=='policy_default'
        assert 'failed chronological holdout' in verdict['calibration']['reason']


def test_near_tie_and_no_evidence_do_not_force_live_trade():
    with tempfile.TemporaryDirectory() as d:
        p=AutoEvolution(Store(Path(d)/'data.sqlite3'),Path(d)/'policy.py')
        # The screenshot showed a 0.4 pp lead: too ambiguous even for a
        # validated 1 pp adaptive floor.
        scores={'BUY':.345,'SELL':.349,'HOLD':.306}
        verdict=p.evaluate_entry(scores,scored_history(),accuracy=.43,samples=240,spread_bps=1.8)
        assert verdict['action']=='HOLD',verdict
        assert verdict['threshold']>=.01
        raw={'BUY':.32,'SELL':.35,'HOLD':.33}
        assert p.evaluate_entry(raw,[],accuracy=.43,samples=0,spread_bps=1.8)['action']=='HOLD'


def test_validated_reduced_margin_reaches_broker_only_after_manual_close(monkeypatch):
    install_synthetic_context(monkeypatch, direction='SELL')
    async def run():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'data.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            engine=SingleTradeEngine(store,brain)
            engine.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation()
            engine.attach(broker)
            await engine.poll()
            broker.open=[]
            await engine.sync()
            brain.history.extend(scored_history())
            brain.labeled=240;brain.correct=103
            original_tick=brain.tick
            def controlled_tick(*args,**kwargs):
                result=original_tick(*args,**kwargs)
                result['scores']={'BUY':.32,'SELL':.35,'HOLD':.33}
                result['action']='SELL'
                return result
            brain.tick=controlled_tick
            for _ in range(5):await engine.poll()
            opens=[r for r in broker.requests if r[0]=='OPEN']
            assert len(opens)==1,engine.snapshot()['entry_block_reason']
            assert opens[0][2]=='SELL'
            assert engine.positions and engine.positions[0]['dealId']=='b2'
            assert not store.unresolved()
            assert engine.snapshot()['entry_margin_calibration'] is None # with open position, no new entry
    asyncio.run(run())
