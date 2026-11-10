"""Offline relearn from saved trades + candles — corrected teachers, no mid-trade M1.

Uses closed deals, OHLC paths and structure memory to rebuild:
  1. structure library (wins + losses)
  2. profit_learning history modes (structure HOLD / gave-then-lost)
  3. LIVE neural weights via trade-PnL only on flat entry transitions

Entry transitions are rebuilt with ``h=c=0`` (flat OPEN) and z from OHLC
features at the entry bar. Missing fusion/external channels are zero-filled
and recorded in the report — better than replaying poisoned mid-trade M1.
"""
from __future__ import annotations

import math
import shutil
from datetime import timedelta
from pathlib import Path

from . import structure_shadow as ss
from .market_context import FEATURE_DIM, candle_time, market_features
from .profit_learning import label_from_profit, path_excursion, ProfitLearning


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _iso(ts):
    dt = candle_time(ts)
    return dt.isoformat() if dt is not None else None


def load_trades(store):
    rows = store.db.execute(
        'SELECT deal_id,epic,direction,opened,closed,entry,exit,pnl,pnl_currency,pnl_source '
        'FROM trades ORDER BY opened').fetchall()
    cols = ['deal_id', 'epic', 'direction', 'opened', 'closed', 'entry', 'exit',
            'pnl', 'pnl_currency', 'pnl_source']
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get('direction') not in ('BUY', 'SELL'):
            continue
        if _finite(d.get('entry')) is None or _finite(d.get('exit')) is None:
            continue
        if _finite(d.get('pnl')) is None:
            continue
        out.append(d)
    return out


def load_candles(store, epic, *, n=20000):
    """All saved minutes — live dumps use MINUTE/QUOTE, not only HISTORICAL."""
    return list(store.candles(epic, n=n, historical_only=False) or [])


def path_between(candles, opened, closed):
    """Candles from entry minute through exit minute (inclusive)."""
    o = _iso(opened)
    c = _iso(closed)
    if not o or not c:
        return []
    rows = []
    for raw in candles:
        at = _iso(raw.get('at'))
        if at is None:
            continue
        if at < o:
            continue
        if at > c:
            break
        rows.append(raw)
    return rows


def pre_bars_before(candles, opened, *, limit=40):
    o = _iso(opened)
    if not o:
        return []
    pre = []
    for raw in candles:
        at = _iso(raw.get('at'))
        if at is None or at >= o:
            continue
        pre.append(raw)
    return pre[-int(limit):]


def features_at_entry(candles, *, opened, spread=0.5, epic=None):
    """OHLC feature vector at the entry bar (61 completed minutes before open)."""
    open_dt = candle_time(opened)
    if open_dt is None:
        return None
    # market_features excludes bars at/after now_utc; use open minute as cutoff
    # so the last completed bar is the minute before OPEN.
    snap = market_features(
        candles, now_utc=open_dt, spread=float(spread or 0), epic=epic)
    if not snap.get('ready') or not snap.get('features'):
        return None
    feats = list(snap['features'][:FEATURE_DIM])
    if len(feats) != FEATURE_DIM:
        return None
    return feats


def flat_transition(brain, features):
    """Decision-time transition as if OPEN from a flat recurrent state."""
    d = int(brain.core.d)
    z = list(features[:d])
    if len(z) < d:
        z = z + [0.0] * (d - len(z))
    import numpy as np
    z_arr = np.asarray(z, dtype=float)
    h0 = np.zeros(brain.core.n)
    c0 = np.zeros(brain.core.n)
    return {
        'z': z_arr.copy(),
        'h': h0.copy(),
        'c': c0.copy(),
        'dt': 60.0,
        'neuron_ids': brain.core.neuron_ids.copy(),
        'external_zero_fill': len(features) < d,
    }


def clear_structure_memory(store):
    store.set(ss.STORE_KEY, {'buckets': {}, 'updated': None})
    store.set(ss.SNAPSHOT_KEY, [])
    store.set(ss.LAST_ADVICE_KEY, None)


