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
    discover_seed_brain, discover_sqlite_dumps, prepare_merged_workdir, relearn,
)


def _pick_install_client(root: Path, client: str | None) -> Path | None:
    accounts = root / 'data' / 'accounts'
    if not accounts.is_dir():
        return None
    if client:
        folder = accounts / client
        return folder if folder.is_dir() else None
    # Prefer client_1, else first client_* with brain.npz / robot.sqlite3.
    preferred = accounts / 'client_1'
    if preferred.is_dir():
        return preferred
    for folder in sorted(accounts.iterdir()):
        if folder.is_dir() and folder.name.startswith('client_'):
            return folder
    return None


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
                   help='After OK relearn, copy brain.npz into the live client folder')
    p.add_argument('--client', type=str, default=None,
                   help='Install target client folder name (default: client_1)')
    p.add_argument('--lr', type=float, default=0.28)
    p.add_argument('--loss-repeats', type=int, default=3)
    p.add_argument('--win-repeats', type=int, default=2)
    args = p.parse_args(argv)

    root = args.root.expanduser().resolve()
    out = (args.out or (root / 'data' / 'relearn_from_backups')).expanduser().resolve()

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
        client_dir = _pick_install_client(root, args.client)
        if client_dir is None:
            installed = {'ok': False, 'reason': 'no_client_folder'}
        else:
            src_brain = Path(paths['work']) / 'brain.npz'
            dst_brain = client_dir / 'brain.npz'
            # Keep previous live brain as rollback beside the client.
            if dst_brain.is_file():
                bak = client_dir / 'brain.before_backup_relearn.npz'
                shutil.copy2(dst_brain, bak)
            shutil.copy2(src_brain, dst_brain)
            # Structure library lives in robot.sqlite3 state — copy merged store
            # keys the operator needs without wiping live candle stream: merge
            # report already rebuilt structure inside out/robot.sqlite3; copy
            # that file only when client has no open need — safer: copy state
            # via Store.get keys.
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
        if installed and installed.get('ok'):
            print(f'[OK] Installed into {installed.get("client")}')
        elif args.install and not (installed and installed.get('ok')):
            print('[WARN] Install skipped/failed — copy brain manually if needed.')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
