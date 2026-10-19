"""Operator-confirmed fresh start for Neural Trading AI (offline, Python stdlib only).

Move the *entire* runtime data folder aside; never connect to the broker and
never delete a damaged SQLite DB, WAL, journal or previous neural weights.
The application will initialize new runtime data on its next start.
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def data_folder():
    return Path(os.getenv('NEURAL_AI_DATA_DIR') or ROOT / 'data').expanduser().resolve()


def service_running():
    # Connecting to the app port is enough to refuse a reset while it may be live.
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=0.5):
            return True
    except OSError:
        return False


def fresh_start(data: Path, confirmation: str, *, check_running=True, stamp=None):
    """Return (success, message). No existing data is deleted."""
    data = Path(data).expanduser().resolve()
    if confirmation != 'RESET':
        return False, 'Cancelled: type RESET (uppercase) to authorize a fresh start.'
    if check_running and service_running():
        return False, 'STOP: localhost:8765 is in use. Close the robot completely, then retry.'
    if not data.is_dir():
        return False, f'Data directory not found: {data}. Nothing was changed.'
    # Avoid applying a destructive folder reset to a mistyped directory.
    if data.name.lower() != 'data' and not os.getenv('NEURAL_AI_DATA_DIR'):
        return False, 'Unexpected data folder. Nothing was changed.'
    stamp = stamp or dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup = data.parent / f'NEURAL_AI_DATA_BACKUP_{stamp}'
    if backup.exists():
        return False, f'Backup already exists: {backup}. Nothing was changed.'
    try:
        # Atomic same-parent rename: original full history can be restored later.
        data.rename(backup)
    except OSError as exc:
        return False, f'Backup failed, original untouched: {type(exc).__name__}: {exc}'
    try:
        data.mkdir()
        (data / 'README.txt').write_text(
            'Fresh learning state. Previous complete data folder is backed up at:\n'
            + str(backup) + '\n'
            'Do not copy old robot.sqlite3, -wal/-shm, brain*.npz or active_policy.py '
            'into this new data folder.\n', encoding='utf-8')
    except OSError as exc:
        # If possible restore original atomically. Otherwise retain the backup.
        try:
            if data.exists():
                for p in data.iterdir():
                    if p.is_file():
                        p.unlink()
                data.rmdir()
            backup.rename(data)
            return False, f'Fresh folder creation failed; original restored: {exc}'
        except OSError:
            return False, f'Fresh folder creation failed; original retained at {backup}: {exc}'
    return True, (
        f'FRESH START READY.\nOld data backup: {backup}\n'
        f'New empty data folder: {data}\n'
        'Neural weights, prior mandate, trade/order journal and policy evolution '
        'state will restart from zero. Capital.com broker positions are NOT changed.\n'
        'Start run_windows.bat, reconnect LIVE, and check open positions/SL/TP '
        'directly with Capital.com. The first manual position adoption process '
        'will be required again.'
    )


def main():
    data = data_folder()
    print('NEURAL TRADING AI -- FRESH LEARNING RESET', flush=True)
    print('DATA FOLDER:', data, flush=True)
    print('This moves ALL existing learning, policy, trade/order logs and SQLite files')
    print('into a timestamped BACKUP. It does not contact Capital.com or place orders.')
    print('Your broker positions will remain OPEN if they are open at the broker.')
    print('\n1. Close ALL robot windows / Python processes.')
    print('2. Verify LIVE positions and stop loss directly in Capital.com.')
    print('3. Type RESET to confirm or press Enter to cancel.\n')
    try:
        confirmation = input('Type RESET: ').strip()
    except (EOFError, KeyboardInterrupt):
        print('Cancelled.')
        return 2
    success, message = fresh_start(data, confirmation)
    print('\n' + message, flush=True)
    return 0 if success else 2


if __name__ == '__main__':
    sys.exit(main())
