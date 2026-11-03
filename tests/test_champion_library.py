"""Multi-champion library: save many profitable versions; activate on context match."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.champion_library import (
    append_trade, consider_save, context_from, find_match, load, maybe_activate,
    news_bucket, should_save_champion, soft_key, snapshot, DEFAULT_MIN_TRADES,
)
from app.core import NeuralCore
from app.engine import Store, AdaptiveBrain


def test_context_fingerprint_and_soft_key():
    ctx = context_from(epic='GOLD', trend='UP', regime='ALIGNED',
                       news={'status': 'CLEAR', 'entry_allowed': True})
    assert ctx['key'] == 'GOLD|UP|ALIGNED|CLEAR'
    assert soft_key(ctx['key']) == 'GOLD|UP|ALIGNED'
    assert news_bucket({'status': 'NEWS_WINDOW'}) == 'WINDOW'
    assert news_bucket({'status': 'X', 'entry_allowed': False}) == 'BLOCK'


def test_should_save_requires_proven_window():
    assert should_save_champion({'n': 3, 'net': 5.0, 'win_rate': 1.0}) is False
    assert should_save_champion({
        'n': DEFAULT_MIN_TRADES, 'net': 0.01, 'win_rate': 0.8}) is False
    assert should_save_champion({
        'n': DEFAULT_MIN_TRADES, 'net': 1.0, 'win_rate': 0.3}) is False
    assert should_save_champion({
        'n': DEFAULT_MIN_TRADES, 'net': 1.0, 'win_rate': 0.6}) is True


def test_multiple_context_champions_and_match():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        store = Store(root / 's.db')
        brain = AdaptiveBrain(store, data_dir=root / 'b', n=8, seed=3)
        brain.epic = 'GOLD'
        brain.model_version = 7
        engine = MagicMock()
        engine.brain = brain
        engine.feature_registry = MagicMock(approved=[], approved_ids=lambda: [])
        engine.positions = []
        engine.restore_neural_weights = MagicMock(return_value=True)

        up = context_from(epic='GOLD', trend='UP', regime='ALIGNED',
                          news={'status': 'CLEAR'})
        down = context_from(epic='GOLD', trend='DOWN', regime='SHIFT',
                            news={'status': 'CLEAR'})
        for _ in range(DEFAULT_MIN_TRADES):
            append_trade(store, context=up, pnl=0.4, deal_id='u')
            append_trade(store, context=down, pnl=0.5, deal_id='d')

        a = consider_save(store, engine, context=up)
        b = consider_save(store, engine, context=down)
        assert a and b
        assert a['key'] != b['key']
        lib = load(store)
        assert len(lib['champions']) == 2

        hit = find_match(lib, up)
        assert hit['match'] == 'exact'
        assert hit['key'] == up['key']

        # Soft: same epic|trend|regime, different news → still matches.
        soft_ctx = context_from(epic='GOLD', trend='UP', regime='ALIGNED',
                                news={'status': 'NEWS_WINDOW'})
        soft = find_match(lib, soft_ctx, allow_soft=True)
        assert soft and soft['match'] == 'soft'

        act = maybe_activate(store, engine, context=up)
        assert act and act.get('activated') is True
        engine.restore_neural_weights.assert_called()
        # Mid-trade: never swap.
        engine.positions = [{'dealId': '1'}]
        assert maybe_activate(store, engine, context=down) is None

        snap = snapshot(store)
        assert snap['champions'] == 2
        assert snap['active']['key'] == up['key']


def test_losing_context_not_saved():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        store = Store(root / 's.db')
        brain = AdaptiveBrain(store, data_dir=root / 'b', n=8, seed=1)
        engine = MagicMock(brain=brain, feature_registry=MagicMock(
            approved=[], approved_ids=lambda: []), positions=[])
        ctx = context_from(epic='GOLD', trend='MIXED', regime='ALIGNED',
                           news={'status': 'CLEAR'})
        for i in range(8):
            append_trade(store, context=ctx, pnl=(-0.5 if i % 2 else 0.2))
        assert consider_save(store, engine, context=ctx) is None
        assert load(store)['champions'] == {}
