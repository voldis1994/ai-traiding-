"""End-result proof: structure shadow teaches LIVE neurons — no hard veto.

Operator logic:
  LIVE executes only neural BUY/SELL/HOLD.
  Shadow frames why SL happened and teaches a different label into weights.
  Next votes shift away from the grave side — £ improves without an external gate.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from app.engine import AdaptiveBrain, Store
from app import structure_shadow as ss


LOSS_GBP = -0.12
WIN_GBP = 0.30


def _trend_down(n=12, start=3950.0, step=0.35):
    rows, px = [], start
    for i in range(n):
        o, c = px, px - step
        rows.append({
            'at': f'2024-06-01T00:{i:02d}:00+00:00',
            'open': o, 'high': o + 0.05, 'low': c - 0.05, 'close': c,
        })
        px = c
    return rows


def _trend_up(n=12, start=3950.0, step=0.35):
    rows, px = [], start
    for i in range(n):
        o, c = px, px + step
        rows.append({
            'at': f'2024-06-01T01:{i:02d}:00+00:00',
            'open': o, 'high': c + 0.05, 'low': o - 0.05, 'close': c,
        })
        px = c
    return rows


def _scores(brain, transition):
    """Softmax BUY/SELL/HOLD from a frozen transition (decision-time h/c)."""
    core = brain.core
    z = np.asarray(transition['z'], dtype=float)
    h = np.asarray(transition['h'], dtype=float)
    c = np.asarray(transition['c'], dtype=float)
    _, _, logits = core._forward(z, h, c, float(transition.get('dt') or 1.0))
    prob = core._softmax(logits)
    return dict(zip(('BUY', 'SELL', 'HOLD'), map(float, prob)))


def test_suggest_teach_explains_hostile_frame():
    frame = ss.frame_from_ohlc(_trend_down())
    assert frame['structure'] == 'TREND_DOWN'
    teach = ss.suggest_teach(frame=frame, direction='BUY', broker_pnl=LOSS_GBP)
    assert teach is not None
    assert teach['suggest'] in ('HOLD', 'SELL')
    assert teach['label'] in (1, 2)
    assert 'TREND_DOWN' in teach['why']


def test_advise_is_evidence_not_required_for_live():
    """advise() may say HOLD — that is shadow opinion, not a LIVE gate API."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        bars = _trend_down()
        for i in range(3):
            ss.record_loss(
                store, epic='GOLD', direction='BUY',
                entry=bars[0]['open'], exit_level=bars[-1]['close'],
                exit_reason='sl', broker_pnl=LOSS_GBP, bars=bars, deal_id=f'd{i}')
        advice = ss.advise(store, epic='GOLD', proposal='BUY', bars=bars)
        assert advice.get('action') == 'HOLD'  # opinion
        # Contract: callers must not treat this as submit veto — documented.
        assert 'reason' in advice


