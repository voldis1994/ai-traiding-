import tempfile
from pathlib import Path
from app.engine import Store, AdaptiveBrain, SingleTradeEngine
from app.autonomy import AutoEvolution

def test_adaptive_threshold_and_policy_integration():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        policy=AutoEvolution(store,Path(d)/'policy.py')
        baseline=policy.adaptive_threshold(.9,50,0)
        assert baseline == .06
        assert policy.adaptive_threshold(.9,500,0) < baseline
        assert policy.adaptive_threshold(.2,500,0) > baseline
        scores={'BUY':.43,'SELL':.35,'HOLD':.22}
        assert policy.decision(scores)=='BUY'
        assert policy.decision(scores,threshold=.04)=='BUY'
        assert policy.decision(scores,threshold=.10)=='HOLD'
        engine=SingleTradeEngine(store,AdaptiveBrain(store,data_dir=d))
        engine.last_signal={'scores':scores}
        assert 'Policy HOLD' in engine._signal_reason('HOLD')
