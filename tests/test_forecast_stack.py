"""Multi-horizon forecast stack: memory, targets, model, calibration, benchmark."""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from app.engine import Store
from app.forecast_benchmark import run_benchmark
from app.forecast_calibration import expected_calibration_error, summarize_calibration
from app.forecast_memory import ForecastMemory
from app.forecast_model import MultiHorizonForecast
from app.forecast_targets import (
    HORIZONS, direction_label, mid_change, targets_at_horizons,
)


def test_targets_cover_1_3_5_15():
    entry = 2650.0
    # Rising path
    future = [2651.0, 2652.0, 2653.0, 2654.0, 2655.0] + [2650.0 + i for i in range(6, 16)]
    out = targets_at_horizons(entry, future, spread=0.2)
    assert set(out) == set(HORIZONS)
    assert out[1]['r'] == pytest.approx(1.0)
    assert out[1]['label'] == 0  # BUY
    assert mid_change(entry, 2649.0) == pytest.approx(-1.0)
    assert direction_label(entry, entry, cost=0.3) == 2  # HOLD


def test_forecast_memory_record_and_resolve():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        mem = ForecastMemory(store)
        feats = list(np.linspace(-0.5, 0.5, 32))
        scores = {'BUY': 0.5, 'SELL': 0.2, 'HOLD': 0.3}
        candles = []
        t0 = '2026-11-17T10:00:00+00:00'
        rid = mem.record(
            epic='GOLD', features=feats, scores=scores, mid=2650.0,
            spread=0.3, tick_size=0.1, bar_time=t0, signal_id=1)
        assert rid is not None
        # Build 16 subsequent M1 candles
        for i in range(16):
            candles.append({
                'time': f'2026-11-17T10:{i+1:02d}:00+00:00',
                'close': 2650.0 + (i + 1) * 0.5,
                'spread': 0.3,
            })
        candles_full = [{'time': t0, 'close': 2650.0, 'spread': 0.3}] + candles
        n = mem.resolve_with_candles(candles_full, epic='GOLD')
        assert n == 1
        rows = mem.rows(epic='GOLD', resolved_only=True)
        assert len(rows) == 1
        assert len(rows[0]['features']) == 32
        assert set(rows[0]['outcomes']) == set(HORIZONS)
        s = mem.summary(epic='GOLD')
        assert s['resolved'] == 1


def test_model_predicts_r_sigma_p_all_horizons():
    rng = np.random.default_rng(0)
    dim = 8
    rows = []
    for i in range(40):
        x = rng.normal(size=dim)
        # Synthetic: first feature drives short-horizon return
        r1 = 0.5 * x[0]
        outcomes = {}
        for h in HORIZONS:
            r = r1 * (1 + 0.1 * (h - 1))
            lab = 0 if r > 0.05 else (1 if r < -0.05 else 2)
            outcomes[h] = {'r': float(r), 'label': lab, 'sigma_proxy': abs(r)}
        rows.append({
            'features': x.tolist(),
            'scores': {'BUY': 0.4, 'SELL': 0.3, 'HOLD': 0.3},
            'outcomes': outcomes,
        })
    model = MultiHorizonForecast(dim=dim, epochs=25, seed=1)
    info = model.fit(rows)
    assert info['fitted']
    pred = model.predict_one(rows[0]['features'])
    assert len(pred) == 4
    for p in pred:
        assert p.horizon in HORIZONS
        assert p.sigma_hat > 0
        assert abs(sum(p.p_hat.values()) - 1.0) < 1e-6


def test_calibration_ece_defined():
    probs = [{'BUY': 0.8, 'SELL': 0.1, 'HOLD': 0.1}] * 20
    labels = [0] * 16 + [2] * 4  # mostly BUY when confident BUY
    ece = expected_calibration_error(probs, labels, n_bins=5)
    assert ece is not None
    assert 0 <= ece <= 1
    summary = summarize_calibration(probs, labels)
    assert 'ece' in summary and summary['n'] == 20


def test_benchmark_compares_on_chrono_split():
    rng = np.random.default_rng(2)
    dim = 6
    rows = []
    for i in range(50):
        x = rng.normal(size=dim)
        r = float(0.8 * x[0])
        lab = 0 if r > 0.05 else (1 if r < -0.05 else 2)
        # Baseline scores loosely related
        scores = {'BUY': 0.34, 'SELL': 0.33, 'HOLD': 0.33}
        if lab == 0:
            scores = {'BUY': 0.5, 'SELL': 0.25, 'HOLD': 0.25}
        elif lab == 1:
            scores = {'BUY': 0.25, 'SELL': 0.5, 'HOLD': 0.25}
        outcomes = {h: {'r': r * (1 + 0.05 * h), 'label': lab, 'sigma_proxy': abs(r)}
                    for h in HORIZONS}
        rows.append({'features': x.tolist(), 'scores': scores, 'outcomes': outcomes})
    report = run_benchmark(rows, seed=3)
    assert report['ok']
    assert report['by_horizon'][1]['n'] >= 4
    assert 'baseline_argmax_acc' in report['by_horizon'][1]
    assert 'forecast_model_acc' in report['by_horizon'][1]
    assert 'no new execution' in report['note'].lower() or 'Forecast quality' in report['note']
