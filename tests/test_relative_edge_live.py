"""LIVE signal gate and broker routing, tested OFFLINE with deterministic evidence."""
import asyncio
import tempfile
from pathlib import Path
from decimal import Decimal
import numpy as np

from app.autonomy import AutoEvolution, TEMPLATES
from app.engine import Store, AdaptiveBrain, SingleTradeEngine
from tests.test_integration import BrokerSimulation
from tests.market_helpers import install_synthetic_context, priced_horizon_history


def history_for(action='SELL',correct=True):
    # Marked delayed outcomes from non-overlapping windows; no broker orders.
    yes={'BUY':.366,'SELL':.433,'HOLD':.201} if action=='SELL' else {'BUY':.433,'SELL':.366,'HOLD':.201}
    opposite={'BUY':.8,'SELL':.1,'HOLD':.1} if action=='SELL' else {'BUY':.1,'SELL':.8,'HOLD':.1}
    desired_label=1 if action=='SELL' else 0
    rows=[]
    for segment in range(40):
        scores=yes if segment%2==0 else opposite
        label=(desired_label if correct else 2) if segment%2==0 else (0 if action=='SELL' else 1)
        for _ in range(8):rows.append({'scores':scores,'label':label,'spread_bps':2})
    return priced_horizon_history(rows)


def test_medium_sell_is_not_blocked_by_unrelated_absolute_60_percent():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3');policy=AutoEvolution(store,Path(d)/'policy.py')
        scores={'BUY':.366,'SELL':.433,'HOLD':.201}
        assert policy.score_edge(scores) > .06
        assert policy.decision(scores,threshold=.06)=='SELL'
        assert policy.decision(scores,threshold=.08)=='HOLD'
        verdict=policy.evaluate_entry(scores,history_for(),accuracy=.60,samples=80,spread_bps=2)
        assert verdict['action']=='SELL'
        assert verdict['evidence']['allowed']
        assert verdict['evidence']['samples']>=8


def test_poor_historical_direction_wont_force_real_order():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3');policy=AutoEvolution(store,Path(d)/'policy.py')
        scores={'BUY':.366,'SELL':.433,'HOLD':.201}
        verdict=policy.evaluate_entry(scores,history_for(correct=False),accuracy=.60,samples=80,spread_bps=2)
        assert verdict['proposal']=='SELL' and verdict['action']=='HOLD'
        assert verdict['evidence']['precision']==0


def test_no_trade_if_scores_invalid_even_when_history_positive():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'db.sqlite3');p=AutoEvolution(s,Path(d)/'policy.py')
        for scores in ({'BUY':60,'SELL':30,'HOLD':10},
                       {'BUY':float('nan'),'SELL':.4,'HOLD':.6},
                       {'BUY':.5,'SELL':.5}):
            assert p.evaluate_entry(scores,history_for(),accuracy=.7,samples=90,spread_bps=2)['action']=='HOLD'


def test_old_policy_migrates_once_preserving_backup_and_old_generation():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'db.sqlite3');s.set('code_generation',5)
        path=Path(d)/'active_policy.py'
        old=TEMPLATES['confidence'].format(confidence=.64,reversal=.72,min_hold=3,margin=.03)
        path.write_text(old)
        p=AutoEvolution(s,path)
        assert p.migrated and p.generation==5 and p.kind=='edge'
        assert 'MIN_EDGE' in path.read_text()
        assert path.with_name('active_policy.before_edge_v2.py').read_text()==old
        assert not AutoEvolution(s,path).migrated


def test_custom_handwritten_policy_not_silently_overwritten():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'db.sqlite3');path=Path(d)/'active_policy.py'
        src="CONFIDENCE = .8\ndef decide(scores,current_direction=None,age=0):\n    return 'HOLD'\n"
        path.write_text(src)
        p=AutoEvolution(s,path)
        assert not p.migrated
        assert path.read_text()==src


def test_medium_buy_or_sell_reaches_broker_after_manual_close_with_validated_labels(monkeypatch):
    async def scenario(direction):
        install_synthetic_context(monkeypatch, direction=direction)
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(s,data_dir=d)
            e=SingleTradeEngine(s,brain);e.policy=AutoEvolution(s,Path(d)/'policy.py')
            broker=BrokerSimulation();e.attach(broker)
            await e.poll()  # manual adoption + existing protection
            broker.open=[]
            await e.sync()
            assert e.last_closed_step>=0
            # Populate instrument-specific replay independently of Capital.com.
            rows=history_for(direction)
            brain.history.extend(rows)
            brain.labeled=80;brain.correct=60
            # In this integration test the *model* emits moderate scores.
            sample={'BUY':.366,'SELL':.433,'HOLD':.201} if direction=='SELL' else {'BUY':.433,'SELL':.366,'HOLD':.201}
            original_tick=brain.tick
            def controlled_tick(mid,spread,tick_size,epic,dt=1.,**kwargs):
                result=original_tick(mid,spread,tick_size,epic,dt,**kwargs)
                result['scores']=sample.copy()
                result['action']=direction
                result['confidence']=max(sample.values())
                return result
            brain.tick=controlled_tick
            for _ in range(5):await e.poll()
            order=[r for r in broker.requests if r[0]=='OPEN']
            assert len(order)==1, (direction,e.snapshot()['entry_block_reason'])
            assert order[0][2]==direction and order[0][3]==Decimal('.25')
            assert e.positions and e.positions[0]['dealId']=='b2'
            assert not s.unresolved()
    for action in ('BUY','SELL'):asyncio.run(scenario(action))
