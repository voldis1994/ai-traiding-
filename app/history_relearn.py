"""Offline relearn from saved trades + candles — corrected teachers, no mid-trade M1.

Uses closed deals, OHLC paths and structure memory to rebuild:
  1. structure library (wins + losses)
  2. profit_learning history modes (structure HOLD / gave-then-lost)
  3. LIVE neural weights via trade-PnL only on flat entry transitions

Also merges many ``NEURAL_AI_LEARNING_BACKUP_*`` / client sqlite dumps into one
corpus (dedupe trades + candles) so ~1000 historical closes become one school.

Entry transitions are rebuilt with ``h=c=0`` (flat OPEN) and z from OHLC
features at the entry bar. Missing fusion/external channels are zero-filled
and recorded in the report — better than replaying poisoned mid-trade M1.
"""
from __future__ import annotations

import math
import re
import shutil
import sqlite3
from pathlib import Path

from . import structure_shadow as ss
from .market_context import FEATURE_DIM, candle_time, market_features
from .profit_learning import label_from_profit, path_excursion, ProfitLearning

BACKUP_DIR_RE = re.compile(r'^NEURAL_AI_LEARNING_BACKUP_', re.I)
CLIENT_DIR_RE = re.compile(r'^client_\d+$')
# Multi-week merges need far more than a live 120-bar window window.
CANDLE_LOAD_N = 200_000


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
        'FROM trades WHERE closed IS NOT NULL ORDER BY opened').fetchall()
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


def _trade_merge_key(row: dict) -> tuple | None:
    """Stable identity across backups: deal_id, else epic+path fingerprint."""
    did = str(row.get('deal_id') or '').strip()
    if did:
        return ('id', did)
    epic = str(row.get('epic') or '').strip()
    opened = str(row.get('opened') or '').strip()
    closed = str(row.get('closed') or '').strip()
    direction = str(row.get('direction') or '').strip()
    if not (epic and opened and closed and direction):
        return None
    return ('ep', epic, opened, closed, direction)


def load_candles(store, epic, *, n=CANDLE_LOAD_N):
    """All saved minutes — live dumps use MINUTE/QUOTE, not only HISTORICAL."""
    return list(store.candles(epic, n=n, historical_only=False) or [])


def discover_sqlite_dumps(root: Path, *, extra: list | None = None) -> list:
    """Find robot.sqlite3 under LEARNING_BACKUP_* and data/accounts/client_*.

    ``extra`` paths are always included (explicit operator dumps).
    """
    root = Path(root).expanduser().resolve()
    found = []
    seen = set()

    def _add(p: Path):
        try:
            rp = p.expanduser().resolve()
        except OSError:
            return
        if not rp.is_file():
            return
        key = str(rp)
        if key in seen:
            return
        seen.add(key)
        found.append(rp)

    for p in extra or []:
        _add(Path(p))
    if not root.is_dir():
        return found
    # Timestamped learning wipes beside the project root / data parent.
    for child in sorted(root.iterdir()):
        if child.is_dir() and BACKUP_DIR_RE.match(child.name):
            for hit in child.rglob('robot.sqlite3'):
                _add(hit)
    # Live client journals.
    data = root / 'data' if (root / 'data').is_dir() else root
    accounts = data / 'accounts'
    if accounts.is_dir():
        for folder in sorted(accounts.iterdir()):
            if folder.is_dir() and CLIENT_DIR_RE.match(folder.name):
                _add(folder / 'robot.sqlite3')
    # Legacy single-tenant layout.
    _add(data / 'robot.sqlite3')
    return found


def _trade_quality(row: dict) -> tuple:
    """Higher is better when the same deal_id appears in several backups."""
    src = str(row.get('pnl_source') or '')
    broker = 2 if src == 'broker' else (1 if row.get('pnl') is not None else 0)
    closed = 1 if row.get('closed') else 0
    spread_ok = 1 if _finite(row.get('entry')) is not None else 0
    return (broker, closed, spread_ok, str(row.get('closed') or ''))


