"""Anti-repeat shadow promote: same evidence must not promote again."""
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.core import NeuralCore
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.learning_generation import counters, current
from app.self_params import DEFAULTS, save


def _seed_histories(engine, *, n=24, model_version=1):
    for i in range(n):
        engine.profit_learning.history.append({
            'actual_pnl':0.2 if i%3 else -0.1,
            'direction':'BUY','entry':100.,'exit':100.2 if i%3 else 99.9,
            'at':f'2024-01-01T00:{i:02d}:00+00:00',
            'bar_time':f'2024-01-01T00:{i:02d}:00+00:00',
            'model_version':model_version,
            'unit':'account_currency'})
    engine.profit_learning._save()
    for i in range(n):
        engine.shadow_paper.history.append({
            'paper_pnl':0.35 if i%2 else -0.05,
            'price_pnl':0.35 if i%2 else -0.05,
            'unit':'account_currency','direction':'SELL',
            'source':'shadow_paper',
            'exit_reason':'tp' if i%2 else 'sl',
            'at':f'2024-01-01T00:{i:02d}:00+00:00',
            'bar_time':f'2024-01-01T00:{i:02d}:00+00:00',
            'model_version':model_version})
    engine.shadow_paper._persist()


def test_second_promote_blocked_without_new_evidence():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'shadow_min_broker_trades':8,'model_min_oos':8,
                    'val_oos_frac':0.5},reason='test')
        brain=AdaptiveBrain(store,data_dir=d)
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        shadow=e._ensure_shadow_brain()
        if shadow.d!=brain.core.d:
            pytest.skip('shadow dim mismatch')
        if shadow.n!=brain.core.n:
            e.shadow_brain=brain.core.clone_architecture(
                n=brain.core.n,seed=99,copy_weights=False)
            shadow=e.shadow_brain
            shadow.model_version=1
        _seed_histories(e,n=24,model_version=1)
        shadow.P[:]+=0.01
        out1=e.policy.shadow_self_dev_tick(e)
        assert out1 is not None
        last=store.get(AutoEvolution.LAST_PROMOTE_KEY)
        if out1.get('weights_promoted'):
            assert last and last.get('weight_checksum')
            gen_after=current(store)
            out2=e.policy.shadow_self_dev_tick(e)
            assert out2 is not None
            assert out2.get('weights_promoted') is not True
            assert 'same model/checksum' in (out2.get('reason') or e.policy.last_reason)
            assert current(store)==gen_after
            assert counters(store)['verified_candidate_promotes']>=1
        else:
            checksum=e.policy.weight_checksum(shadow)
            e.policy._record_promote_evidence(
                e,e.profit_learning.history,e.shadow_paper.paper_rows(),checksum)
            assert e.policy._promote_blocked_same_evidence(
                e,e.profit_learning.history,e.shadow_paper.paper_rows(),checksum)
            e.shadow_paper.history.append({
                'paper_pnl':0.5,'unit':'account_currency','direction':'BUY',
                'source':'shadow_paper','exit_reason':'tp',
                'at':'2024-01-01T01:00:00+00:00',
                'bar_time':'2024-01-01T01:00:00+00:00','model_version':1})
            e.shadow_paper._persist()
            assert not e.policy._promote_blocked_same_evidence(
                e,e.profit_learning.history,e.shadow_paper.paper_rows(),checksum)


def test_weight_checksum_stable_and_sensitive():
    a=NeuralCore(n=8,d=4,seed=1)
    b=NeuralCore(n=8,d=4,seed=1)
    c=NeuralCore(n=8,d=4,seed=2)
    assert AutoEvolution.weight_checksum(a)==AutoEvolution.weight_checksum(b)
    assert AutoEvolution.weight_checksum(a)!=AutoEvolution.weight_checksum(c)
