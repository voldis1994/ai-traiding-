#!/usr/bin/env python3
"""Adversarial full-cycle stress — try to break quantization, HOLD-shadow, learn.

Runs poison inputs + multi-seed 1000-trade sims. Exit non-zero on invariant fail.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import hold_shadow, self_params, unified_cycle
from app.engine import Store
from scripts.zero_start_cycle_sim import run


def _two_decimals(x: float) -> bool:
    if x is None or not math.isfinite(x):
        return False
    return abs(round(x, 2) - x) < 1e-9 and abs(x * 100 - round(x * 100)) < 1e-6


def poison_quantization() -> list[str]:
    fails = []
    # Compare against the live quantizers — only assert 2dp + rails + non-finite defaults.
    for raw in (0.065, 0.064, 0.086, 0.123, 0.029, 0.121, 0.035, 0.099):
        got = unified_cycle.quantize_entry_edge(raw)
        if not _two_decimals(got):
            fails.append(f'entry_edge not 2dp: {raw!r}→{got!r}')
        if got < unified_cycle.ENTRY_EDGE_MIN - 1e-9 or got > unified_cycle.ENTRY_EDGE_MAX + 1e-9:
            fails.append(f'entry_edge rails: {raw!r}→{got}')
        # Must equal its own re-quantize (idempotent)
        if abs(got - unified_cycle.quantize_entry_edge(got)) > 1e-9:
            fails.append(f'entry_edge not idempotent {got}')
    for raw in (None, float('nan'), float('inf'), float('-inf')):
        got = unified_cycle.quantize_entry_edge(raw)
        if abs(got - unified_cycle.ENTRY_EDGE_DEFAULT) > 1e-9:
            fails.append(f'entry_edge default fail {raw!r}→{got}')

    for raw in (0.294, 0.123, 0.286, 0.001, 0.999, 0.285, 0.275):
        got = self_params.quantize_learn_lr(raw)
        if not _two_decimals(got):
            fails.append(f'learn_lr not 2dp: {raw!r}→{got!r}')
        lo, hi = self_params.BOUNDS['learn_lr']
        if got < lo - 1e-9 or got > hi + 1e-9:
            fails.append(f'learn_lr rails: {raw!r}→{got}')
        if abs(got - self_params.quantize_learn_lr(got)) > 1e-9:
            fails.append(f'learn_lr not idempotent {got}')
    for raw in (None, float('nan'), float('inf')):
        got = self_params.quantize_learn_lr(raw)
        if abs(got - float(self_params.DEFAULTS['learn_lr'])) > 1e-9:
            fails.append(f'learn_lr default {raw!r}→{got}')
    return fails


def poison_store_evolve() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        self_params.save(store, {**self_params.DEFAULTS, 'learn_lr': 0.12345},
                         reason='poison')
        lr = self_params.get(store, 'learn_lr')
        if not _two_decimals(lr) or abs(lr - 0.12) > 1e-9:
            fails.append(f'save poison learn_lr stayed {lr}')
        # Spam evolve — must stay on 0.01 grid
        for pnl in [-2.0, -1.0, 0.5, -3.0, 1.0, -0.5, 2.0] * 20:
            self_params.evolve_from_feedback(store, broker_pnl=pnl)
            lr = self_params.load(store)['learn_lr']
            if not _two_decimals(lr):
                fails.append(f'evolve drift learn_lr={lr}')
                break
            lo, hi = self_params.BOUNDS['learn_lr']
            if lr < lo - 1e-9 or lr > hi + 1e-9:
                fails.append(f'learn_lr out of rails {lr}')
                break
        # Floor poison through set_calibrated_floor
        for v in (0.065, 0.086, 0.0311, 0.1199, -1.0, 9.9):
            meta = unified_cycle.set_calibrated_floor(store, v)
            f = float(meta['floor'])
            if not _two_decimals(f):
                fails.append(f'calibrated floor not 2dp {v}→{f}')
            if f < unified_cycle.ENTRY_EDGE_MIN - 1e-9 or f > unified_cycle.ENTRY_EDGE_MAX + 1e-9:
                fails.append(f'floor rails {v}→{f}')
            live = float(unified_cycle.load_state(store)['entry_edge_floor'])
            if abs(live - f) > 1e-9:
                fails.append(f'store floor mismatch meta={f} live={live}')
    return fails


def poison_hold_shadow() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        tr = {'z': [0.1] * 8, 'h': [0.0] * 4, 'c': [0.0] * 4,
              'dt': 60., 'neuron_ids': list(range(4))}
        # Empty bars — must not crash / teach
        hold_shadow.enqueue(
            store, proposal='BUY', reason='x', bar_time='b0', epic='GOLD',
            entry=100., spread=0.2, stop_distance=1., profit_distance=1.,
            tick=0.1, min_distance=0.2, transition=tr)
        outs = hold_shadow.resolve_ready(
            store, candles_after=lambda *_: [], learn_callback=None)
        if outs:
            fails.append('empty bars should keep pending')
        if len(hold_shadow.load_pending(store)) != 1:
            fails.append('pending lost on empty path')

        # Flat path under horizon — still waiting
        flat = [{'mid': 100., 'high': 100.02, 'low': 99.98, 'spread': 0.2,
                 'at': f'b{i}'} for i in range(5)]
        outs = hold_shadow.resolve_ready(
            store, candles_after=lambda *_: flat, learn_callback=None,
            horizon=15)
        if outs:
            fails.append('flat short path should not resolve')

        # Sudden TP on few bars — must teach immediately
        taught = []

        def learn(label, transition, mode, detail):
            taught.append(mode)

        down = []
        px = 100.
        for i in range(4):
            px -= 0.7
            down.append({'mid': px, 'high': px + 0.05, 'low': px - 0.05,
                         'spread': 0.2, 'at': f'd{i}'})
        outs = hold_shadow.resolve_ready(
            store, candles_after=lambda *_: down, learn_callback=learn,
            horizon=15)
        if not outs or not outs[0].get('taught'):
            fails.append('decisive TP path did not teach immediately')
        if outs and outs[0].get('path_bars', 99) > 4:
            fails.append(f'path_bars too long {outs[0].get("path_bars")}')
        if not taught:
            fails.append('learn_callback not fired')

        # Bad proposal / missing transition — enqueue None
        bad = hold_shadow.enqueue(
            store, proposal='HOLD', reason='x', bar_time='z', epic='GOLD',
            entry=100., spread=0.2, stop_distance=1., profit_distance=1.,
            tick=0.1, min_distance=0.2, transition=tr)
        if bad is not None:
            fails.append('HOLD proposal should not enqueue')
    return fails


def sim_invariants(summary: dict, seed: int) -> list[str]:
    fails = []
    floor_s = summary.get('entry_edge_floor_start')
    floor_e = summary.get('entry_edge_floor_end')
    for name, v in (('start', floor_s), ('end', floor_e)):
        if v is None or not _two_decimals(float(v)):
            fails.append(f'seed{seed} floor_{name} not 2dp: {v}')
        elif float(v) < 0.03 - 1e-9 or float(v) > 0.12 + 1e-9:
            fails.append(f'seed{seed} floor_{name} rails: {v}')
    lr = summary.get('learn_lr')
    if lr is not None and not _two_decimals(float(lr)):
        fails.append(f'seed{seed} learn_lr not 2dp: {lr}')
    done = int(summary.get('trades_completed') or summary.get('n_trades') or 0)
    if done < 900:
        fails.append(f'seed{seed} incomplete trades={done}')
    return fails


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/opt/cursor/artifacts')
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'poison': {},
        'sims': [],
        'fails': [],
    }
    print('=== poison quantization ===')
    f = poison_quantization()
    report['poison']['quantization'] = f
    report['fails'].extend(f)
    print('ok' if not f else f'FAIL {f}')

    print('=== poison store/evolve ===')
    f = poison_store_evolve()
    report['poison']['store_evolve'] = f
    report['fails'].extend(f)
    print('ok' if not f else f'FAIL {f}')

    print('=== poison hold_shadow ===')
    f = poison_hold_shadow()
    report['poison']['hold_shadow'] = f
    report['fails'].extend(f)
    print('ok' if not f else f'FAIL {f}')

    # Full system sims — more seeds, try to break consistency
    seeds = [29, 7, 101, 404, 777, 1337]
    mode_kw = dict(couple_weights=True, policy_live=True, freeze_edge_floor=False)
    marks = (0, 100, 500, 1000)
    print('=== full-cycle 1000×6 seeds ===')
    for s in seeds:
        try:
            r = run(n_trades=1000, seed=s, max_bars=80000, milestones=marks, **mode_kw)
            summary = dict(r.summary)
            final_m = (r.milestones or [{}])[-1]
            summary['learn_lr'] = final_m.get('learn_lr')
            summary['n_trades'] = final_m.get('n_trades') or summary.get('trades_completed')
            # Scan milestones for lr/floor 2dp
            mil_fails = []
            for m in r.milestones or []:
                for key in ('learn_lr', 'entry_edge_floor'):
                    v = m.get(key)
                    if v is None:
                        continue
                    if not _two_decimals(float(v)):
                        mil_fails.append(f'seed{s} n={m.get("n_trades")} {key}={v}')
            inv = sim_invariants(summary, s) + mil_fails
            report['fails'].extend(inv)
            row = {
                'seed': s,
                'pnl': summary.get('cumulative_pnl'),
                'wr': summary.get('win_rate'),
                'floor_end': summary.get('entry_edge_floor_end'),
                'learn_lr': summary.get('learn_lr'),
                'hs_taught': summary.get('hold_shadow_taught'),
                'hs_opp_tp': summary.get('hold_shadow_opp_tp'),
                'last100': summary.get('last100_pnl'),
                'fails': inv,
            }
            report['sims'].append(row)
            status = 'OK' if not inv else 'FAIL'
            print(
                f'{status} seed={s} pnl={row["pnl"]:.2f} wr={row["wr"]:.3f} '
                f'floor={row["floor_end"]:.2f} lr={row["learn_lr"]} '
                f'hs={row["hs_taught"]} last100={row["last100"]:.2f}'
            )
        except Exception as exc:
            msg = f'seed{s} crashed: {type(exc).__name__}: {exc}'
            report['fails'].append(msg)
            report['sims'].append({'seed': s, 'error': msg, 'trace': traceback.format_exc()})
            print('CRASH', msg)

    path = out_dir / 'break_full_cycle_stress.json'
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('wrote', path)
    if report['fails']:
        print('BREAKS FOUND:', len(report['fails']))
        for x in report['fails'][:30]:
            print(' -', x)
        return 1
    print('NO BREAKS — invariants held')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
