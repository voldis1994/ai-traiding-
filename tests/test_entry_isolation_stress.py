"""Stress: in-trade M1 flip-churn poisons next OPEN; freeze+reset+structure wins £.

Artificial alternating regimes (DOWN, UP, DOWN, …):
  • Correct: after a loss in a lethal frame, prefer HOLD (structure teacher).
  • Old bug: while in-trade, M1 adverse labels flip the entry weights to the
    opposite side → next regime is the other way → flip loses again.

Pass: fixed (freeze in-trade + flat RNN reset + structure HOLD) beats baseline £.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from app.engine import AdaptiveBrain, Store
from app.profit_learning import label_from_profit
from app import structure_shadow as ss


EPIC = 'GOLD'
LOSS = -0.12
WIN = 0.28
N_EPISODES = 48
IN_TRADE_BARS = 10


def sandbox():
    return tempfile.TemporaryDirectory()


def _scores(brain, transition):
    core = brain.core
    z = np.asarray(transition['z'], dtype=float)
    h = np.asarray(transition['h'], dtype=float)
    c = np.asarray(transition['c'], dtype=float)
    _, _, logits = core._forward(z, h, c, float(transition.get('dt') or 1.0))
    return dict(zip(('BUY', 'SELL', 'HOLD'), map(float, core._softmax(logits))))


def _action(scores):
    return max(('BUY', 'SELL', 'HOLD'), key=lambda a: scores[a])


def _make_transition(brain):
    d = int(brain.core.d)
    z = np.linspace(-0.15, 0.15, d)
    brain.core.last_z = z.copy()
    h0, c0 = brain.core.h.copy(), brain.core.c.copy()
    tr = {
        'z': z.copy(), 'h': h0, 'c': c0, 'dt': 1.0,
        'neuron_ids': brain.core.neuron_ids.copy(),
    }
    brain.core.last_transition = {
        'z': z.copy(), 'h': h0.copy(), 'c': c0.copy(), 'dt': 1.0,
        'neuron_ids': brain.core.neuron_ids.copy(),
    }
    return tr


def _bias_buy(brain, tr, n=20):
    for _ in range(n):
        brain.learn_from_trade(tr, 0, lr=0.45, won=True)


def _ohlc_features(brain):
    return [0.0] * int(brain.ohlc_dim)


def _down_bars(mid):
    return [{
        'open': mid + (8 - i) * 0.4, 'high': mid + (8 - i) * 0.4 + 0.05,
        'low': mid + (7 - i) * 0.4 - 0.05, 'close': mid + (7 - i) * 0.4,
        'mid': mid + (7 - i) * 0.4, 'at': f'd{i}',
    } for i in range(8)]


def _up_bars(mid):
    return [{
        'open': mid + i * 0.4, 'high': mid + (i + 1) * 0.4 + 0.05,
        'low': mid + i * 0.4 - 0.05, 'close': mid + (i + 1) * 0.4,
        'mid': mid + (i + 1) * 0.4, 'at': f'u{i}',
    } for i in range(8)]


def _run_campaign(*, freeze_in_trade: bool, reset_flat: bool, structure_hold: bool):
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        # Warm OHLC schema so tick accepts features.
        t0 = '2026-05-01T00:00:00+00:00'
        brain.tick(4000.0, 0.5, 0.1, EPIC, dt=60, features=_ohlc_features(brain),
                   bar_time=t0, in_position=False)
        tr0 = _make_transition(brain)
        _bias_buy(brain, tr0)

        pnls = []
        holds = 0
        mid = 4000.0
        minute = 1
        for ep in range(N_EPISODES):
            regime = 'DOWN' if (ep % 2 == 0) else 'UP'
            if reset_flat:
                # Flat OPEN always from cleared recurrent memory.
                brain.reset_flat_state(reason='pre_open')
            tr = _make_transition(brain)
            act = _action(_scores(brain, tr))
            if act == 'HOLD':
                holds += 1
                # Flat perceive+train ok.
                brain.tick(
                    mid, 0.5, 0.1, EPIC, dt=60, features=_ohlc_features(brain),
                    bar_time=f'2026-05-01T{minute // 60:02d}:{minute % 60:02d}:00+00:00',
                    in_position=False)
                minute += 1
                mid += (-0.2 if regime == 'DOWN' else 0.2)
                continue

            # Directional open.
            entry_tr = dict(tr)
            if (act == 'BUY' and regime == 'DOWN') or (act == 'SELL' and regime == 'UP'):
                pnls.append(LOSS)
                won = False
            else:
                pnls.append(WIN)
                won = True

            # In-trade bars.
            for j in range(IN_TRADE_BARS):
                mid += (-0.45 if regime == 'DOWN' else 0.45)
                bt = f'2026-05-01T{minute // 60:02d}:{minute % 60:02d}:00+00:00'
                minute += 1
                brain.tick(
                    mid, 0.5, 0.1, EPIC, dt=60, features=_ohlc_features(brain),
                    bar_time=bt, in_position=freeze_in_trade)
                if not freeze_in_trade and not won:
                    # Old bug: adverse M1 flips entry weights to the opposite side.
                    flip = 1 if act == 'BUY' else 0
                    brain.learn_from_trade(entry_tr, flip, lr=0.35, won=False)

            bars = _down_bars(mid) if regime == 'DOWN' else _up_bars(mid)
            if not won:
                ss.record_loss(
                    store, epic=EPIC, direction=act,
                    entry=bars[0]['open'], exit_level=bars[-1]['close'],
                    exit_reason='sl', broker_pnl=LOSS, bars=bars, deal_id=f'e{ep}')
                frame = ss.frame_from_ohlc(bars)
                prefer = ss.library_prefers_hold(
                    store, epic=EPIC, structure=frame.get('structure'), direction=act)
                decision = label_from_profit(
                    act, LOSS, entry=bars[0]['open'], exit_level=bars[-1]['close'],
                    spread=0.5, bars=bars, stop_distance=2.0, profit_distance=2.0,
                    tick=0.1, trail=False,
                    structure_prefer_hold=bool(
                        structure_hold and prefer.get('prefer_hold')))
                lab = decision.get('label')
                if lab is None:
                    lab = 2
                for _ in range(3):
                    brain.learn_from_trade(entry_tr, int(lab), lr=0.4, won=False)
            else:
                for _ in range(2):
                    brain.learn_from_trade(
                        entry_tr, 0 if act == 'BUY' else 1, lr=0.35, won=True)

            if reset_flat:
                brain.reset_flat_state(reason='post_close')

        return {
            'net': float(sum(pnls)),
            'trades': len(pnls),
            'holds': holds,
            'losses': sum(1 for p in pnls if p < 0),
            'wins': sum(1 for p in pnls if p > 0),
        }


def test_stress_freeze_and_reset_beats_flip_churn():
    baseline = _run_campaign(
        freeze_in_trade=False, reset_flat=False, structure_hold=False)
    fixed = _run_campaign(
        freeze_in_trade=True, reset_flat=True, structure_hold=True)
    print(f'baseline={baseline}')
    print(f'fixed={fixed}')
    assert fixed['trades'] + fixed['holds'] >= 1
    assert baseline['trades'] >= 1
    assert fixed['net'] > baseline['net'], (
        f'fixed net {fixed["net"]:.2f} must beat baseline {baseline["net"]:.2f}')


def test_in_position_tick_does_not_move_entry_weights():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain.tick(
            4000.0, 0.5, 0.1, EPIC, dt=60, features=_ohlc_features(brain),
            bar_time='2026-04-01T00:00:00+00:00', in_position=False)
        before = brain.core.R.copy()
        mid = 4000.0
        for j in range(1, 21):
            mid -= 0.5
            out = brain.tick(
                mid, 0.5, 0.1, EPIC, dt=60, features=_ohlc_features(brain),
                bar_time=f'2026-04-01T00:{j:02d}:00+00:00',
                in_position=True)
            assert out is not None
            assert out.get('entry_train') is False
        assert np.allclose(before, brain.core.R), 'in-trade M1 must not ΔW entry R'


def test_reset_flat_clears_recurrent_memory():
    with sandbox() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        brain.core.h[:] = 0.7
        brain.core.c[:] = -0.4
        brain.core.last_transition = {'z': [0.1]}
        brain.pending.append({'epic': EPIC})
        brain.reset_flat_state(reason='unit')
        assert float(np.max(np.abs(brain.core.h))) == 0.0
        assert float(np.max(np.abs(brain.core.c))) == 0.0
        assert brain.core.last_transition is None
        assert list(brain.pending) == []