def relearn(*, store, brain, lr=0.28, loss_repeats=3, win_repeats=2,
            tick=0.1, default_spread=0.5) -> dict:
    """Rebuild structure memory + retrain trade-PnL from chronological closes."""
    trades = load_trades(store)
    if not trades:
        return {'ok': False, 'reason': 'no_trades', 'taught': 0}
    epic = trades[0].get('epic') or getattr(brain, 'epic', None) or 'GOLD'
    candles = load_candles(store, epic)
    if len(candles) < 80:
        return {'ok': False, 'reason': 'too_few_candles', 'candles': len(candles)}

    clear_structure_memory(store)
    pl = ProfitLearning(store)
    pl.history = []
    pl._save()

    taught = []
    skipped = []
    mode_counts = {}

    for tr in trades:
        direction = tr['direction']
        entry = float(tr['entry'])
        exit_level = float(tr['exit'])
        pnl = float(tr['pnl'])
        spread = default_spread
        path = ss.trim_path_entry_to_exit(
            path_between(candles, tr['opened'], tr['closed']),
            direction=direction, entry=entry, exit_level=exit_level,
            entry_bar_time=_iso(tr['opened']), exit_bar_time=_iso(tr['closed']))
        pre = pre_bars_before(candles, tr['opened'], limit=ss.MAX_PRE_BARS)
        # Infer exit reason from path vs a soft stop scale (median range proxy).
        stop_d = profit_d = None
        if path:
            ranges = [float(b['high']) - float(b['low']) for b in path
                      if _finite(b.get('high')) is not None]
            if ranges:
                ranges = sorted(ranges)
                stop_d = profit_d = max(tick * 2, ranges[len(ranges) // 2])
        from .bracket_learn import infer_exit_reason_from_path
        exit_reason = infer_exit_reason_from_path(
            direction=direction, entry=entry, exit_level=exit_level, bars=path,
            stop_distance=stop_d, profit_distance=profit_d, tick=tick)
        if pnl < 0:
            ss.record_loss(
                store, epic=epic, direction=direction, entry=entry,
                exit_level=exit_level, exit_reason=exit_reason or 'loss',
                broker_pnl=pnl, bars=path, pre_bars=pre, deal_id=tr['deal_id'],
                spread=spread)
        elif pnl > 0:
            ss.record_win(
                store, epic=epic, direction=direction, entry=entry,
                exit_level=exit_level, exit_reason=exit_reason or 'tp',
                broker_pnl=pnl, bars=path, pre_bars=pre, deal_id=tr['deal_id'],
                spread=spread)

        frame = ss.frame_from_ohlc(path, entry=entry, exit_level=exit_level,
                                   pre_bars=pre)
        prefer = ss.library_prefers_hold(
            store, epic=epic, structure=frame.get('structure'),
            direction=direction)
        exc = path_excursion(path, direction, entry)
        decision = label_from_profit(
            direction, pnl, entry=entry, exit_level=exit_level, spread=spread,
            bars=path or None, stop_distance=stop_d, profit_distance=profit_d,
            tick=tick, trail=False,
            structure_prefer_hold=bool(prefer.get('prefer_hold')),
            path_mfe=exc.get('mfe'), path_mae=exc.get('mae'))
        # Persist corrected ledger row.
        pl.record_closed(
            deal_id=tr['deal_id'], epic=epic, direction=direction, entry=entry,
            exit_level=exit_level, broker_pnl=pnl, currency=tr.get('pnl_currency'),
            spread=spread, bars=path or None, stop_distance=stop_d,
            profit_distance=profit_d, tick=tick, trail=False,
            structure_prefer_hold=bool(prefer.get('prefer_hold')),
            path_mfe=exc.get('mfe'), path_mae=exc.get('mae'),
            require_broker_pnl=True)
        mode = decision.get('mode') or 'none'
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        label = decision.get('label')
        if label is None:
            skipped.append({'deal_id': tr['deal_id'], 'reason': 'no_label', 'mode': mode})
            continue
        feats = features_at_entry(
            candles, opened=tr['opened'], spread=spread, epic=epic)
        if feats is None:
            skipped.append({'deal_id': tr['deal_id'], 'reason': 'no_features', 'mode': mode})
            continue
        transition = flat_transition(brain, feats)
        repeats = int(win_repeats if pnl > 0 else loss_repeats)
        moved = False
        for i in range(max(1, repeats)):
            loss = brain.learn_from_trade(
                transition, int(label), lr=lr, won=pnl > 0, count_sample=(i == 0))
            meta = getattr(brain.core, 'last_learn_meta', None) or {}
            if (loss is not None and not meta.get('restored')
                    and float(meta.get('delta_norm') or 0) > 1e-9):
                moved = True
        brain.reset_flat_state(reason='history_relearn_close')
        taught.append({
            'deal_id': tr['deal_id'], 'direction': direction, 'pnl': pnl,
            'label': int(label), 'mode': mode, 'moved': moved,
            'structure': frame.get('structure'),
            'structure_prefer_hold': bool(prefer.get('prefer_hold')),
            'path_mfe': exc.get('mfe'), 'path_bars': len(path),
            'exit_reason': exit_reason,
            'external_zero_fill': bool(transition.get('external_zero_fill')),
        })

    brain.model_version = int(getattr(brain, 'model_version', 0) or 0) + 1
    brain.save()
    store.set('history_relearn_last_v1', {
        'taught': len(taught), 'skipped': len(skipped),
        'modes': mode_counts, 'epic': epic,
        'model_version': brain.model_version,
    })
    return {
        'ok': True,
        'epic': epic,
        'trades': len(trades),
        'candles': len(candles),
        'taught': len(taught),
        'skipped': len(skipped),
        'modes': mode_counts,
        'structure': ss.summary(store),
        'rows': taught[-20:],
        'skipped_rows': skipped[:20],
        'model_version': brain.model_version,
    }


def prepare_workdir(src_db: Path, src_brain: Path, work: Path) -> dict:
    """Copy live dump into a working folder for safe relearn."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    db_dst = work / 'robot.sqlite3'
    brain_dst = work / 'brain.npz'
    shutil.copy2(src_db, db_dst)
    shutil.copy2(src_brain, brain_dst)
    return {'db': db_dst, 'brain': brain_dst, 'work': work}
