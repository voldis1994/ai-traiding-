#!/usr/bin/env python3
"""Find LEARNING_BACKUP / robot*.sqlite3 dumps and relearn the maximum from them.

Windows operators: double-click ``extract_learning_windows.bat``.
Does NOT average brains — merges journals (deduped), then one corrected relearn.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engine import AdaptiveBrain, Store
from app.history_relearn import (
    CLIENT_DIR_RE, discover_seed_brain, discover_sqlite_dumps,
    prepare_merged_workdir, relearn,
)


def _client_dir_from_brain(brain_path: Path | None) -> Path | None:
    """If seed brain lives in data/accounts/client_N/, return that folder."""
    if brain_path is None:
        return None
    try:
        p = Path(brain_path).resolve()
    except OSError:
        return None
    if p.name.lower() != 'brain.npz':
        return None
    parent = p.parent
    if CLIENT_DIR_RE.match(parent.name) and parent.parent.name == 'accounts':
        return parent
    return None


def _list_client_dirs(root: Path) -> list:
    accounts = root / 'data' / 'accounts'
    if not accounts.is_dir():
        return []
    out = []
    try:
        for folder in sorted(accounts.iterdir()):
            if folder.is_dir() and CLIENT_DIR_RE.match(folder.name):
                out.append(folder)
    except OSError:
        return []
    return out


def _pick_install_clients(root: Path, client: str | None, *,
                          all_clients: bool = True,
                          seed_brain: Path | None = None) -> list:
    """Default: every data/accounts/client_N. ``--client`` selects one."""
    accounts = root / 'data' / 'accounts'
    if not accounts.is_dir():
        return []
    if client:
        folder = accounts / client
        return [folder] if folder.is_dir() else []
    if all_clients:
        return _list_client_dirs(root)
    # Legacy single-target fallback: seed client, else client_1, else first.
    from_seed = _client_dir_from_brain(seed_brain)
    if from_seed is not None and from_seed.is_dir():
        return [from_seed]
    preferred = accounts / 'client_1'
    if preferred.is_dir():
        return [preferred]
    listed = _list_client_dirs(root)
    return listed[:1] if listed else []


def _install_into_client(*, root: Path, work: Path, client_dir: Path,
                         merged_store=None) -> dict:
    """Copy relearn brain + structure/profit memory into a live client folder."""
    src_brain = Path(work) / 'brain.npz'
    if not src_brain.is_file():
        return {'ok': False, 'reason': 'missing_relearn_brain', 'path': str(src_brain)}
    dst_brain = client_dir / 'brain.npz'
    if dst_brain.is_file():
        bak = client_dir / 'brain.before_backup_relearn.npz'
        shutil.copy2(dst_brain, bak)
    shutil.copy2(src_brain, dst_brain)

    store = merged_store
    close_store = False
    if store is None:
        merged_db = Path(work) / 'robot.sqlite3'
        if not merged_db.is_file():
            return {
                'ok': True, 'brain': str(dst_brain), 'client': str(client_dir),
                'rollback_brain': str(client_dir / 'brain.before_backup_relearn.npz'),
                'structure_warning': 'no_merged_sqlite',
            }
        store = Store(merged_db)
        close_store = True

    live = Store(client_dir / 'robot.sqlite3')
    try:
        from app import structure_shadow as ss
        from app.profit_learning import ProfitLearning
        for key in (ss.STORE_KEY, ss.SNAPSHOT_KEY, ss.LAST_ADVICE_KEY,
                    'history_merge_last_v1'):
            val = store.get(key)
            if val is not None:
                live.set(key, val)
        pl_src = ProfitLearning(store)
        pl_dst = ProfitLearning(live)
        pl_dst.history = list(pl_src.history or [])
        pl_dst._save()
        installed = {
            'ok': True, 'brain': str(dst_brain),
            'client': str(client_dir),
            'rollback_brain': str(client_dir / 'brain.before_backup_relearn.npz'),
        }
    except Exception as exc:
        installed = {
            'ok': True, 'brain': str(dst_brain),
            'client': str(client_dir),
            'structure_warning': str(exc),
        }
    live.close()
    if close_store:
        store.close()
    return installed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT,
                   help='Project folder (default: this repo)')
    p.add_argument('--out', type=Path, default=None,
                   help='Output folder (default: data/relearn_from_backups)')
    p.add_argument('--brain', type=Path, default=None,
                   help='Seed brain.npz (default: auto-detect live client)')
    p.add_argument('--extra-db', action='append', default=[],
                   help='Extra robot*.sqlite3 paths (repeatable)')
    p.add_argument('--scan-parent', action='store_true',
                   help='Also scan the parent folder for NEURAL_AI_LEARNING_BACKUP_*')
    p.add_argument('--no-loose', action='store_true',
                   help='Do not pick loose robot(1).sqlite3-style copies')
    p.add_argument('--install', action='store_true',
                   help='After OK relearn, copy brain into all client_* (or --client)')
    p.add_argument('--install-only', action='store_true',
                   help='Only copy existing relearn_from_backups brain into clients')
    p.add_argument('--client', type=str, default=None,
                   help='Install only this client_N (default: ALL client_*)')
    p.add_argument('--lr', type=float, default=0.28)
    p.add_argument('--loss-repeats', type=int, default=3)
    p.add_argument('--win-repeats', type=int, default=2)
    args = p.parse_args(argv)

    root = args.root.expanduser().resolve()
    out = (args.out or (root / 'data' / 'relearn_from_backups')).expanduser().resolve()

    if args.install_only:
        brain_path = discover_seed_brain(
            root, scan_parent=bool(args.scan_parent), prefer=args.brain)
        clients = _pick_install_clients(
            root, args.client, all_clients=True, seed_brain=brain_path)
        if not clients:
            print(json.dumps({'ok': False, 'reason': 'no_client_folder'}, indent=2))
            return 1
        results = []
        for client_dir in clients:
            results.append(_install_into_client(
                root=root, work=out, client_dir=client_dir, merged_store=None))
        ok = all(r.get('ok') for r in results)
        print(json.dumps({
            'ok': ok,
            'install_only': True,
            'install': results,
            'clients': [str(c) for c in clients],
            'out': str(out),
        }, indent=2, default=str))
        if ok:
            for r in results:
                print(f'[OK] Installed into {r.get("client")}')
            print('[OK] Now start run_windows.bat (robot must have been STOPPED).')
            return 0
        return 1

    sources = discover_sqlite_dumps(
        root,
        extra=list(args.extra_db),
        scan_parent=bool(args.scan_parent),
        loose=not args.no_loose,
    )
    # Never relearn from our own previous output journal.
    sources = [s for s in sources if out not in s.parents and s.parent != out]

    brain_path = discover_seed_brain(
        root, scan_parent=bool(args.scan_parent), prefer=args.brain)
    if not sources:
        print(json.dumps({
            'ok': False,
            'reason': 'no_sqlite_sources',
            'hint': 'Put NEURAL_AI_LEARNING_BACKUP_* next to the project, '
                    'or drop robot*.sqlite3 copies into the project folder.',
            'root': str(root),
        }, indent=2))
        return 1
    if brain_path is None or not Path(brain_path).is_file():
        print(json.dumps({
            'ok': False,
            'reason': 'no_seed_brain',
            'hint': 'Need data/accounts/client_N/brain.npz (or pass --brain).',
            'root': str(root),
        }, indent=2))
        return 1

    print(f'[INFO] Root: {root}')
    print(f'[INFO] Seed brain: {brain_path}')
    print(f'[INFO] Found {len(sources)} sqlite dump(s):')
    for s in sources:
        try:
            rel = s.relative_to(root)
        except ValueError:
            rel = s
        print(f'       - {rel}')
    print(f'[INFO] Output: {out}')
    print('[INFO] Merging journals + relearning (no mid-trade M1 replay)...')

    paths = prepare_merged_workdir(
        brain=brain_path, work=out, sources=sources, seed_db=None)
    store = paths['store']
    brain = AdaptiveBrain(store, data_dir=str(paths['work']))
    report = relearn(
        store=store, brain=brain, lr=args.lr,
        loss_repeats=args.loss_repeats, win_repeats=args.win_repeats)
    report['merge'] = paths['merge']
    report['sources'] = [str(s) for s in sources]
    report['seed_brain'] = str(brain_path)

    out_json = Path(paths['work']) / 'relearn_report.json'
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    installed = None
    if report.get('ok') and args.install:
        clients = _pick_install_clients(
            root, args.client, all_clients=True, seed_brain=brain_path)
        if not clients:
            installed = [{'ok': False, 'reason': 'no_client_folder'}]
        else:
            installed = [
                _install_into_client(
                    root=root, work=paths['work'], client_dir=client_dir,
                    merged_store=store)
                for client_dir in clients
            ]

    summary = {
        'ok': report.get('ok'),
        'taught': report.get('taught'),
        'skipped': report.get('skipped'),
        'trades': report.get('trades'),
        'candles': report.get('candles'),
        'modes': report.get('modes'),
        'structure_buckets': (report.get('structure') or {}).get('buckets'),
        'model_version': report.get('model_version'),
        'merge_sources': (paths['merge'] or {}).get('sources'),
        'merge_unique_trades': (paths['merge'] or {}).get('unique_trades'),
        'merge_candles_inserted': (paths['merge'] or {}).get('candles_inserted_total'),
        'seed_brain': str(brain_path),
        'out': str(paths['work']),
        'brain_out': str(Path(paths['work']) / 'brain.npz'),
        'report': str(out_json),
        'install': installed,
    }
    print(json.dumps(summary, indent=2, default=str))
    if report.get('ok'):
        print('[OK] Relearn finished. Deploy brain:')
        print(f'     {Path(paths["work"]) / "brain.npz"}')
        if isinstance(installed, list) and installed:
            if all(r.get('ok') for r in installed):
                for r in installed:
                    print(f'[OK] Installed into {r.get("client")}')
            else:
                print('[WARN] Install skipped/failed for some clients — see JSON.')
        elif args.install:
            print('[WARN] Install skipped/failed — copy brain manually if needed.')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
