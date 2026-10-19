"""Offline, operator-confirmed SQLite diagnosis and best-effort salvage.

NEVER starts the broker, never places orders, never overwrites the sole copy.
The robot MUST be closed before running this tool (including other instances).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path

TABLES = ('state', 'orders', 'trades', 'events', 'signals', 'candles')
CRITICAL = {'state', 'orders', 'trades'}
SCHEMA = {
    'events': 'CREATE TABLE events (id INTEGER PRIMARY KEY,time TEXT,kind TEXT,message TEXT,details TEXT)',
    'state': 'CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT)',
    'signals': 'CREATE TABLE signals (id INTEGER PRIMARY KEY,time TEXT,epic TEXT,action TEXT,scores TEXT,price REAL,model_step INTEGER,label TEXT,spread REAL,tick_size REAL,label_version INTEGER)',
    'trades': 'CREATE TABLE trades (deal_id TEXT PRIMARY KEY,epic TEXT,size TEXT,direction TEXT,opened TEXT,closed TEXT,entry REAL,exit REAL,pnl REAL)',
    'orders': 'CREATE TABLE orders (id INTEGER PRIMARY KEY,time TEXT,kind TEXT,deal_id TEXT,reference TEXT,payload TEXT,status TEXT)',
    'candles': 'CREATE TABLE candles (epic TEXT,at TEXT,open REAL,high REAL,low REAL,close REAL,PRIMARY KEY(epic,at))',
}


def database_path():
    root = Path(__file__).resolve().parent
    return Path(os.environ.get('NEURAL_AI_DATA_DIR') or root / 'data') / 'robot.sqlite3'


def validate(path):
    try:
        db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
        try:
            result = db.execute('PRAGMA integrity_check').fetchall()
            return (len(result) == 1 and result[0][0] == 'ok', [str(x[0]) for x in result][:8])
        finally:
            db.close()
    except (sqlite3.Error, OSError) as exc:
        return False, [f'{type(exc).__name__}: {exc}']


def backup_files(source, target):
    target.mkdir(parents=True, exist_ok=False)
    # SQLite WAL-mode state consists of a set of files, not only robot.sqlite3.
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(source) + suffix)
        if p.is_file():
            shutil.copy2(p, target / p.name)
    for pat in ('brain*.npz', 'active_policy.py'):
        for p in source.parent.glob(pat):
            if p.is_file():
                shutil.copy2(p, target / p.name)


def create_target(path):
    db = sqlite3.connect(path)
    for sql in SCHEMA.values():
        db.execute(sql)
    db.commit()
    return db


def table_columns(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def extract_table(source, target, table):
    """Safely copy complete table; never present a partial critical table as repaired."""
    available = table_columns(source, table)
    if not available:
        return 0, 'source table missing'
    expected = table_columns(target, table)
    columns = [c for c in expected if c in available]
    if not columns:
        return 0, 'source schema incompatible'
    names = ','.join('"' + c + '"' for c in columns)
    placeholders = ','.join('?' for _ in columns)
    insert = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
    try:
        rows = source.execute(f'SELECT {names} FROM "{table}"').fetchall()
        target.executemany(insert, rows)
        target.commit()
        return len(rows), None
    except sqlite3.Error as exc:
        target.rollback()
        # Reject automatic partial salvage rather than discarding order journal
        # or silently training on an incomplete dataset.
        return 0, f'{type(exc).__name__}: {exc}'


def recover(source, target):
    original = sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True)
    recovered = create_target(target)
    report = {}
    try:
        for table in TABLES:
            report[table] = extract_table(original, recovered, table)
        ok, result = validate(target)
        return report, ok, result
    finally:
        original.close()
        recovered.close()


def run(argv=None):
    parser = argparse.ArgumentParser(description='Offline SQLite backup and integrity recovery')
    parser.add_argument('--database', type=Path, default=None, help='Full path to robot.sqlite3')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    db = (args.database or database_path()).resolve()
    print('Database:', db, flush=True)
    if not db.is_file():
        print('No database found; nothing to recover. No files changed.')
        return 2
    valid, problems = validate(db)
    print('Integrity:', 'OK' if valid else 'CORRUPT', '/'.join(problems))
    if args.check_only or valid:
        return 0 if valid else 1
    print('\nSTOP: Close every running robot process first. Do not edit SQLite files while it runs.')
    if input('Type BACKUP to create a safe snapshot and attempt offline recovery: ').strip() != 'BACKUP':
        print('Cancelled; original data untouched.')
        return 3
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    folder = db.parent / f'database_recovery_{stamp}'
    backup_files(db, folder)
    original_copy = folder / db.name
    recovered_copy = folder / 'robot_recovered.sqlite3'
    print('Safety backup:', folder)
    print('Recovering into a separate file (original is still untouched)...')
    try:
        report, clean, errors = recover(original_copy, recovered_copy)
    except (sqlite3.Error, OSError) as exc:
        print('Recovery could not read original:', type(exc).__name__, str(exc))
        print('Keep the snapshot; do NOT delete the damaged files.')
        return 4
    for table, (rows, error) in report.items():
        print(f'{table:8} {rows:8} rows', ('ERROR: ' + error) if error else '')
    print('Recovered candidate integrity:', 'OK' if clean else errors)
    broken = [name for name, (_, issue) in report.items() if issue]
    if not clean or broken:
        print('\nAUTOMATIC REPLACEMENT REFUSED. Some data could not be read:', ', '.join(broken))
        print('Backup and candidate were kept for expert/manual recovery.')
        print('Do not replace the original with a database missing order/trade/learning history.')
        return 5
    print('\nAll six tables were read and copied successfully.')
    print('Original SQLite files are retained in:', folder)
    print('IMPORTANT: Confirm open positions and SL/TP directly at Capital.com before reconnecting LIVE.')
    if input('Type APPLY to replace the original database with this verified copy: ').strip() != 'APPLY':
        print('Not applied. Verified candidate:', recovered_copy)
        return 0
    # Rename the *set* of original files to prevent stale WAL replay into repaired DB.
    # This is only safe after the robot has been fully stopped.
    for suffix in ('', '-wal', '-shm'):
        original_file = Path(str(db) + suffix)
        if original_file.exists():
            original_file.rename(folder / ('original_before_apply' + suffix))
    shutil.copy2(recovered_copy, db)
    final_ok, detail = validate(db)
    if not final_ok:
        print('WARNING: Recovered copy failed final check:', detail)
        return 6
    print('SUCCESS: Database recovered. Backup saved at:', folder)
    return 0


if __name__ == '__main__':
    sys.exit(run())
