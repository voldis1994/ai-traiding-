#!/usr/bin/env python3
"""Relearn LIVE weights from a saved robot.sqlite3 + brain.npz dump.

Example:
  PYTHONPATH=. python3 scripts/relearn_from_history.py \\
      --db /path/robot.sqlite3 --brain /path/brain.npz --out /tmp/relearn_out
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
from app.history_relearn import prepare_workdir, relearn


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True, type=Path, help='Source robot.sqlite3')
    p.add_argument('--brain', required=True, type=Path, help='Source brain.npz')
    p.add_argument('--out', required=True, type=Path, help='Working output directory')
    p.add_argument('--lr', type=float, default=0.28)
    p.add_argument('--loss-repeats', type=int, default=3)
    p.add_argument('--win-repeats', type=int, default=2)
    args = p.parse_args(argv)

    paths = prepare_workdir(args.db, args.brain, args.out)
    store = Store(paths['db'])
    brain = AdaptiveBrain(store, data_dir=str(paths['work']))
    report = relearn(
        store=store, brain=brain, lr=args.lr,
        loss_repeats=args.loss_repeats, win_repeats=args.win_repeats)
    out_json = paths['work'] / 'relearn_report.json'
    out_json.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({
        'ok': report.get('ok'), 'taught': report.get('taught'),
        'skipped': report.get('skipped'), 'modes': report.get('modes'),
        'structure_buckets': (report.get('structure') or {}).get('buckets'),
        'model_version': report.get('model_version'),
        'out': str(paths['work']), 'report': str(out_json),
    }, indent=2))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
