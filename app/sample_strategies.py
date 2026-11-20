"""Build strategies from trade samples — no programming, only market outcomes.

Pipeline:
  closed trades (+ OHLC path) → structure library buckets → strategy_gen cards

Uses the same framing as ``history_relearn`` / ``structure_shadow``, but does
**not** require neural relearn. Samples are the material; cards are the
strategies that soft-nudge LIVE.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from . import structure_shadow as ss
from . import history_relearn as hr
from . import strategy_gen

DEFAULT_BACKUP_ROOT = Path('/tmp/visi-dati')
STORE_FLAG = 'sample_strategies_ingest_v1'


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _is_gold(epic: str) -> bool:
    e = str(epic or '').upper()
    return 'GOLD' in e or 'XAU' in e


def library_sample_count(store) -> int:
    lib = ss.load_library(store)
    buckets = lib.get('buckets') if isinstance(lib, dict) else {}
    if not isinstance(buckets, dict):
        return 0
    n = 0
    for b in buckets.values():
        if isinstance(b, dict):
            n += int(b.get('samples') or 0)
    return n


def ingest_trades(store, trades, candles, *, epic=None, tick=0.1,
                  default_spread=0.5, clear=False) -> dict:
    """Frame each closed trade into structure library (win/loss buckets)."""
    if clear:
        hr.clear_structure_memory(store)
    if not trades:
        return {'ok': False, 'reason': 'no_trades', 'ingested': 0}
    chron = sorted(candles or [], key=lambda r: str(r.get('at') or ''))
    if len(chron) < 40:
        return {'ok': False, 'reason': 'too_few_candles', 'candles': len(chron)}

    ingested = 0
    skipped = 0
    by_structure = {}
    for tr in trades:
        direction = str(tr.get('direction') or '').upper()
        if direction not in ('BUY', 'SELL'):
            skipped += 1
            continue
        entry = _finite(tr.get('entry'))
        exit_level = _finite(tr.get('exit'))
        pnl = _finite(tr.get('pnl'))
        if entry is None or exit_level is None or pnl is None:
            skipped += 1
            continue
        ep = epic or tr.get('epic') or 'GOLD'
        path = ss.trim_path_entry_to_exit(
            hr.path_between(chron, tr.get('opened'), tr.get('closed')),
            direction=direction, entry=entry, exit_level=exit_level,
            entry_bar_time=hr._iso(tr.get('opened')),
            exit_bar_time=hr._iso(tr.get('closed')))
        pre = hr.pre_bars_before(chron, tr.get('opened'), limit=ss.MAX_PRE_BARS)
        stop_d = profit_d = None
        if path:
            ranges = [float(b['high']) - float(b['low']) for b in path
                      if _finite(b.get('high')) is not None
                      and _finite(b.get('low')) is not None]
            if ranges:
                ranges = sorted(ranges)
                stop_d = profit_d = max(float(tick) * 2, ranges[len(ranges) // 2])
        try:
            from .bracket_learn import infer_exit_reason_from_path
            exit_reason = infer_exit_reason_from_path(
                direction=direction, entry=entry, exit_level=exit_level,
                bars=path, stop_distance=stop_d, profit_distance=profit_d,
                tick=tick)
        except Exception:
            exit_reason = 'loss' if pnl < 0 else 'tp'
        spread = _finite(tr.get('spread')) or float(default_spread)
        if pnl < 0:
            rec = ss.record_loss(
                store, epic=ep, direction=direction, entry=entry,
                exit_level=exit_level, exit_reason=exit_reason or 'loss',
                broker_pnl=pnl, bars=path, pre_bars=pre,
                deal_id=tr.get('deal_id'), spread=spread)
        elif pnl > 0:
            rec = ss.record_win(
                store, epic=ep, direction=direction, entry=entry,
                exit_level=exit_level, exit_reason=exit_reason or 'tp',
                broker_pnl=pnl, bars=path, pre_bars=pre,
                deal_id=tr.get('deal_id'), spread=spread)
        else:
            skipped += 1
            continue
        if not rec:
            skipped += 1
            continue
        ingested += 1
        st = ((rec.get('snapshot') or {}).get('frame') or {}).get('structure')
        if not st:
            st = (ss.frame_from_ohlc(path, entry=entry, exit_level=exit_level,
                                     pre_bars=pre) or {}).get('structure')
        key = f'{st}|{direction}'
        by_structure[key] = by_structure.get(key, 0) + 1

    return {
        'ok': ingested > 0,
        'ingested': ingested,
        'skipped': skipped,
        'by_structure': by_structure,
        'library_samples': library_sample_count(store),
    }


def collect_gold_samples_from_backups(root: Path | None = None) -> dict:
    """Dedupe GOLD closed trades + pick richest candle tape from backups."""
    root = Path(root or os.environ.get('VISI_DATI') or DEFAULT_BACKUP_ROOT)
    if not root.is_dir():
        return {'ok': False, 'reason': 'no_backup_root', 'root': str(root)}
    dumps = hr.discover_sqlite_dumps(root)
    if not dumps:
        return {'ok': False, 'reason': 'no_dumps', 'root': str(root)}

    trades_by_key = {}
    candles = []
    candle_epic = 'GOLD'
    sources = []
    for db_path in dumps:
        try:
            rows = hr._read_src_trades(Path(db_path))
        except Exception:
            continue
        gold = [r for r in rows if _is_gold(r.get('epic'))]
        if not gold:
            continue
        added = 0
        for r in gold:
            key = hr._trade_merge_key(r)
            if key is None:
                continue
            prev = trades_by_key.get(key)
            if prev is None or hr._trade_quality(r) > hr._trade_quality(prev):
                trades_by_key[key] = r
                added += 1
        # Prefer the dump with the most GOLD candles for framing paths.
        try:
            from app.engine import Store
            src = Store(Path(db_path))
            # Guess epic from first gold trade.
            ep = gold[0].get('epic') or 'GOLD'
            c = hr.load_candles(src, ep)
            if len(c) > len(candles):
                candles = c
                candle_epic = ep
            sources.append({'path': str(db_path), 'gold_trades': len(gold),
                            'added': added, 'candles': len(c)})
        except Exception as exc:
            sources.append({'path': str(db_path), 'gold_trades': len(gold),
                            'error': type(exc).__name__})

    trades = sorted(
        trades_by_key.values(),
        key=lambda r: str(r.get('opened') or ''))
    return {
        'ok': bool(trades) and len(candles) >= 40,
        'trades': trades,
        'candles': candles,
        'n_trades': len(trades),
        'n_candles': len(candles),
        'sources': sources,
        'root': str(root),
        'epic': candle_epic or 'GOLD',
    }


def build_strategies_from_samples(
        store, *, backup_root=None, use_store_trades=True,
        clear_library=False, force=False) -> dict:
    """Samples → library → strategy cards. Idempotent unless ``force``."""
    if store is None:
        return {'ok': False, 'reason': 'no_store'}
    prev = store.get(STORE_FLAG) or {}
    lib_n = library_sample_count(store)
    if (isinstance(prev, dict) and prev.get('done') and not force
            and lib_n >= int(prev.get('library_samples') or 0)
            and lib_n >= 30):
        # Still harvest so cards track library.
        h = strategy_gen.harvest(store)
        return {
            'ok': True, 'reason': 'already_ingested',
            'library_samples': lib_n, 'harvest': h,
        }

    trades = []
    candles = []
    epic = 'GOLD'
    source = 'none'

    if use_store_trades:
        try:
            st_trades = [t for t in hr.load_trades(store) if _is_gold(t.get('epic'))]
            if st_trades:
                ep = st_trades[0].get('epic') or 'GOLD'
                st_candles = hr.load_candles(store, ep)
                if len(st_candles) >= 40:
                    trades, candles, epic = st_trades, st_candles, ep
                    source = 'store'
        except Exception:
            pass

    # Backups fill / enlarge the school when local store is thin.
    need_backup = len(trades) < 40 or force
    if need_backup or not trades:
        bag = collect_gold_samples_from_backups(backup_root)
        if bag.get('ok'):
            # Merge: prefer union by deal_id.
            by_key = {hr._trade_merge_key(t): t for t in trades
                      if hr._trade_merge_key(t)}
            for t in bag['trades']:
                k = hr._trade_merge_key(t)
                if k and k not in by_key:
                    by_key[k] = t
            trades = sorted(by_key.values(), key=lambda r: str(r.get('opened') or ''))
            if len(bag.get('candles') or []) > len(candles):
                candles = bag['candles']
            epic = bag.get('epic') or epic
            source = 'store+backups' if source == 'store' else 'backups'

    if not trades:
        # Fall back: audit seed cards only (no sample paths).
        seed = strategy_gen.seed_audit_cards(store)
        h = strategy_gen.harvest(store)
        return {
            'ok': False, 'reason': 'no_samples',
            'seed': seed, 'harvest': h, 'source': source,
        }

    ingest = ingest_trades(
        store, trades, candles, epic=epic, clear=clear_library)
    # Seed cards then harvest so library EV becomes live strategies.
    seed = strategy_gen.seed_audit_cards(store)
    harvest = strategy_gen.harvest(store, epic=epic)
    bag = strategy_gen.load(store)
    summary = {
        'ok': bool(ingest.get('ok')),
        'source': source,
        'epic': epic,
        'trades': len(trades),
        'candles': len(candles),
        'ingest': ingest,
        'seed': seed,
        'harvest': harvest,
        'active_cards': list(bag.get('active_ids') or []),
        'library_samples': library_sample_count(store),
    }
    store.set(STORE_FLAG, {
        'done': True,
        'library_samples': summary['library_samples'],
        'trades': len(trades),
        'source': source,
        'active_n': len(summary['active_cards']),
    })
    try:
        store.event('learning', 'Strategies built from trade samples', {
            'source': source, 'trades': len(trades),
            'ingested': ingest.get('ingested'),
            'library_samples': summary['library_samples'],
            'active_cards': summary['active_cards'],
            'by_structure': ingest.get('by_structure'),
        })
        from .learning_generation import bump
        if ingest.get('ingested'):
            bump(store, 'sample_strategies_built', kind='architecture_changes')
    except Exception:
        pass
    return summary
