"""Gold loss-streak diagnosis: what shadow taught vs what LIVE should teach.

Live symptom (operator): ~15 consecutive small Gold losses (~£0.10–0.16) with
``flip_loss_counterfactual_*`` modes — direction flipping every trade.

Root cause in the shadow path (not TP/SL hardcode):
  ``_shadow_paper_step`` taught losses via raw ``best_action_from_counterfactual``
  (endpoint mid, no opposite-only / spread floor). Missing or tiny spread made
  the opposite look "best" → false flip labels into the shadow brain → promote
  copied those poisoned weights into LIVE.

These tests freeze that diagnosis and the shared teacher gate.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.profit_learning import best_action_from_counterfactual, label_from_profit


GOLD_ENTRY = 3950.0
GOLD_SPREAD = 0.35
GOLD_TINY_MOVE = 0.08
GOLD_LOSS_GBP = -0.12


def _gold_alternating_losses(n=15):
    """BUY/SELL/BUY… each a small loss (matches operator screenshots)."""
    rows = []
    prev_dir = None
    for i in range(n):
        direction = 'BUY' if i % 2 == 0 else 'SELL'
        entry = GOLD_ENTRY + i * 0.1
        if direction == 'BUY':
            exit_px = entry - GOLD_TINY_MOVE
        else:
            exit_px = entry + GOLD_TINY_MOVE
        flip = bool(prev_dir and prev_dir != direction)
        rows.append((direction, entry, exit_px, GOLD_LOSS_GBP, flip))
        prev_dir = direction
    return rows


def test_gold_streak_raw_endpoint_cf_false_flips_without_spread():
    """Bug reproduction: endpoint CF with spread=0 flips almost every loss."""
    flips = 0
    for direction, entry, exit_px, pnl, _ in _gold_alternating_losses(15):
        sim = best_action_from_counterfactual(entry, exit_px, spread=0.0)
        opposite = 'SELL' if direction == 'BUY' else 'BUY'
        if sim and sim['best'] == opposite:
            flips += 1
    assert flips >= 12, f'expected false opposite dominance, got {flips}/15'


def test_gold_streak_shared_teacher_holds_when_spread_missing():
    """Fix: missing spread → HOLD teach, never false flip_loss_* side."""
    for direction, entry, exit_px, pnl, flip in _gold_alternating_losses(15):
        d = label_from_profit(
            direction, pnl, flip_after_loss=flip,
            entry=entry, exit_level=exit_px, spread=0.0)
        assert d['label'] == 2
        assert 'hold' in d['mode']
        assert d['mode'] not in (
            'flip_loss_counterfactual_buy', 'flip_loss_counterfactual_sell',
            'loss_counterfactual_buy', 'loss_counterfactual_sell')


def test_gold_streak_known_spread_tiny_move_teaches_hold_not_flip():
    """Real Gold spread: tiny opposite mid move fails 0.25×spread floor."""
    for direction, entry, exit_px, pnl, flip in _gold_alternating_losses(15):
        d = label_from_profit(
            direction, pnl, flip_after_loss=flip,
            entry=entry, exit_level=exit_px, spread=GOLD_SPREAD)
        assert d['label'] == 2
        assert d['mode'].endswith('hold')


def test_shadow_paper_close_must_not_match_raw_false_flip():
    """Closed paper row with spread=0: shared teacher ≠ raw endpoint flip."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j_hist_direction = 'BUY'
        entry, exit_px = GOLD_ENTRY, GOLD_ENTRY - 2.0
        sim = best_action_from_counterfactual(entry, exit_px, spread=0.0)
        decision = label_from_profit(
            j_hist_direction, -2.0, entry=entry, exit_level=exit_px, spread=0.0)
        assert decision['label'] == 2
        assert 'missing_spread' in decision['mode']
        # Raw CF typically picks SELL (opposite) — that used to be taught to shadow.
        assert sim is not None and sim['best'] == 'SELL'
        assert decision['label'] != {'BUY': 0, 'SELL': 1}[sim['best']]


def test_shadow_paper_step_emits_hold_teach_event():
    """Engine shadow step applies label_from_profit and logs the mode."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        brain.core.last_z = np.linspace(-0.05, 0.05, brain.core.d)
        e = SingleTradeEngine(store, brain)
        shadow = e._ensure_shadow_brain()
        e._shadow_scores_now(advance=True, bar_time='seed')
        j = e.shadow_paper
        j.cash_scale = 1.0
        j.position = {
            'direction': 'SELL', 'entry': GOLD_ENTRY,
            'stop': GOLD_ENTRY + 1.5, 'profit': GOLD_ENTRY - 1.5,
            'original_risk': 1.5, 'tick': 0.01, 'min_distance': 0.1,
            'opened_at': 'seed', 'bar_time': 'seed',
            'model_version': 0, 'kappa': 1.0, 'sl_scale': 1.0,
            'shadow_transition': dict(shadow.last_transition),
            'trail_extreme': None,
        }
        closed = e._shadow_paper_step(
            mid=GOLD_ENTRY + 2.0, spread=0.0, bar_time='close1',
            high=GOLD_ENTRY + 2.5, low=GOLD_ENTRY + 1.0, candles=[])
        assert closed is not None
        assert closed['exit_reason'] in ('sl', 'tp', 'trail')
        taught = [ev for ev in store.recent(40)
                  if ev.get('kind') == 'shadow_paper'
                  and 'taught' in str(ev.get('message') or '').lower()]
        assert taught, 'expected shadow_paper teach event'
        details = taught[0].get('details')
        if isinstance(details, str):
            details = json.loads(details)
        assert details.get('label') == 2
        assert 'hold' in str(details.get('mode') or '')
