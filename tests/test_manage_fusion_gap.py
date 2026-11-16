"""Manage reverse must work when fusion EXTERNAL is gapped; Admin not stuck at 0%."""
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.market_context import FEATURE_DIM
from app.market_fusion import EXTERNAL_NAMES
from app.runtime_hub import AccountRuntime


def test_manage_perceive_ohlc_pads_when_on_fusion_schema():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain._activate('GOLD')
        brain._switch_to_market_features()
        brain._extend_to_external_features()
        assert brain.feature_schema == brain.fusion_schema
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e._last_good_peers = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        e._last_good_news = [0.0, 0.0]
        feats = [0.02] * FEATURE_DIM
        out = e._manage_perceive_ohlc(
            mid=100., spread=0.2, tick=0.01, epic='GOLD',
            bar_t='2026-11-16T10:00:00+00:00', features=feats)
        assert out is not None
        assert isinstance(out.get('scores'), dict)
        assert any(out['scores'].values())


def test_existing_open_reason_uses_reverse_watch_not_waiting():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.positions = [{'dealId': 'd1', 'direction': 'BUY', 'upl': -0.45}]
        e.last_signal = None
        e._manage_signal = {
            'scores': {'BUY': 0.3, 'SELL': 0.5, 'HOLD': 0.2},
            'action': 'SELL',
        }
        reason = e._existing_open_block_reason()
        assert 'waiting for first M1 neural vote' not in reason
        assert 'reverse watch' in reason.lower()
        assert 'SELL' in reason


def test_seed_lock_from_manage_clears_zero_admin():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.positions = [{'dealId': 'd1', 'direction': 'BUY', 'upl': -0.1}]
        e.last_signal = None
        e._locked_entry_signal = None
        e._manage_signal = {
            'scores': {'BUY': 0.2, 'SELL': 0.55, 'HOLD': 0.25},
            'action': 'SELL', 'bar_time': 't',
            'entry_train': False, 'in_position': True,
        }
        # Mimic poll seeding.
        import copy
        seeded = copy.deepcopy(e._manage_signal)
        seeded['scores_frozen'] = True
        seeded['seeded_from_manage'] = True
        e._locked_entry_signal = seeded
        e.last_signal = copy.deepcopy(seeded)
        snap = e.snapshot()
        assert snap['signal']['scores']['SELL'] == 0.55
        assert 'waiting for first M1 neural vote' not in (snap.get('entry_block_reason') or '')


def test_runtime_hub_surfaces_manage_scores_when_signal_empty():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.policy = AutoEvolution(store, Path(d) / 'policy.py')
        e.positions = [{'dealId': 'x', 'direction': 'BUY', 'upl': -0.4}]
        e.last_signal = None
        e._locked_entry_signal = None
        e._manage_signal = {
            'scores': {'BUY': 0.2, 'SELL': 0.6, 'HOLD': 0.2},
            'action': 'SELL', 'bar_time': 't',
        }
        rt = AccountRuntime(
            client_id=1, account_id=1, data_dir=Path(d),
            store=store, brain=brain, engine=e, stream=None)
        real_snap = e.snapshot

        def fake_snap():
            s = real_snap()
            s['positions'] = e.positions
            s['signal'] = {}
            s['manage_signal'] = e._manage_signal
            s['entry_block_reason'] = e._existing_open_block_reason()
            return s

        e.snapshot = fake_snap
        view = rt.client_view()
        assert view['scores']['SELL'] == 0.6
        assert 'waiting for first M1 neural vote' not in (view.get('entry_block_reason') or '')
        assert view.get('neural_close_executable') is True
        assert 'Reverse watch' in (view['evolution'].get('last_reason') or '')


def test_fusion_dim_matches_external_names():
    assert len(EXTERNAL_NAMES) >= 8
