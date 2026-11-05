"""End-result proof: structure shadow improves £ vs blind re-entry.

Scenario (Gold-like scratch SL world):
  - Many TREND_DOWN windows where BUY → SL (−£0.12)
  - Occasional TREND_UP windows where BUY → TP (+£0.30)
  - Neural always proposes BUY (worst case: directionally stuck)

Without structure memory: takes every BUY → death by a thousand scratches.
With structure memory: after MIN_ADVICE_SAMPLES SL graves on TREND_DOWN|BUY,
further BUY proposals in that frame become HOLD → skips the graves, still
takes TREND_UP wins.

Asserts: net £, loss count, and max loss-streak all improve with shadow on.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.engine import Store
from app import structure_shadow as ss


LOSS_GBP = -0.12
WIN_GBP = 0.30
N_DOWN = 15   # hostile TREND_DOWN episodes
N_UP = 4      # favorable TREND_UP episodes interleaved


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


def _run(*, use_structure_shadow: bool) -> dict:
    """Replay the same proposal stream; optionally apply structure advice."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3') if use_structure_shadow else None
        # When shadow off, still need a throwaway store for frame classify only
        # if we want — but advice is skipped entirely.
        if store is None:
            store = Store(Path(tmp) / 'off.sqlite3')
            shadow_on = False
        else:
            shadow_on = True

        pnls = []
        actions = []
        holds = 0
        deal = 0
        # Exact mix: 15 hostile DOWN + 4 favorable UP, interleaved (4 DOWN, 1 UP).
        episodes = []
        down_left, up_left = N_DOWN, N_UP
        i = 0
        while down_left or up_left:
            # Every 5th slot prefers UP while any remain; else DOWN.
            take_up = up_left > 0 and (i % 5 == 4 or down_left == 0)
            if take_up:
                episodes.append(('UP', _trend_up(start=3950 + i)))
                up_left -= 1
            else:
                episodes.append(('DOWN', _trend_down(start=3950 + i * 0.5)))
                down_left -= 1
            i += 1
        assert sum(1 for e in episodes if e[0] == 'DOWN') == N_DOWN
        assert sum(1 for e in episodes if e[0] == 'UP') == N_UP

        for kind, bars in episodes:
            proposal = 'BUY'  # stuck bullish brain
            frame = ss.frame_from_ohlc(bars)
            assert frame['ready']
            if kind == 'DOWN':
                assert frame['structure'] == 'TREND_DOWN'
            else:
                assert frame['structure'] == 'TREND_UP'

            action = proposal
            if shadow_on:
                advice = ss.advise(
                    store, epic='GOLD', proposal=proposal, bars=bars)
                if advice.get('action') == 'HOLD':
                    action = 'HOLD'
                    holds += 1
                    actions.append('HOLD')
                    continue  # flat — no £

            # Execute proposal
            if kind == 'DOWN':
                pnl = LOSS_GBP
                exit_reason = 'sl'
            else:
                pnl = WIN_GBP
                exit_reason = 'tp'
            pnls.append(pnl)
            actions.append(action)
            deal += 1
            if shadow_on and pnl < 0:
                ss.record_loss(
                    store, epic='GOLD', direction=proposal,
                    entry=bars[0]['open'], exit_level=bars[-1]['close'],
                    exit_reason=exit_reason, broker_pnl=pnl,
                    bars=bars, deal_id=f'd{deal}', spread=0.35)

        # Loss streak (consecutive negative pnls in executed trades)
        max_streak = streak = 0
        for p in pnls:
            if p < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0

        return {
            'shadow': shadow_on,
            'net': sum(pnls),
            'trades': len(pnls),
            'holds': holds,
            'losses': sum(1 for p in pnls if p < 0),
            'wins': sum(1 for p in pnls if p > 0),
            'max_loss_streak': max_streak,
            'pnls': pnls,
            'actions': actions,
        }


def test_structure_shadow_improves_net_pnl():
    blind = _run(use_structure_shadow=False)
    smart = _run(use_structure_shadow=True)

    # Blind takes every hostile BUY → many losses.
    assert blind['losses'] >= N_DOWN - 1
    assert blind['holds'] == 0

    # Shadow learns after MIN_ADVICE_SAMPLES and skips further DOWN graves.
    assert smart['holds'] >= N_DOWN - ss.MIN_ADVICE_SAMPLES
    assert smart['losses'] < blind['losses']
    assert smart['net'] > blind['net']
    assert smart['max_loss_streak'] <= ss.MIN_ADVICE_SAMPLES
    assert smart['max_loss_streak'] < blind['max_loss_streak']
    # Still captured the UP wins (shadow does not block TREND_UP|BUY).
    assert smart['wins'] == blind['wins'] == N_UP


def test_structure_shadow_improvement_is_material():
    """£ gap must be more than noise — at least several skipped scratch SL."""
    blind = _run(use_structure_shadow=False)
    smart = _run(use_structure_shadow=True)
    saved = smart['net'] - blind['net']
    skipped = blind['losses'] - smart['losses']
    assert skipped >= 5
    # Each skipped SL saves |LOSS_GBP|; wins identical → gap ≈ skipped * |loss|
    assert saved >= skipped * abs(LOSS_GBP) - 1e-9
    assert saved >= 0.60  # ≥ five × £0.12


def test_print_comparison_for_operator(capsys):
    """Human-readable proof line (always asserts improvement)."""
    blind = _run(use_structure_shadow=False)
    smart = _run(use_structure_shadow=True)
    print(
        f"BLIND  net=£{blind['net']:.2f} trades={blind['trades']} "
        f"losses={blind['losses']} max_streak={blind['max_loss_streak']}")
    print(
        f"SHADOW net=£{smart['net']:.2f} trades={smart['trades']} "
        f"losses={smart['losses']} holds={smart['holds']} "
        f"max_streak={smart['max_loss_streak']}")
    print(
        f"DELTA  £{smart['net'] - blind['net']:+.2f} "
        f"(skipped {blind['losses'] - smart['losses']} SL graves)")
    assert smart['net'] > blind['net']
