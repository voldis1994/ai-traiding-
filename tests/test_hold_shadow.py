"""HOLD abstention → opposite-path shadow (same brackets); teach if TP hit."""
import tempfile
from pathlib import Path

import pytest

from app import hold_shadow as hs
from app.engine import Store


def sandbox():
    return tempfile.TemporaryDirectory()


def _bars_trend(direction, n=40, start=100.0, step=0.4, spread=0.2):
    """Synthetic path that trends so one side hits TP and the other SL."""
    bars = []
    px = start
    for i in range(n):
        if direction == 'up':
            o, c = px, px + step
            h, l = c + 0.05, o - 0.05
        else:
            o, c = px, px - step
            h, l = o + 0.05, c - 0.05
        bars.append({'mid': c, 'high': h, 'low': l, 'spread': spread, 'at': f'b{i}'})
        px = c
    return bars


def test_evaluate_sides_opposite_hits_tp_on_down_move():
    bars = _bars_trend('down', n=30, step=0.5)
    # BUY proposal would lose; SELL opposite hits TP with 1.0 profit distance.
    out = hs.evaluate_sides(
        proposal='BUY', bars=bars, spread=0.2,
        stop_distance=1.0, profit_distance=1.0, tick=0.1, min_distance=0.2)
    assert out is not None
    assert out['opposite'] == 'SELL'
    assert out['opposite_hit_tp'] is True
    assert out['proposal_hit_tp'] is False
    assert out['teach_opposite'] is True


def test_evaluate_sides_no_teach_when_proposal_also_tps():
    # Degenerate short path — neither clear TP; teach_opposite false.
    bars = [{'mid': 100., 'high': 100.1, 'low': 99.9, 'spread': 0.2}] * 10
    out = hs.evaluate_sides(
        proposal='BUY', bars=bars, spread=0.2,
        stop_distance=5.0, profit_distance=5.0, tick=0.1)
    assert out is not None
    assert out['teach_opposite'] is False


def test_enqueue_and_resolve_teaches_opposite():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        transition = {
            'z': [0.1] * 8, 'h': [0.0] * 4, 'c': [0.0] * 4,
            'dt': 60., 'neuron_ids': list(range(4)),
        }
        row = hs.enqueue(
            store, proposal='BUY', reason='weak edge',
            bar_time='2026-01-01T10:00:00Z', epic='GOLD',
            entry=100.0, spread=0.2, stop_distance=1.0, profit_distance=1.0,
            tick=0.1, min_distance=0.2, transition=transition)
        assert row is not None
        assert len(hs.load_pending(store)) == 1
        taught = []

        def learn(label, tr, mode, detail):
            taught.append((label, mode, detail['opposite']))

        bars = _bars_trend('down', n=40, step=0.5)

        def candles_after(bar_time, epic):
            return bars

        outs = hs.resolve_ready(
            store, candles_after=candles_after, learn_callback=learn, horizon=30)
        assert outs
        assert outs[0]['opposite_hit_tp'] is True
        assert outs[0]['taught'] is True
        assert taught and taught[0][0] == hs._ACTION_LABEL['SELL']
        assert 'hold_missed_opposite_tp_sell' in taught[0][1]
        assert hs.load_stats(store)['taught_opposite'] == 1
        assert hs.load_pending(store) == []


def test_strategy_hint_after_enough_opposite_tps():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        stats = hs.default_stats()
        stats['resolved'] = 5
        stats['opposite_hit_tp'] = 3  # 60% ≥ 50%, n≥5
        stats = hs._update_strategy_hint(stats)
        assert stats['strategy_hint']
        assert 'opposite hit TP' in stats['strategy_hint']
        # Below min sample — no hint yet.
        cold = hs._update_strategy_hint({**hs.default_stats(), 'resolved': 3,
                                         'opposite_hit_tp': 3})
        assert cold['strategy_hint'] is None
        ev = hs.strategy_evidence(store)
        assert ev['pending'] == 0


def test_machine_spec_documents_hold_shadow():
    from app import machine_spec
    assert 'hold_shadow.enqueue' in machine_spec.HOLD_SHADOW_OPPOSITE
