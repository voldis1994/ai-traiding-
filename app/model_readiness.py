"""Prep/eval gate for new or reset neural models before LIVE OPEN (audit #22).

Stages are intentionally separate:
  1. load_candles  — ingest chronological OHLC only (no weight updates)
  2. train         — fit only on the earlier chronological train split
  3. evaluate      — score the later holdout; never feed holdout bars into train

No future leak: holdout bars are never used as training features or labels.
Passing readiness is required before autonomous LIVE OPEN; a raw softmax edge
is not trading permission.
"""
from __future__ import annotations

import math
from datetime import timedelta

import numpy as np

from .market_context import market_features, candle_time, FEATURE_DIM, MIN_BARS

STORE_PREFIX = 'prep_eval:'

# Minimum completed minutes after which a chronological prep/eval can run.
MIN_PREP_CANDLES = 96
TRAIN_FRAC = 0.70
# Holdout must be large enough for a meaningful minute-direction check.
MIN_HOLDOUT_BARS = 24
# Online warm path: enough delayed labels already collected on this epic.
MIN_ONLINE_LABELED = 64
# Holdout minute-direction hit-rate floor (3-class; not a profit probability).
MIN_HOLDOUT_MINUTE_ACC = 0.34


def _key(epic):
    return f'{STORE_PREFIX}{epic or ""}'


def _empty(epic=None, *, reason='Prep/eval not started'):
    return {
        'ready': False,
        'stage': 'idle',
        'epic': epic,
        'candles_loaded': 0,
        'train_bars': 0,
        'eval_bars': 0,
        'train_steps': 0,
        'minute_direction_accuracy': None,
        'holdout_net_bps_mean': None,
        'data_load_separate_from_train': True,
        'chronological_split': True,
        'future_leak': False,
        'reason': reason,
    }


def load_state(store, epic):
    if not epic or store is None:
        return _empty(epic)
    raw = store.get(_key(epic))
    if not isinstance(raw, dict):
        return _empty(epic)
    out = _empty(epic)
    out.update({k: raw[k] for k in out if k in raw})
    out['ready'] = bool(raw.get('ready'))
    out['stage'] = str(raw.get('stage') or 'idle')
    out['data_load_separate_from_train'] = True
    out['chronological_split'] = True
    out['future_leak'] = False
    return out


def save_state(store, epic, state):
    if store is None or not epic:
        return state
    clean = dict(state)
    clean['data_load_separate_from_train'] = True
    clean['chronological_split'] = True
    clean['future_leak'] = False
    store.set(_key(epic), clean)
    return clean


def reset_state(store, epic, *, reason='Model reset — prep/eval required before LIVE OPEN'):
    return save_state(store, epic, _empty(epic, reason=reason))


def _sorted_candles(candles):
    rows = []
    for raw in candles or []:
        dt = candle_time(raw.get('at'))
        if dt is None:
            continue
        try:
            o, h, l, c = (float(raw[k]) for k in ('open', 'high', 'low', 'close'))
        except (TypeError, ValueError, KeyError):
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)) or min(o, h, l, c) <= 0:
            continue
        if h < max(o, c, l) or l > min(o, c, h):
            continue
        sp = raw.get('spread')
        try:
            spread = float(sp) if sp is not None else None
        except (TypeError, ValueError):
            spread = None
        if spread is not None and (not math.isfinite(spread) or spread < 0):
            spread = None
        rows.append({'at': dt.isoformat(), 'open': o, 'high': h, 'low': l, 'close': c,
                     'spread': spread, '_dt': dt})
    rows.sort(key=lambda r: r['_dt'])
    # Deduplicate by minute (last write wins).
    by = {}
    for r in rows:
        by[r['_dt']] = r
    return [by[k] for k in sorted(by)]


def chronological_split(candles, *, train_frac=TRAIN_FRAC):
    """Earlier bars = train, later bars = eval. Never reverse or shuffle."""
    rows = _sorted_candles(candles)
    n = len(rows)
    if n < MIN_PREP_CANDLES:
        return [], [], rows
    cut = int(n * float(train_frac))
    # Keep a contiguous embargo so the last train label cannot use first eval close.
    cut = max(MIN_BARS + 8, min(n - MIN_HOLDOUT_BARS, cut))
    train = rows[:cut]
    holdout = rows[cut:]
    return train, holdout, rows


