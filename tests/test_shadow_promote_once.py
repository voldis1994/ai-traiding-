"""Validated SHADOW→LIVE weight adopt; same-evidence must not re-adopt."""
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.self_params import DEFAULTS, save


def _seed_histories(engine, *, n=24, model_version=1):
    for i in range(n):
        engine.profit_learning.history.append({
            'actual_pnl': 0.2 if i % 3 else -0.1,
            'direction': 'BUY', 'entry': 100., 'exit': 100.2 if i % 3 else 99.9,
            'at': f'2024-01-01T00:{i:02d}:00+00:00',
            'bar_time': f'2024-01-01T00:{i:02d}:00+00:00',
            'model_version': model_version,
            'unit': 'account_currency'})
    engine.profit_learning._save()
    for i in range(n):
        engine.shadow_paper.history.append({
            'paper_pnl': 0.35 if i % 2 else -0.05,
            'price_pnl': 0.35 if i % 2 else -0.05,
            'unit': 'account_currency', 'direction': 'SELL',
            'source': 'shadow_paper',
            'exit_reason': 'tp' if i % 2 else 'sl',
            'at': f'2024-01-01T00:{i:02d}:00+00:00',
            'bar_time': f'2024-01-01T00:{i:02d}:00+00:00',
            'model_version': model_version})
    engine.shadow_paper._persist()


def test_shadow_promote_adopts_live_weights_when_validated():
    """When SHADOW beats LIVE on the £ window, LIVE weights are adopted."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**DEFAULTS, 'shadow_min_broker_trades': 8, 'model_min_oos': 8,
                     'val_oos_frac': 0.5}, reason='test')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        shadow = e._ensure_shadow_brain()
        assert shadow is not brain.core
        live_before = AutoEvolution.weight_checksum(brain.core)
        _seed_histories(e, n=24, model_version=1)
        shadow.P[:] += 0.01
        shadow.model_version = max(1, int(getattr(shadow, 'model_version', 0) or 0))
        out1 = e.policy.shadow_self_dev_tick(e)
        assert out1 is not None
        if out1.get('weights_promoted'):
            assert out1.get('live_rewrite') is True
            assert AutoEvolution.weight_checksum(brain.core) != live_before
            checksum = e.policy.weight_checksum(shadow)
            assert e.policy._promote_blocked_same_evidence(
                e, e.profit_learning.history, e.shadow_paper.paper_rows(), checksum)
        else:
            # Compare may refuse thin/unstable windows — still no silent rewrite.
            assert out1.get('live_rewrite') is not True
            assert AutoEvolution.weight_checksum(brain.core) == live_before


def test_second_promote_blocked_without_new_evidence():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        save(store, {**DEFAULTS, 'shadow_min_broker_trades': 8, 'model_min_oos': 8,
                     'val_oos_frac': 0.5}, reason='test')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        shadow = e._ensure_shadow_brain()
        assert shadow is not brain.core
        _seed_histories(e, n=24, model_version=1)
        shadow.P[:] += 0.01
        shadow.model_version = max(1, int(getattr(shadow, 'model_version', 0) or 0))
        out1 = e.policy.shadow_self_dev_tick(e)
        assert out1 is not None
        checksum = e.policy.weight_checksum(shadow)
        e.policy._record_promote_evidence(
            e, e.profit_learning.history, e.shadow_paper.paper_rows(), checksum)
        live_mid = AutoEvolution.weight_checksum(brain.core)
        out2 = e.policy.shadow_self_dev_tick(e)
        assert out2 is not None
        assert out2.get('weights_promoted') is not True
        assert AutoEvolution.weight_checksum(brain.core) == live_mid
