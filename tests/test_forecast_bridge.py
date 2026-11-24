"""Forecast bridge: merge candles, fit head, soft LIVE nudge (not NeuralCore replace)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.engine import Store
from app import forecast_bridge
from app.forecast_memory import ForecastMemory
from app.forecast_targets import HORIZONS


def _seed_resolved(store, n=40, dim=8, epic='GOLD'):
    mem = ForecastMemory(store)
    t0 = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
    rng = np.random.default_rng(0)
    for i in range(n):
        x = rng.normal(size=dim).tolist()
        r = 0.5 * x[0]
        lab = 0 if r > 0.05 else (1 if r < -0.05 else 2)
        outcomes = {h: {'r': float(r), 'label': lab, 'sigma_proxy': abs(r),
                        'exit_mid': 2650.0 + r}
                    for h in HORIZONS}
        # Direct insert resolved rows for fit tests
        store.db.execute(
            '''INSERT INTO forecast_memory
               (time,epic,signal_id,features,scores,mid,spread,tick_size,
                in_position,resolved,outcomes)
               VALUES(?,?,?,?,?,?,?,?,0,1,?)''',
            ((t0 + timedelta(minutes=i)).isoformat(), epic, i,
             __import__('json').dumps(x),
             __import__('json').dumps({'BUY': 0.4, 'SELL': 0.3, 'HOLD': 0.3}),
             2650.0, 0.3, 0.1,
             __import__('json').dumps({str(h): outcomes[h] for h in HORIZONS})))
    store.db.commit()


def test_merge_candles_dedupes_and_drops_forming():
    now = datetime(2026, 11, 17, 10, 30, 20, tzinfo=timezone.utc)
    hist = [{'at': '2026-11-17T10:28:00+00:00', 'close': 1.0}]
    quote = [
        {'at': '2026-11-17T10:29:00+00:00', 'close': 2.0},
        {'at': '2026-11-17T10:30:00+00:00', 'close': 99.0},  # forming
    ]
    merged = forecast_bridge.merge_candle_lists(hist, quote, now_utc=now)
    assert len(merged) == 2
    assert merged[-1]['close'] == 2.0


def test_fit_and_soft_nudge_agree_boosts_side():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        _seed_resolved(store, n=40, dim=8)
        info = forecast_bridge.fit_from_memory(store, epic='GOLD', min_resolved=24)
        assert info['fitted']
        model = forecast_bridge.load_model(store)
        assert model is not None
        x = np.zeros(8); x[0] = 1.0
        advice = forecast_bridge.shadow_advice(
            store, x.tolist(),
            neural_scores={'BUY': 0.5, 'SELL': 0.25, 'HOLD': 0.25})
        assert advice.get('advice') in ('BUY', 'SELL', 'HOLD')
        scores = {'BUY': 0.40, 'SELL': 0.30, 'HOLD': 0.30}
        # Force agree BUY for nudge test
        advice = {'advice': 'BUY', 'confidence': 0.7, 'agree': True, 'reason': 'test'}
        nudged, meta = forecast_bridge.soft_nudge_scores(scores, advice, store=store)
        assert meta['nudged'] is True
        assert nudged['BUY'] > scores['BUY']
        assert abs(sum(nudged.values()) - 1.0) < 1e-9


def test_conflict_does_not_push_hold_or_flip():
    scores = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
    advice = {'advice': 'SELL', 'confidence': 0.8, 'agree': False, 'reason': 'conflict'}
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        nudged, meta = forecast_bridge.soft_nudge_scores(scores, advice, store=store)
        assert meta['nudged'] is False
        assert nudged == scores
        assert max(nudged, key=nudged.get) == 'BUY'


def test_forecast_hold_does_not_suppress_trade():
    scores = {'BUY': 0.50, 'SELL': 0.30, 'HOLD': 0.20}
    advice = {'advice': 'HOLD', 'confidence': 0.9, 'agree': False, 'reason': 'f_hold'}
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        nudged, meta = forecast_bridge.soft_nudge_scores(scores, advice, store=store)
        assert meta['nudged'] is False
        assert nudged['BUY'] == scores['BUY']
        assert max(nudged, key=nudged.get) == 'BUY'


def test_maybe_refit_and_shadow():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        # Floor raised to 40 resolved rows before LIVE soft nudge / refit.
        _seed_resolved(store, n=45, dim=6)
        out = forecast_bridge.maybe_refit_and_advise(
            store, epic='GOLD', features=[0.1] * 6,
            neural_scores={'BUY': 0.4, 'SELL': 0.3, 'HOLD': 0.3})
        assert out['resolved'] >= 40
        assert out['refit'] and out['refit'].get('fitted')
        assert out['shadow'] is not None
        assert out['shadow'].get('horizons')