def test_structure_teach_shifts_live_neurons_away_from_grave_side():
    """After SL frames teach HOLD/opposite, BUY score on that state drops."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        z = np.linspace(-0.2, 0.2, brain.core.d)
        brain.core.last_z = z.copy()
        # Capture a decision-time transition (as OPEN would).
        h0, c0 = brain.core.h.copy(), brain.core.c.copy()
        brain.core.last_transition = {
            'z': z.copy(), 'h': h0, 'c': c0, 'dt': 1.0,
            'neuron_ids': brain.core.neuron_ids.copy(),
        }
        tr = dict(brain.core.last_transition)
        before = _scores(brain, tr)
        bars = _trend_down()
        frame = ss.frame_from_ohlc(bars)
        teach = ss.suggest_teach(frame=frame, direction='BUY', broker_pnl=LOSS_GBP)
        assert teach and teach['label'] in (0, 1, 2)
        # Simulate shadow → LIVE teach loop (same as engine after SL).
        for _ in range(8):
            brain.learn_from_trade(tr, int(teach['label']), lr=0.35, won=False)
        after = _scores(brain, tr)
        # Grave side BUY must not gain; HOLD or opposite should rise.
        assert after['BUY'] <= before['BUY'] + 1e-9
        if teach['suggest'] == 'HOLD':
            assert after['HOLD'] >= before['HOLD'] - 1e-9
            assert after['HOLD'] > before['HOLD'] or after['BUY'] < before['BUY']
        else:
            assert after['SELL'] >= before['SELL'] - 1e-9


def test_taught_brain_skips_more_graves_improves_pnl():
    """£ proof: stuck-BUY brain vs same brain after structure-shadow teach.

    Both start BUY-biased (like Gold flip streak). Blind keeps BUY→SL on
    TREND_DOWN. Teach path: after each SL, shadow frames why and trains HOLD
    into LIVE neurons — later argmax skips graves. No external HOLD veto.
    """
    with tempfile.TemporaryDirectory() as tmp:
        z = np.linspace(-0.15, 0.15, 48)  # filled per brain.d below
        down = _trend_down()
        up = _trend_up()
        # Hostile first (streak), then a few UP recoveries.
        episodes = [('DOWN', down)] * 12 + [('UP', up)] * 4

        def _fresh_buy_biased(tag: str):
            b = AdaptiveBrain(Store(Path(tmp) / f'{tag}.sqlite3'), data_dir=tmp)
            zz = np.linspace(-0.15, 0.15, b.core.d)
            b.core.last_z = zz.copy()
            h0, c0 = b.core.h.copy(), b.core.c.copy()
            local_tr = {
                'z': zz.copy(), 'h': h0, 'c': c0, 'dt': 1.0,
                'neuron_ids': b.core.neuron_ids.copy(),
            }
            b.core.last_transition = dict(local_tr)
            # Pound BUY into the core — stuck bullish (operator Gold symptom).
            for _ in range(25):
                b.learn_from_trade(local_tr, 0, lr=0.45, won=True)
            sc0 = _scores(b, local_tr)
            assert sc0['BUY'] == max(sc0.values())
            return b, local_tr

        def run(use_teach: bool):
            b, local_tr = _fresh_buy_biased('teach' if use_teach else 'blind')
            pnls = []
            holds = 0
            for kind, bars in episodes:
                sc = _scores(b, local_tr)
                action = max(('BUY', 'SELL', 'HOLD'), key=lambda a: sc[a])
                if action == 'HOLD':
                    holds += 1
                    continue
                if action == 'BUY' and kind == 'DOWN':
                    pnls.append(LOSS_GBP)
                    if use_teach:
                        frame = ss.frame_from_ohlc(bars)
                        teach = ss.suggest_teach(
                            frame=frame, direction='BUY', broker_pnl=LOSS_GBP)
                        # Engine uses ~2 repeats; amplify slightly for unit-test
                        # horizon so weight move is visible within 12 graves.
                        for _ in range(4):
                            b.learn_from_trade(
                                local_tr, int(teach['label']), lr=0.4, won=False)
                elif action == 'BUY' and kind == 'UP':
                    pnls.append(WIN_GBP)
                elif action == 'SELL' and kind == 'DOWN':
                    pnls.append(WIN_GBP)
                elif action == 'SELL' and kind == 'UP':
                    pnls.append(LOSS_GBP)
            return {
                'net': sum(pnls), 'trades': len(pnls), 'holds': holds,
                'losses': sum(1 for p in pnls if p < 0),
                'wins': sum(1 for p in pnls if p > 0),
            }

        blind = run(False)
        smart = run(True)
        print(
            f"BLIND  net=£{blind['net']:.2f} trades={blind['trades']} "
            f"losses={blind['losses']} holds={blind['holds']}")
        print(
            f"TEACH  net=£{smart['net']:.2f} trades={smart['trades']} "
            f"losses={smart['losses']} holds={smart['holds']}")
        print(f"DELTA  £{smart['net'] - blind['net']:+.2f}")
        assert blind['losses'] >= 10  # stuck BUY eats the DOWN streak
        assert smart['net'] > blind['net']
        assert smart['losses'] < blind['losses']
        # "Do differently" may be HOLD *or* opposite (SELL on TREND_DOWN) —
        # either path is neural, not an external veto. £ is the proof.
        assert smart['net'] - blind['net'] >= 1.0
