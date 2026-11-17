"""Multi-horizon forecast stack: memory, targets, model, calibration, benchmark."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.engine import Store
from app.forecast_benchmark import chrono_split, run_benchmark, run_gold_benchmark
from app.forecast_calibration import expected_calibration_error, summarize_calibration
from app.forecast_memory import ForecastMemory
from app.forecast_model import MultiHorizonForecast
from app.forecast_targets import (
    HORIZONS, direction_label, mid_change, path_from_candles, targets_at_horizons,
)


def test_targets_cover_1_3_5_15():
    entry = 2650.0
    future = [2651.0, 2652.0, 2653.0, 2654.0, 2655.0] + [2650.0 + i for i in range(6, 16)]
    out = targets_at_horizons(entry, future, spread=0.2)
    assert set(out) == set(HORIZONS)
    assert out[1]['r'] == pytest.approx(1.0)
    assert out[1]['label'] == 0  # BUY
    assert mid_change(entry, 2649.0) == pytest.approx(-1.0)
    assert direction_label(entry, entry, cost=0.3) == 2  # HOLD


def test_path_from_candles_matches_store_at_key():
    """Store candles use ``at`` — must still link forecast time → future mids."""
    t0 = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(20):
        ts = t0 + timedelta(minutes=i)
        candles.append({
            'at': ts.isoformat().replace('+00:00', 'Z'),
            'close': 2650.0 + i * 0.5,
            'spread': 0.3,
        })
    mids, spreads, matched = path_from_candles(
        candles, after_time=t0.isoformat(), max_h=15)
    assert matched is not None
    assert len(mids) == 15
    assert mids[0] == pytest.approx(2650.5)   # h=1
    assert mids[2] == pytest.approx(2651.5)   # h=3
    assert mids[14] == pytest.approx(2657.5)  # h=15


def test_path_from_candles_rejects_minute_gap():
    """10:00 then 10:02 must not become a fake h=1 outcome."""
    t0 = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    candles = [
        {'at': t0.isoformat(), 'close': 2650.0, 'spread': 0.3},
        # missing 10:01
        {'at': (t0 + timedelta(minutes=2)).isoformat(), 'close': 2652.0, 'spread': 0.3},
        {'at': (t0 + timedelta(minutes=3)).isoformat(), 'close': 2653.0, 'spread': 0.3},
    ]
    mids, spreads, matched = path_from_candles(
        candles, after_time=t0.isoformat(), max_h=15)
    assert matched is not None
    assert mids == []  # gap stops path before any horizon
    # Contiguous prefix then gap: keep only bars before the hole.
    candles2 = [
        {'at': t0.isoformat(), 'close': 2650.0, 'spread': 0.3},
        {'at': (t0 + timedelta(minutes=1)).isoformat(), 'close': 2651.0, 'spread': 0.3},
        {'at': (t0 + timedelta(minutes=2)).isoformat(), 'close': 2652.0, 'spread': 0.3},
        # skip 10:03
        {'at': (t0 + timedelta(minutes=4)).isoformat(), 'close': 2654.0, 'spread': 0.3},
    ]
    mids2, _, _ = path_from_candles(candles2, after_time=t0.isoformat(), max_h=15)
    assert len(mids2) == 2
    assert mids2[0] == pytest.approx(2651.0)
    assert mids2[1] == pytest.approx(2652.0)


def test_sigma_depends_on_input_features():
    model = MultiHorizonForecast(dim=4, epochs=1, seed=0)
    x_quiet = np.zeros(4)
    x_wild = np.array([3.0, 0.0, 0.0, 0.0])
    # Feature-dependent σ head: first feature raises uncertainty.
    model.W_s[:] = 0.0
    model.W_s[:, 0] = 0.5
    model.b_s[:] = -1.0
    s0 = model.predict_one(x_quiet)[0].sigma_hat
    s1 = model.predict_one(x_wild)[0].sigma_hat
    assert s1 > s0
    assert s0 > 0 and s1 > 0


def test_forecast_memory_links_all_horizons_via_at():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        mem = ForecastMemory(store)
        feats = list(np.linspace(-0.5, 0.5, 32))
        scores = {'BUY': 0.5, 'SELL': 0.2, 'HOLD': 0.3}
        t0 = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
        rid = mem.record(
            epic='GOLD', features=feats, scores=scores, mid=2650.0,
            spread=0.3, tick_size=0.1, bar_time=t0.isoformat(), signal_id=1)
        assert rid is not None
        candles = []
        for i in range(16):
            ts = t0 + timedelta(minutes=i)
            candles.append({
                'at': ts.isoformat(),
                'close': 2650.0 + i * 0.5,
                'spread': 0.3,
            })
        n = mem.resolve_with_candles(candles, epic='GOLD')
        assert n == 1
        rows = mem.rows(epic='GOLD', resolved_only=True)
        assert set(rows[0]['outcomes']) == set(HORIZONS)
        assert rows[0]['outcomes'][1]['r'] == pytest.approx(0.5)
        assert rows[0]['outcomes'][3]['r'] == pytest.approx(1.5)
        assert rows[0]['outcomes'][15]['r'] == pytest.approx(7.5)


def test_record_while_in_position_does_not_require_flat():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        mem = ForecastMemory(store)
        feats = [0.1] * 8
        scores = {'BUY': 0.4, 'SELL': 0.3, 'HOLD': 0.3}
        rid = mem.record(
            epic='GOLD', features=feats, scores=scores, mid=2650.0,
            bar_time='2026-11-17T12:00:00+00:00', in_position=True)
        assert rid is not None
        rows = mem.rows(resolved_only=False)
        assert rows[0]['in_position'] is True
        assert mem.summary()['in_position_rows'] == 1


def test_chrono_split_embargo_blocks_horizon_leakage():
    rows = [{'id': i, 'time': f'2026-11-17T10:{i:02d}:00+00:00',
             'features': [0.0], 'scores': {}, 'outcomes': {}}
            for i in range(40)]
    train, val, meta = chrono_split(rows, train_frac=0.7, horizons=HORIZONS)
    assert meta['embargo'] == 15
    # First val index must be ≥ cut + 15
    assert rows.index(val[0]) >= meta['cut'] + 15
    # Gap rows are neither in train nor val
    gap = rows[meta['cut']:meta['cut'] + meta['embargo']]
    assert all(r not in train and r not in val for r in gap)


def test_model_predicts_r_sigma_p_all_horizons():
    rng = np.random.default_rng(0)
    dim = 8
    rows = []
    for i in range(40):
        x = rng.normal(size=dim)
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
    labels = [0] * 16 + [2] * 4
    ece = expected_calibration_error(probs, labels, n_bins=5)
    assert ece is not None
    assert 0 <= ece <= 1
    summary = summarize_calibration(probs, labels)
    assert 'ece' in summary and summary['n'] == 20


def _gold_rows(n=80, dim=6, seed=2):
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        x = rng.normal(size=dim)
        r = float(0.8 * x[0])
        lab = 0 if r > 0.05 else (1 if r < -0.05 else 2)
        scores = {'BUY': 0.34, 'SELL': 0.33, 'HOLD': 0.33}
        if lab == 0:
            scores = {'BUY': 0.5, 'SELL': 0.25, 'HOLD': 0.25}
        elif lab == 1:
            scores = {'BUY': 0.25, 'SELL': 0.5, 'HOLD': 0.25}
        outcomes = {h: {'r': r * (1 + 0.05 * h), 'label': lab, 'sigma_proxy': abs(r)}
                    for h in HORIZONS}
        rows.append({
            'id': i,
            'time': (t0 + timedelta(minutes=i)).isoformat(),
            'epic': 'GOLD',
            'features': x.tolist(),
            'scores': scores,
            'outcomes': outcomes,
        })
    return rows


def test_benchmark_compares_on_chrono_split():
    report = run_benchmark(_gold_rows(80), seed=3)
    assert report['ok']
    assert report['split']['embargo'] == 15
    assert report['by_horizon'][1]['n'] >= 4
    assert 'baseline_argmax_acc' in report['by_horizon'][1]
    assert 'forecast_model_acc' in report['by_horizon'][1]


def test_gold_benchmark_same_data_train_and_compare():
    rows = _gold_rows(90, seed=5)
    # Mix in a non-GOLD row — must be ignored.
    rows.append({
        'id': 999, 'time': '2026-11-17T20:00:00+00:00', 'epic': 'SILVER',
        'features': [0.0] * 6, 'scores': {'BUY': 0.3, 'SELL': 0.3, 'HOLD': 0.4},
        'outcomes': {h: {'r': 0.0, 'label': 2, 'sigma_proxy': 0.0} for h in HORIZONS},
    })
    report = run_gold_benchmark(rows, seed=7)
    assert report['ok']
    assert report['epic'] == 'GOLD'
    assert report['split']['embargo'] >= 15
    h1 = report['by_horizon'][1]
    assert h1['baseline_argmax_acc'] is not None
    assert h1['forecast_model_acc'] is not None