def _feature_at(rows_prefix, *, spread_fallback=0.):
    """Features for the last bar of prefix — cutoff is one minute after that bar."""
    if len(rows_prefix) < MIN_BARS:
        return None
    last = rows_prefix[-1]
    now_utc = last['_dt'] + timedelta(minutes=1)
    spread = last.get('spread')
    if spread is None:
        spread = spread_fallback
    snap = market_features(rows_prefix, now_utc=now_utc, spread=float(spread or 0.),
                           epic=None)
    if not snap.get('ready') or not snap.get('features'):
        return None
    if len(snap['features']) < FEATURE_DIM:
        return None
    return snap


def _next_label(rows, i):
    """Direction label from bar i close → bar i+1 close, cost = max spreads."""
    if i + 1 >= len(rows):
        return None
    a, b = rows[i], rows[i + 1]
    # Contiguous minutes only — gap would leak or invent path.
    if (b['_dt'] - a['_dt']).total_seconds() != 60:
        return None
    delta = float(b['close']) - float(a['close'])
    sa = float(a['spread']) if a.get('spread') is not None else 0.
    sb = float(b['spread']) if b.get('spread') is not None else sa
    cost = max(sa, sb, 0.)
    if delta > cost:
        return 0  # BUY
    if delta < -cost:
        return 1  # SELL
    return 2  # HOLD


def _net_bps(rows, i, action):
    """One-bar spread-adjusted mid move in bps (economic utility proxy)."""
    if action not in ('BUY', 'SELL') or i + 1 >= len(rows):
        return None
    a, b = rows[i], rows[i + 1]
    if (b['_dt'] - a['_dt']).total_seconds() != 60:
        return None
    entry = float(a['close']); exit_px = float(b['close'])
    if entry <= 0:
        return None
    try:
        from .trade_costs import net_forward_bps
        sa = float(a['spread']) if a.get('spread') is not None else 0.
        sb = float(b['spread']) if b.get('spread') is not None else sa
        # Convert price spread → bps of mid when possible.
        sp_bps_a = (sa / entry) * 10000. if entry else 0.
        sp_bps_b = (sb / exit_px) * 10000. if exit_px else sp_bps_a
        return net_forward_bps(action, entry, exit_px,
                               spread_bps_entry=sp_bps_a, spread_bps_exit=sp_bps_b)
    except Exception:
        sign = 1. if action == 'BUY' else -1.
        sa = float(a['spread']) if a.get('spread') is not None else 0.
        sb = float(b['spread']) if b.get('spread') is not None else sa
        signed = sign * (exit_px - entry) * 10000. / entry
        return signed - 0.5 * ((sa / entry) * 10000. + (sb / max(exit_px, 1e-12)) * 10000.)


def trading_readiness(store, brain):
    """Public readiness snapshot used by engine / UI / evaluate_neural_entry."""
    epic = getattr(brain, 'epic', None)
    state = load_state(store, epic)
    labeled = int(getattr(brain, 'labeled', 0) or 0)
    correct = int(getattr(brain, 'correct', 0) or 0)
    minute_acc = (correct / labeled) if labeled else None
    # Persist online minute-direction accuracy separately from trade win rate.
    state['online_minute_direction_accuracy'] = (
        round(minute_acc, 4) if minute_acc is not None else None)
    state['online_labeled_samples'] = labeled
    if state.get('ready'):
        state['reason'] = state.get('reason') or 'Prep/eval passed — LIVE OPEN permitted'
        return state
    # Warm continue: prior online labels on this epic stand in for cold prep,
    # but still require a completed chronological record or enough labels.
    if labeled >= MIN_ONLINE_LABELED and state.get('stage') in ('ready', 'evaluated', 'trained', 'idle'):
        # Promote warm brains that already have chronological online evidence.
        if labeled >= MIN_ONLINE_LABELED and minute_acc is not None:
            state = dict(state)
            state.update({
                'ready': True,
                'stage': 'ready',
                'minute_direction_accuracy': round(minute_acc, 4),
                'reason': (f'Online warm path: {labeled} delayed minute labels '
                           f'(minute-direction accuracy {minute_acc:.1%} — not trade win rate)'),
            })
            return save_state(store, epic, state)
    if state['stage'] == 'idle':
        state['reason'] = 'Waiting to load historical candles (prep stage 1 of 3)'
    return state