def _open_src_db(db_path: Path):
    """Open backup sqlite read-only; ``immutable=1`` salvages torn WAL copies."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _read_src_trades(db_path: Path) -> list:
    try:
        con = _open_src_db(db_path)
    except sqlite3.Error:
        return []
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(trades)')}
        if 'deal_id' not in cols:
            return []
        want = ['deal_id', 'epic', 'size', 'direction', 'opened', 'closed',
                'entry', 'exit', 'pnl']
        optional = []
        if 'pnl_currency' in cols:
            optional.append('pnl_currency')
        if 'pnl_source' in cols:
            optional.append('pnl_source')
        sel = want + optional
        try:
            rows = con.execute(
                f"SELECT {','.join(sel)} FROM trades").fetchall()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            d = dict(zip(sel, r))
            if d.get('direction') not in ('BUY', 'SELL'):
                continue
            if _finite(d.get('entry')) is None or _finite(d.get('exit')) is None:
                continue
            if _finite(d.get('pnl')) is None:
                continue
            if not d.get('closed'):
                continue
            out.append(d)
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _read_src_candles(db_path: Path) -> list:
    try:
        con = _open_src_db(db_path)
    except sqlite3.Error:
        return []
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(candles)')}
        need = {'epic', 'at', 'open', 'high', 'low', 'close'}
        if not need.issubset(cols):
            return []
        has_src = 'source' in cols
        has_sp = 'spread' in cols
        q = 'SELECT epic,at,open,high,low,close'
        q += ',source' if has_src else ",'MINUTE' AS source"
        q += ',spread' if has_sp else ',NULL AS spread'
        q += ' FROM candles ORDER BY rowid'
        out = []
        try:
            cur = con.execute(q)
            while True:
                try:
                    row = cur.fetchone()
                except sqlite3.Error:
                    break
                if row is None:
                    break
                out.append(dict(zip(
                    ('epic', 'at', 'open', 'high', 'low', 'close', 'source', 'spread'),
                    row)))
        except sqlite3.Error:
            pass
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def merge_sqlite_dumps(dest_store, sources: list) -> dict:
    """Union trades + candles from many dumps into ``dest_store`` (deduped).

    Same ``deal_id`` keeps the highest-quality row (broker PnL preferred).
    Candles keyed by ``(epic, at)``. Does not copy poisoned neural state.
    """
    sources = [Path(p) for p in sources]
    by_key = {}
    candle_n = 0
    per_src = []
    for src in sources:
        trades = _read_src_trades(src)
        kept = 0
        for t in trades:
            key = _trade_merge_key(t)
            if key is None:
                continue
            prev = by_key.get(key)
            if prev is None or _trade_quality(t) >= _trade_quality(prev):
                t = dict(t)
                t['_src'] = str(src)
                # Synthetic deal_id for path-only rows so PRIMARY KEY insert works.
                if not str(t.get('deal_id') or '').strip():
                    t['deal_id'] = 'path:' + '|'.join(str(x) for x in key[1:])
                by_key[key] = t
                kept += 1
        candles = _read_src_candles(src)
        inserted = 0
        for c in candles:
            try:
                cur = dest_store.db.execute(
                    'INSERT OR IGNORE INTO candles(epic,at,open,high,low,close,source,spread) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (c.get('epic'), c.get('at'), c.get('open'), c.get('high'),
                     c.get('low'), c.get('close'), c.get('source') or 'MINUTE',
                     c.get('spread')))
                if int(cur.rowcount or 0) > 0:
                    inserted += 1
            except sqlite3.Error:
                continue
        dest_store.db.commit()
        candle_n += inserted
        per_src.append({
            'path': str(src), 'trades_read': len(trades),
            'trades_kept_prefer': kept, 'candles_read': len(candles),
            'candles_inserted': inserted,
        })
    # Rewrite trades table from merged map.
    dest_store.db.execute('DELETE FROM trades')
    for t in by_key.values():
        dest_store.db.execute(
            'INSERT OR REPLACE INTO trades('
            'deal_id,epic,size,direction,opened,closed,entry,exit,pnl,'
            'pnl_currency,pnl_source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (t.get('deal_id'), t.get('epic'), str(t.get('size') or ''),
             t.get('direction'), t.get('opened'), t.get('closed'),
             t.get('entry'), t.get('exit'), t.get('pnl'),
             t.get('pnl_currency'), t.get('pnl_source')))
    dest_store.db.commit()
    return {
        'sources': len(sources),
        'unique_trades': len(by_key),
        'candles_inserted_total': candle_n,
        'per_source': per_src,
    }


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


def prepare_merged_workdir(*, brain: Path, work: Path, sources: list,
                           seed_db: Path | None = None) -> dict:
    """Build an empty/seed sqlite, merge all sources, copy brain for relearn."""
    from .engine import Store
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    db_dst = work / 'robot.sqlite3'
    brain_dst = work / 'brain.npz'
    if seed_db is not None and Path(seed_db).is_file():
        shutil.copy2(seed_db, db_dst)
    elif db_dst.exists():
        db_dst.unlink()
    store = Store(db_dst)
    # Drop seed trades/candles only when we intentionally start empty; if seed
    # was copied, merge_sqlite_dumps still dedupes on top.
    merge = merge_sqlite_dumps(store, sources)
    shutil.copy2(brain, brain_dst)
    store.set('history_merge_last_v1', merge)
    return {'db': db_dst, 'brain': brain_dst, 'work': work, 'merge': merge,
            'store': store}
