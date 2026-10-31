#!/usr/bin/env python3
"""From-zero LIVE cycle simulation — how the machine self-improves over N trades.

Starts with empty Store + fresh AdaptiveBrain + default unified_cycle / brackets /
self_params. Drives synthetic M1 OHLC through the *same* modules LIVE uses:

  perceive → compose_open → stop_take → settle → bracket_learn → neural trade_pnl

Not a profitability claim — measures whether internal state actually moves
(aggression, κ/sl multipliers, streaks, neural trade labels, edge HOLDs).
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import bracket_learn
from app import learning_generation
from app import self_params
from app import unified_cycle
from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, Store
from app.math_rules import ohlc_range_distance, stop_take


EPIC = 'GOLD'
TICK = 0.1
SPREAD = 0.30
MIN_DIST = 0.50
SIZE = 0.07
# Synthetic £ scale: size × ticks × £/tick (Capital GOLD-ish demo unit).
TICK_VALUE = 1.0
MILESTONES = (0, 10, 25, 50, 75, 100)


@dataclass
class Snapshot:
    n_trades: int
    cumulative_pnl: float
    win_rate: float | None
    wins: int
    losses: int
    scratches: int
    aggression: float
    loss_streak: int
    win_streak: int
    kappa_mult: float
    sl_mult: float
    learn_lr: float
    learn_win_repeats: int
    learn_loss_repeats: int
    scratch_pnl_abs: float
    neurons: int
    trade_labeled: int
    trade_correct: int
    minute_labeled: int
    minute_acc: float | None
    learning_generation: int
    opens: int
    settlements: int
    edge_holds: int
    hold_skips: int
    rolling20_pnl: float | None
    rolling20_win_rate: float | None
    last_bracket_reason: str | None


@dataclass
class SimResult:
    seed: int
    n_target: int
    n_trades: int
    n_bars: int
    started_at: str
    finished_at: str
    zero_state: dict
    milestones: list
    trades: list = field(default_factory=list)
    series: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _cash(signed_price_move: float) -> float:
    ticks = signed_price_move / TICK
    return float(SIZE * ticks * TICK_VALUE)


def _gen_ohlc(n_bars: int, seed: int):
    """Mixed regimes: drift up, chop, drift down — not pure random walk."""
    rng = np.random.default_rng(seed)
    px = 2000.0
    t0 = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n_bars):
        phase = (i // 180) % 3
        if phase == 0:
            drift = 0.045
            vol = 0.55
        elif phase == 1:
            drift = -0.01
            vol = 0.90
        else:
            drift = -0.040
            vol = 0.60
        # Mild mean reversion toward local mid of last 40 bars.
        if len(rows) >= 40:
            local = np.mean([r['close'] for r in rows[-40:]])
            drift += 0.08 * (local - px) / max(1.0, abs(local))
        o = px
        ret = drift + float(rng.normal(0, vol))
        c = max(50.0, o + ret)
        wiggle = abs(float(rng.normal(0, vol * 0.7)))
        h = max(o, c) + wiggle
        l = min(o, c) - wiggle
        at = (t0 + timedelta(minutes=i)).isoformat().replace('+00:00', 'Z')
        rows.append({
            'at': at, 'open': float(o), 'high': float(h),
            'low': float(l), 'close': float(c), 'spread': SPREAD,
        })
        px = c
    return rows


def _scores_action(scores):
    if not scores:
        return 'HOLD'
    return max(('BUY', 'SELL', 'HOLD'), key=lambda a: float(scores.get(a, 0) or 0))


def _snapshot(store, brain, *, n_trades, pnls, edge_holds, hold_skips,
              wins, losses, scratches) -> Snapshot:
    cycle = unified_cycle.load_state(store)
    mult = bracket_learn.load_multipliers(store)
    sp = self_params.load(store)
    recent = pnls[-20:] if pnls else []
    r_wins = sum(1 for p in recent if p > 0)
    r_loss = sum(1 for p in recent if p < 0)
    r_n = r_wins + r_loss
    m_acc = (brain.minute_correct / brain.minute_labeled) if brain.minute_labeled else None
    w = wins + losses
    return Snapshot(
        n_trades=n_trades,
        cumulative_pnl=float(sum(pnls)) if pnls else 0.0,
        win_rate=(wins / w) if w else None,
        wins=wins, losses=losses, scratches=scratches,
        aggression=float(cycle['aggression']),
        loss_streak=int(cycle['loss_streak']),
        win_streak=int(cycle['win_streak']),
        kappa_mult=float(mult['kappa']),
        sl_mult=float(mult['sl_scale']),
        learn_lr=float(sp['learn_lr']),
        learn_win_repeats=int(sp['learn_win_repeats']),
        learn_loss_repeats=int(sp['learn_loss_repeats']),
        scratch_pnl_abs=float(sp['scratch_pnl_abs']),
        neurons=int(brain.core.n),
        trade_labeled=int(brain.trade_labeled),
        trade_correct=int(brain.trade_correct),
        minute_labeled=int(brain.minute_labeled),
        minute_acc=float(m_acc) if m_acc is not None else None,
        learning_generation=int(learning_generation.current(store)),
        opens=int(cycle['opens']),
        settlements=int(cycle['settlements']),
        edge_holds=int(edge_holds),
        hold_skips=int(hold_skips),
        rolling20_pnl=float(sum(recent)) if recent else None,
        rolling20_win_rate=(r_wins / r_n) if r_n else None,
        last_bracket_reason=store.get('bracket_learn_reason'),
    )


def _close_trade(store, brain, policy, *, direction, entry, stop_d, profit_d,
                 exit_px, exit_reason, transition, scores, deal_id):
    sign = 1 if direction == 'BUY' else -1
    move = sign * (exit_px - entry)
    pnl = _cash(move)
    # Cap to bracket cash if SL/TP exact; path noise already in exit_px.
    if exit_reason == 'sl':
        pnl = -abs(_cash(stop_d))
    elif exit_reason == 'tp':
        pnl = abs(_cash(profit_d))

    cycle = unified_cycle.settle(store, pnl=pnl, action=direction, exit_reason=exit_reason)
    zone_k = store.get('bracket_kappa') or 1.0
    zone_s = store.get('bracket_sl_scale') or 1.0
    path = bracket_learn.record_outcome(
        store, kappa=zone_k, sl_scale=zone_s, pnl=pnl, currency='GBP',
        deal_id=deal_id, exit_reason=exit_reason, direction=direction,
        entry=entry, exit_level=exit_px, stop_distance=stop_d,
        adverse_path=abs(min(0.0, move)), volatility=abs(move) * 1.2,
        costs=SPREAD)
    promoted = bracket_learn.evolve_from_history(store)

    # Neural teacher — same rules as engine: win reinforce, else counterfactual HOLD-ish.
    label = None
    mode = 'skip'
    if transition is not None:
        if pnl > float(self_params.load(store)['scratch_pnl_abs']):
            label = 0 if direction == 'BUY' else 1
            mode = 'reinforce_win'
        elif pnl < -float(self_params.load(store)['scratch_pnl_abs']):
            # Prefer HOLD after loss (not opposite churn) — matches flip-after-loss spirit.
            label = 2
            mode = 'loss_hold'
        else:
            mode = 'scratch_skip'
        sp = self_params.load(store)
        if label is not None:
            repeats = (int(sp['learn_win_repeats']) if mode == 'reinforce_win'
                       else int(sp['learn_loss_repeats']))
            for i in range(repeats):
                brain.learn_from_trade(
                    transition, label, lr=float(sp['learn_lr']),
                    won=pnl > 0, count_sample=(i == 0))
            if repeats > 0:
                learning_generation.bump(store, 'closed_trade_weight_update')
                brain.model_version = int(getattr(brain, 'model_version', 0) or 0) + 1

    self_params.evolve_from_feedback(store, broker_pnl=pnl, commit=False)
    from app.profit_learning import ProfitLearning
    pl = ProfitLearning(store)
    pl.record_closed(
        deal_id=deal_id, epic=EPIC, direction=direction, entry=entry,
        exit_level=exit_px, broker_pnl=pnl, currency='GBP', spread=SPREAD,
        require_broker_pnl=True)
    self_params.maybe_commit_long_horizon(store, pl.broker_pnls())

    return {
        'deal_id': deal_id, 'direction': direction, 'entry': entry,
        'exit': exit_px, 'exit_reason': exit_reason, 'pnl': pnl,
        'aggression': cycle['aggression'],
        'loss_streak': cycle['loss_streak'], 'win_streak': cycle['win_streak'],
        'bracket_reason': (path or {}).get('reason') if isinstance(path, dict) else None,
        'promoted': bool(promoted), 'learn_mode': mode,
        'kappa_mult': bracket_learn.load_multipliers(store)['kappa'],
        'sl_mult': bracket_learn.load_multipliers(store)['sl_scale'],
        'scores': scores,
    }


def run(n_trades: int = 100, seed: int = 29, max_bars: int = 8000) -> SimResult:
    started = datetime.now(timezone.utc).isoformat(timespec='seconds')
    tmp = tempfile.TemporaryDirectory()
    data = Path(tmp.name)
    store = Store(data / 'robot.sqlite3')
    brain = AdaptiveBrain(store, data_dir=data, seed=seed)
    policy = AutoEvolution(store, data / 'policy.py')

    zero = asdict(_snapshot(
        store, brain, n_trades=0, pnls=[], edge_holds=0, hold_skips=0,
        wins=0, losses=0, scratches=0))

    bars = _gen_ohlc(max_bars, seed)
    # Warm candles buffer for zone_at_open
    candle_buf = []

    open_pos = None
    trades = []
    pnls = []
    series = []
    milestones = []
    edge_holds = 0
    hold_skips = 0
    wins = losses = scratches = 0
    deal_seq = 0

    # Milestone 0
    milestones.append(zero)

    for i, bar in enumerate(bars):
        if len(trades) >= n_trades:
            break
        mid = float(bar['close'])
        candle_buf.append(bar)
        if len(candle_buf) > 200:
            candle_buf = candle_buf[-200:]

        sig = brain.tick(mid, SPREAD, TICK, EPIC, dt=60., bar_time=bar['at'])
        scores = (sig or {}).get('scores') if sig else None
        if scores is None and brain.core.last_scores is not None:
            # Fallback — some ticks may return None on gaps; skip.
            pass
        if not scores:
            # Use last_signal-like from core step already done inside tick.
            # When tick returns None (e.g. first), continue.
            if open_pos is None:
                continue
            # Still manage open position on price even without new scores.
            scores = open_pos.get('scores')

        # Manage open position first.
        if open_pos is not None:
            d = open_pos['direction']
            entry = open_pos['entry']
            stop_d = open_pos['stop_d']
            profit_d = open_pos['profit_d']
            hi, lo = float(bar['high']), float(bar['low'])
            exit_px = None
            exit_reason = None
            if d == 'BUY':
                sl = entry - stop_d
                tp = entry + profit_d
                # Conservative: if both touched, SL first (worse case).
                if lo <= sl:
                    exit_px, exit_reason = sl, 'sl'
                elif hi >= tp:
                    exit_px, exit_reason = tp, 'tp'
            else:
                sl = entry + stop_d
                tp = entry - profit_d
                if hi >= sl:
                    exit_px, exit_reason = sl, 'sl'
                elif lo <= tp:
                    exit_px, exit_reason = tp, 'tp'
            # Time stop — flat after 45 bars to avoid immortal positions in chop.
            if exit_px is None and (i - open_pos['bar_i']) >= 45:
                exit_px, exit_reason = mid, 'manual'
            if exit_px is not None:
                deal_seq += 1
                row = _close_trade(
                    store, brain, policy,
                    direction=d, entry=entry, stop_d=stop_d, profit_d=profit_d,
                    exit_px=float(exit_px), exit_reason=exit_reason,
                    transition=open_pos['transition'], scores=open_pos.get('scores'),
                    deal_id=f'sim-{deal_seq}')
                trades.append(row)
                pnls.append(row['pnl'])
                band = float(self_params.load(store)['scratch_pnl_abs'])
                if abs(row['pnl']) <= band:
                    scratches += 1
                elif row['pnl'] > 0:
                    wins += 1
                else:
                    losses += 1
                snap = _snapshot(
                    store, brain, n_trades=len(trades), pnls=pnls,
                    edge_holds=edge_holds, hold_skips=hold_skips,
                    wins=wins, losses=losses, scratches=scratches)
                series.append(asdict(snap))
                if len(trades) in MILESTONES:
                    milestones.append(asdict(snap))
                open_pos = None
            continue

        # Flat — decide OPEN via unified cycle.
        if len(candle_buf) < 40:
            continue
        action = _scores_action(scores)
        if action not in ('BUY', 'SELL'):
            hold_skips += 1
            continue
        zone = bracket_learn.zone_at_open(candle_buf)
        rng = zone['range_distance'] or ohlc_range_distance(candle_buf)
        buffer = max(2 * TICK, 2 * SPREAD)
        base_sl = max(float(MIN_DIST) + buffer, float(rng or 0))
        learned = bracket_learn.load_multipliers(store)
        cycle_state = unified_cycle.load_state(store)
        edge = policy.score_edge(scores) if scores else 0.0
        pkg = unified_cycle.compose_open(
            action=action, scores=scores,
            evaluation={'action': action, 'score_edge': edge},
            zone=zone, learned=learned, cycle_state=cycle_state,
            base_sl=base_sl, max_tp=zone.get('max_tp'))
        if pkg.get('blocked') or pkg.get('action') not in ('BUY', 'SELL'):
            edge_holds += 1
            continue
        brackets = stop_take(
            mid, action, TICK, SPREAD, MIN_DIST,
            kappa=pkg['kappa'], sl_scale=pkg['sl_scale'],
            range_distance=float(rng) if rng is not None else 0)
        bracket_learn.save_params(
            store, pkg['kappa'], pkg['sl_scale'],
            reason=f'sim_open agg={pkg.get("aggression"):.3f}')
        tr = None
        if brain.core.last_transition is not None:
            tr = brain._json_transition(brain.core.last_transition)
        unified_cycle.note_open(store, action=action)
        open_pos = {
            'direction': action, 'entry': float(mid),
            'stop_d': float(brackets['stop_distance']),
            'profit_d': float(brackets['profit_distance']),
            'transition': tr, 'scores': scores, 'bar_i': i,
            'kappa': pkg['kappa'], 'sl_scale': pkg['sl_scale'],
        }

    # Force-close leftover.
    if open_pos is not None and len(trades) < n_trades:
        deal_seq += 1
        mid = float(bars[min(len(bars) - 1, open_pos['bar_i'] + 1)]['close'])
        row = _close_trade(
            store, brain, policy,
            direction=open_pos['direction'], entry=open_pos['entry'],
            stop_d=open_pos['stop_d'], profit_d=open_pos['profit_d'],
            exit_px=mid, exit_reason='manual',
            transition=open_pos['transition'], scores=open_pos.get('scores'),
            deal_id=f'sim-{deal_seq}')
        trades.append(row)
        pnls.append(row['pnl'])
        if row['pnl'] > 0:
            wins += 1
        elif row['pnl'] < 0:
            losses += 1
        else:
            scratches += 1
        snap = _snapshot(
            store, brain, n_trades=len(trades), pnls=pnls,
            edge_holds=edge_holds, hold_skips=hold_skips,
            wins=wins, losses=losses, scratches=scratches)
        series.append(asdict(snap))
        if len(trades) in MILESTONES and not any(
                m.get('n_trades') == len(trades) for m in milestones):
            milestones.append(asdict(snap))

    finished = datetime.now(timezone.utc).isoformat(timespec='seconds')
    final = series[-1] if series else zero
    first10 = pnls[:10]
    last20 = pnls[-20:] if len(pnls) >= 20 else pnls
    summary = {
        'trades_completed': len(trades),
        'bars_used': i + 1 if bars else 0,
        'cumulative_pnl': final.get('cumulative_pnl'),
        'win_rate': final.get('win_rate'),
        'aggression_start': zero['aggression'],
        'aggression_end': final.get('aggression'),
        'kappa_mult_start': zero['kappa_mult'],
        'kappa_mult_end': final.get('kappa_mult'),
        'sl_mult_start': zero['sl_mult'],
        'sl_mult_end': final.get('sl_mult'),
        'neurons_start': zero['neurons'],
        'neurons_end': final.get('neurons'),
        'trade_labeled': final.get('trade_labeled'),
        'edge_holds': edge_holds,
        'hold_skips': hold_skips,
        'first10_pnl': float(sum(first10)) if first10 else 0.0,
        'last20_pnl': float(sum(last20)) if last20 else 0.0,
        'first10_win_rate': (
            sum(1 for p in first10 if p > 0) /
            max(1, sum(1 for p in first10 if p != 0))),
        'last20_win_rate': (
            sum(1 for p in last20 if p > 0) /
            max(1, sum(1 for p in last20 if p != 0))) if last20 else None,
        'learning_generation': final.get('learning_generation'),
        'disclaimer': (
            'Synthetic OHLC + cash scale — proves self-state evolution, '
            'not Capital.com profitability.'),
    }
    # Keep temp dir alive until we copy nothing — results are in memory.
    result = SimResult(
        seed=seed, n_target=n_trades, n_trades=len(trades),
        n_bars=summary['bars_used'], started_at=started, finished_at=finished,
        zero_state=zero, milestones=milestones, trades=trades,
        series=series, summary=summary)
    tmp.cleanup()
    return result


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/opt/cursor/artifacts')
    out_dir.mkdir(parents=True, exist_ok=True)
    seeds = [29, 7, 101]
    runs = []
    for s in seeds:
        r = run(n_trades=100, seed=s, max_bars=10000)
        runs.append(asdict(r))
        print(f'seed={s} trades={r.n_trades} pnl={r.summary["cumulative_pnl"]:.2f} '
              f'agg {r.summary["aggression_start"]:.3f}→{r.summary["aggression_end"]:.3f} '
              f'κ {r.summary["kappa_mult_start"]:.3f}→{r.summary["kappa_mult_end"]:.3f} '
              f'sl {r.summary["sl_mult_start"]:.3f}→{r.summary["sl_mult_end"]:.3f} '
              f'edge_holds={r.summary["edge_holds"]}')
    primary = runs[0]
    payload = {
        'primary': primary,
        'runs': [{
            'seed': r['seed'],
            'summary': r['summary'],
            'milestones': r['milestones'],
        } for r in runs],
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    path = out_dir / 'zero_start_cycle_sim.json'
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print('wrote', path)
    return path


if __name__ == '__main__':
    main()