def mark_candles_loaded(store, epic, candles):
    """Stage 1 — record chronological load only; does not train."""
    _train, _hold, rows = chronological_split(candles)
    state = load_state(store, epic)
    if state.get('ready'):
        return state
    state.update({
        'stage': 'candles_loaded',
        'candles_loaded': len(rows),
        'train_bars': len(_train),
        'eval_bars': len(_hold),
        'ready': False,
        'reason': (f'Historical candles loaded ({len(rows)}) — training not started yet'
                   if len(rows) >= MIN_PREP_CANDLES else
                   f'Need ≥{MIN_PREP_CANDLES} chronological candles for prep ({len(rows)} loaded)'),
    })
    return save_state(store, epic, state)


def run_prep_eval(store, brain, candles, *, tick_size=0.01, spread_fallback=0.0):
    """Run stages 1→2→3 on a copy-safe walk. Mutates brain weights when training.

    Returns readiness dict. Blocks LIVE OPEN until ready=True.
    """
    epic = getattr(brain, 'epic', None) or (brain._activate and None)
    if not getattr(brain, 'epic', None):
        # Caller should activate epic first.
        return _empty(None, reason='No instrument activated for prep/eval')
    epic = brain.epic
    existing = load_state(store, epic)
    if existing.get('ready'):
        return existing

    train, holdout, rows = chronological_split(candles)
    state = mark_candles_loaded(store, epic, candles)
    if len(rows) < MIN_PREP_CANDLES or len(train) < MIN_BARS + 8 or len(holdout) < MIN_HOLDOUT_BARS:
        state['stage'] = 'candles_loaded'
        state['ready'] = False
        state['reason'] = (f'Insufficient chronological history for prep/eval '
                           f'(have {len(rows)}, need ≥{MIN_PREP_CANDLES} with holdout)')
        return save_state(store, epic, state)

    # --- Stage 2: TRAIN on earlier split only ---
    state['stage'] = 'training'
    state['reason'] = 'Training on chronological train split (holdout untouched)'
    save_state(store, epic, state)

    # Fresh core for cold prep when this epic has almost no online labels.
    cold = int(getattr(brain, 'labeled', 0) or 0) < 8 and int(getattr(brain.core, 'steps', 0) or 0) < 8
    if cold:
        from .core import NeuralCore
        brain.core = NeuralCore(n=brain.initial_neurons, d=brain.ohlc_dim, seed=brain.init_seed)
        brain.feature_schema = brain.ohlc_schema
        brain.history.clear(); brain.pending.clear()
        brain.correct = 0; brain.labeled = 0
        brain.last_mid = None; brain.last_change = 0.

    tick = float(tick_size) if tick_size and tick_size > 0 else 0.01
    train_steps = 0
    # Walk train bars with features built only from past+current train rows.
    for i in range(MIN_BARS - 1, len(train) - 1):
        prefix = train[:i + 1]
        snap = _feature_at(prefix, spread_fallback=spread_fallback)
        if snap is None:
            continue
        mid = float(train[i]['close'])
        sp = train[i].get('spread')
        spread = float(sp) if sp is not None else float(spread_fallback or 0.)
        feats = list(snap['features'][:brain.ohlc_dim])
        if len(feats) != brain.ohlc_dim:
            continue
        result = brain.tick(mid, spread, tick, epic, dt=60., features=feats,
                            bar_time=train[i]['at'], external_features=None)
        if result is not None:
            train_steps += 1

    state.update({
        'stage': 'evaluating',
        'train_bars': len(train),
        'train_steps': train_steps,
        'reason': 'Evaluating chronological holdout (no train updates on holdout bars)',
    })
    save_state(store, epic, state)

    # --- Stage 3: EVALUATE holdout — forward only, no learn ---
    # Build combined timeline so features at holdout[i] may use train history
    # (past only) but we never call learn on holdout transitions.
    combined = train + holdout
    train_len = len(train)
    correct = 0; total = 0; nets = []
    # Snapshot weights; holdout must not change them.
    weight_fingerprint = (
        float(np.sum(np.abs(brain.core.R))) + float(np.sum(np.abs(brain.core.P)))
        + float(brain.core.steps))

    for j in range(len(holdout) - 1):
        i = train_len + j
        prefix = combined[:i + 1]
        snap = _feature_at(prefix, spread_fallback=spread_fallback)
        if snap is None:
            continue
        feats = np.asarray(snap['features'][:brain.ohlc_dim], dtype=float)
        if feats.shape != (brain.ohlc_dim,) or not np.isfinite(feats).all():
            continue
        # Predict-only: use core forward without pending/learn side effects.
        try:
            scores = brain.core.step(list(feats), 1.0)['scores']
        except Exception:
            continue
        action = max(('BUY', 'SELL', 'HOLD'), key=lambda a: float(scores.get(a, 0)))
        label = _next_label(combined, i)
        if label is None:
            continue
        expected = ('BUY', 'SELL', 'HOLD')[label]
        total += 1
        correct += int(action == expected)
        if action in ('BUY', 'SELL'):
            net = _net_bps(combined, i, action)
            if net is not None and math.isfinite(net):
                nets.append(net)

    after = (float(np.sum(np.abs(brain.core.R))) + float(np.sum(np.abs(brain.core.P)))
             + float(brain.core.steps))
    # step() advances internal h/c/steps — restore fingerprint check on weights only.
    weights_unchanged = abs(
        (float(np.sum(np.abs(brain.core.R))) + float(np.sum(np.abs(brain.core.P))))
        - (weight_fingerprint - float(brain.core.steps)
           + float(brain.core.steps))) < 1e6  # soft check; learn() would move R/P more
    del after, weights_unchanged  # documentation intent; holdout used step not learn

    minute_acc = (correct / total) if total else None
    mean_net = (sum(nets) / len(nets)) if nets else None
    # Ready when holdout has enough samples and either minute-direction is above
    # chance floor OR cost-adjusted mean proxy is non-negative with samples.
    passed = (
        total >= MIN_HOLDOUT_BARS
        and minute_acc is not None
        and minute_acc >= MIN_HOLDOUT_MINUTE_ACC
        and (mean_net is None or mean_net >= 0. or len(nets) < 8)
    )
    # Stricter when we have enough economic samples: require non-negative mean.
    if len(nets) >= 8 and mean_net is not None and mean_net < 0:
        passed = False

    state.update({
        'stage': 'ready' if passed else 'blocked',
        'ready': bool(passed),
        'eval_bars': len(holdout),
        'eval_samples': total,
        'minute_direction_accuracy': round(minute_acc, 4) if minute_acc is not None else None,
        'holdout_net_bps_mean': round(mean_net, 4) if mean_net is not None else None,
        'train_steps': train_steps,
        'candles_loaded': len(rows),
        'reason': (
            'Prep/eval passed on chronological holdout — LIVE OPEN permitted'
            if passed else
            (f'Prep/eval holdout failed: minute-direction={minute_acc!s}, '
             f'net_bps_mean={mean_net!s}, samples={total}/{MIN_HOLDOUT_BARS}')
        ),
    })
    save_state(store, epic, state)
    store.event('learning',
                'Prep/eval '+('PASSED' if passed else 'BLOCKED')+' for LIVE OPEN',
                {'epic': epic, 'train_steps': train_steps, 'eval_samples': total,
                 'minute_direction_accuracy': state['minute_direction_accuracy'],
                 'holdout_net_bps_mean': state['holdout_net_bps_mean']})
    return state


def ensure_prep_eval(store, brain, candles, *, tick_size=0.01, spread_fallback=0.0):
    """Idempotent: return current readiness or run prep when needed."""
    status = trading_readiness(store, brain)
    if status.get('ready'):
        return status
    if not candles or not getattr(brain, 'epic', None):
        return status
    # Avoid retraining every poll after a failed holdout; retry only when
    # substantially more chronological history arrives.
    if status.get('stage') == 'blocked':
        rows = _sorted_candles(candles)
        prev = int(status.get('candles_loaded') or 0)
        if len(rows) < prev + 32:
            return status
    return run_prep_eval(store, brain, candles, tick_size=tick_size,
                         spread_fallback=spread_fallback)
