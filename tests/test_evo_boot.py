"""Physical evo boot — seeds + shorter trial horizon so evolution starts."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import evo_boot
from app import strategy_gen as sg
from app.self_params import (
    DEFAULTS, PENDING_KEY, load, save, evolve_from_feedback,
    maybe_commit_long_horizon,
)


def test_evo_boot_seeds_strategy_cards_and_horizon():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        # Old frozen horizon that blocked commits.
        save(store, {**DEFAULTS, 'self_params_horizon_trades': 20}, reason='old')
        out = evo_boot.ensure_physical_evo(store, epic='GOLD')
        assert out['ok'] is True
        assert load(store)['self_params_horizon_trades'] == 8
        bag = sg.load(store)
        assert bag['active_ids']
        assert any(
            c.get('prefer') == 'SELL' and c.get('structure') == 'TREND_DOWN'
            for c in bag['cards'].values())
        # Idempotent
        out2 = evo_boot.ensure_physical_evo(store, epic='GOLD')
        assert out2.get('reason') == 'already_booted'


def test_short_prior_relative_improvement_commits():
    """Recovery trial (less red) must commit — not require trial_mean > 0."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        save(store, {**DEFAULTS, 'self_params_horizon_trades': 8}, reason='test')
        # Only 3 prior trades (short prior path).
        store.set('profit_learning_history_v1',
                  [{'actual_pnl': -0.5, 'mode': 'loss'}] * 3)
        evolve_from_feedback(store, feature_approved='gen_ret_8', commit=False)
        assert (store.get(PENDING_KEY) or {}).get('trial_active')
        # Trial less red than prior mean −0.5 → relative beat.
        pnls = [-0.5] * 3 + [-0.1] * 8
        out = maybe_commit_long_horizon(store, pnls)
        assert out['committed'] is True
        assert out.get('mode') == 'short_prior_mean'


def test_soft_nudge_works_after_boot_without_library():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        evo_boot.ensure_physical_evo(store, epic='GOLD')
        scores = {'BUY': 0.65, 'SELL': 0.25, 'HOLD': 0.10}
        nudged, meta = sg.soft_nudge_scores(
            scores, structure='TREND_DOWN', epic='GOLD', store=store)
        assert meta['nudged'] is True
        assert nudged['SELL'] > nudged['BUY']
