#!/usr/bin/env python3
"""Relearn LIVE weights from one dump — or merge many LEARNING_BACKUP sqlite files.

Examples:
  # Single dump
  PYTHONPATH=. python3 scripts/relearn_from_history.py \\
      --db /path/robot.sqlite3 --brain /path/brain.npz --out /tmp/relearn_out

  # Max extract: scan project + all NEURAL_AI_LEARNING_BACKUP_* journals
  PYTHONPATH=. python3 scripts/relearn_from_history.py \\
      --backups-root /path/NeuralTradingAI_NEW \\
      --brain /path/data/accounts/client_1/brain.npz \\
      --out /tmp/relearn_merged
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engine import AdaptiveBrain, Store
from app.history_relearn import (
    discover_sqlite_dumps, prepare_merged_workdir, prepare_workdir, relearn,
)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, help='Single source robot.sqlite3')
    p.add_argument('--brain', required=True, type=Path, help='Source brain.npz')
    p.add_argument('--out', required=True, type=Path, help='Working output directory')
    p.add_argument('--backups-root', type=Path,
                   help='Project root containing NEURAL_AI_LEARNING_BACKUP_* + data/')
    p.add_argument('--extra-db', action='append', default=[],
                   help='Additional robot.sqlite3 paths (repeatable)')
    p.add_argument('--scan-parent', action='store_true',
                   help='Also scan parent folder for NEURAL_AI_LEARNING_BACKUP_*')
    p.add_argument('--lr', type=float, default=0.28)
    p.add_argument('--loss-repeats', type=int, default=3)
    p.add_argument('--win-repeats', type=int, default=2)
    args = p.parse_args(argv)

    merge_info = None
    if args.backups_root or args.extra_db:
        sources = discover_sqlite_dumps(
            args.backups_root or Path('.'),
            extra=list(args.extra_db) + ([args.db] if args.db else []),
            scan_parent=bool(args.scan_parent),
            loose=True)
        if not sources:
            print(json.dumps({'ok': False, 'reason': 'no_sqlite_sources'}))
            return 1
        paths = prepare_merged_workdir(
            brain=args.brain, work=args.out, sources=sources, seed_db=None)
        merge_info = paths['merge']
        store = paths['store']
        brain = AdaptiveBrain(store, data_dir=str(paths['work']))
    else:
        if not args.db:
            print(json.dumps({'ok': False, 'reason': 'need --db or --backups-root'}))
            return 1
        paths = prepare_workdir(args.db, args.brain, args.out)
        store = Store(paths['db'])
        brain = AdaptiveBrain(store, data_dir=str(paths['work']))

    report = relearn(
        store=store, brain=brain, lr=args.lr,
        loss_repeats=args.loss_repeats, win_repeats=args.win_repeats)
    if merge_info is not None:
        report['merge'] = merge_info
    out_json = Path(paths['work']) / 'relearn_report.json'
    out_json.write_text(json.dumps(report, indent=2, default=str))
    summary = {
        'ok': report.get('ok'),
        'taught': report.get('taught'),
        'skipped': report.get('skipped'),
        'trades': report.get('trades'),
        'candles': report.get('candles'),
        'modes': report.get('modes'),
        'structure_buckets': (report.get('structure') or {}).get('buckets'),
        'model_version': report.get('model_version'),
        'out': str(paths['work']),
        'report': str(out_json),
    }
    if merge_info is not None:
        summary['merge_sources'] = merge_info.get('sources')
        summary['merge_unique_trades'] = merge_info.get('unique_trades')
        summary['merge_candles_inserted'] = merge_info.get('candles_inserted_total')
    print(json.dumps(summary, indent=2))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
