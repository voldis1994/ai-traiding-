"""Operator-confirmed learning wipe — keeps clients and broker credentials.

Moves neural weights, robot SQLite journals and policy files into a timestamped
backup. Never contacts the broker. Never touches platform.sqlite3 or secret.key
(admin/client logins and encrypted Capital.com API credentials stay intact).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import socket
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# Preserved at data/ root so admins/clients do not re-register.
KEEP_ROOT_NAMES = frozenset({
    'platform.sqlite3',
    'platform.sqlite3-wal',
    'platform.sqlite3-shm',
    'platform.sqlite3-journal',
    'secret.key',
    'README.txt',
})

CLIENT_DIR_RE = re.compile(r'^client_\d+$')


def data_folder():
    return Path(os.getenv('NEURAL_AI_DATA_DIR') or ROOT / 'data').expanduser().resolve()


def service_running():
    # Connecting to the app port is enough to refuse a reset while it may be live.
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=0.5):
            return True
    except OSError:
        return False


def _is_learning_file(path: Path) -> bool:
    """True for neural / journal / policy artifacts (never platform or secret)."""
    name = path.name
    if name in KEEP_ROOT_NAMES:
        return False
    if name in ('robot.sqlite3', 'robot.sqlite3-wal', 'robot.sqlite3-shm',
                'robot.sqlite3-journal', 'active_policy.py', 'policy.py'):
        return True
    if name == 'brain.npz' or (name.startswith('brain_') and name.endswith('.npz')):
        return True
    if name.endswith('.npz') and 'brain' in name.lower():
        return True
    return False


def _iter_learning_paths(data: Path, *, client_id=None):
    """Yield learning files under data/ (legacy root + accounts/client_N/)."""
    if client_id is None:
        for child in sorted(data.iterdir()):
            if child.is_file() and _is_learning_file(child):
                yield child
        accounts = data / 'accounts'
        if accounts.is_dir():
            for folder in sorted(accounts.iterdir()):
                if folder.is_dir() and CLIENT_DIR_RE.match(folder.name):
                    for child in sorted(folder.iterdir()):
                        if child.is_file() and _is_learning_file(child):
                            yield child
        return

    cid = int(client_id)
    folder = data / 'accounts' / f'client_{cid}'
    if folder.is_dir():
        for child in sorted(folder.iterdir()):
            if child.is_file() and _is_learning_file(child):
                yield child


def _rel_under(data: Path, path: Path) -> Path:
    try:
        return path.relative_to(data)
    except ValueError:
        return Path(path.name)


def fresh_start(data: Path, confirmation: str, *, check_running=True, stamp=None,
                client_id=None):
    """Wipe learning history; keep platform clients + secret.key.

    Returns (success, message). Existing learning files are moved to a backup,
    not permanently deleted.
    """
    data = Path(data).expanduser().resolve()
    if confirmation != 'RESET':
        return False, 'Cancelled: type RESET (uppercase) to authorize a fresh start.'
    if check_running and service_running():
        return False, (
            'STOP: localhost:8765 is in use. Close the robot completely '
            '(or Stop all clients in Admin), then retry.'
        )
    if not data.is_dir():
        return False, f'Data directory not found: {data}. Nothing was changed.'
    # Avoid applying a destructive folder reset to a mistyped directory.
    if data.name.lower() != 'data' and not os.getenv('NEURAL_AI_DATA_DIR'):
        return False, 'Unexpected data folder. Nothing was changed.'

    stamp = stamp or dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup = data.parent / f'NEURAL_AI_LEARNING_BACKUP_{stamp}'
    if backup.exists():
        return False, f'Backup already exists: {backup}. Nothing was changed.'

    targets = list(_iter_learning_paths(data, client_id=client_id))
    platform_db = data / 'platform.sqlite3'
    secret = data / 'secret.key'
    kept = []
    if platform_db.is_file():
        kept.append('platform.sqlite3 (admins + clients + broker credentials)')
    if secret.is_file():
        kept.append('secret.key (credential encryption)')

    if not targets:
        scope = f'client_{int(client_id)}' if client_id is not None else 'all clients'
        return True, (
            f'Nothing to wipe for {scope} — learning files already absent.\n'
            f'Kept: {", ".join(kept) or "no platform registry found yet"}.\n'
            f'Data folder: {data}'
        )

    moved = []
    try:
        backup.mkdir(parents=True)
        for src in targets:
            rel = _rel_under(data, src)
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append(str(rel))
    except OSError as exc:
        # Best-effort restore from backup into original locations.
        restored = 0
        if backup.exists():
            for dest in backup.rglob('*'):
                if not dest.is_file():
                    continue
                try:
                    rel = dest.relative_to(backup)
                    src = data / rel
                    if not src.exists():
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dest), str(src))
                        restored += 1
                except OSError:
                    pass
            try:
                # Remove empty backup tree if restore emptied it.
                for p in sorted(backup.rglob('*'), reverse=True):
                    if p.is_dir():
                        try:
                            p.rmdir()
                        except OSError:
                            pass
                if not any(backup.iterdir()):
                    backup.rmdir()
            except OSError:
                pass
        return False, (
            f'Backup/move failed after {len(moved)} file(s); '
            f'restored≈{restored}. Error: {type(exc).__name__}: {exc}'
        )

    # Ensure per-client folders still exist so the next start reuses client_id paths.
    accounts = data / 'accounts'
    if client_id is not None:
        (accounts / f'client_{int(client_id)}').mkdir(parents=True, exist_ok=True)
    elif accounts.is_dir():
        for folder in accounts.iterdir():
            if folder.is_dir() and CLIENT_DIR_RE.match(folder.name):
                folder.mkdir(parents=True, exist_ok=True)

    try:
        note = (
            'Learning wiped for a fresh start.\n'
            f'Backup of neural/trade/policy files: {backup}\n'
            'PRESERVED: platform.sqlite3 (clients + broker APIs) and secret.key.\n'
            'Do NOT copy old robot.sqlite3 / brain*.npz / policy.py back unless '
            'you intend to restore the previous learning state.\n'
        )
        (data / 'README.txt').write_text(note, encoding='utf-8')
    except OSError:
        pass

    scope = f'client_{int(client_id)} only' if client_id is not None else 'all learning files'
    return True, (
        f'FRESH LEARNING START READY ({scope}).\n'
        f'Moved {len(moved)} file(s) → {backup}\n'
        f'Kept: {", ".join(kept) or "(platform/secret not present yet)"}.\n'
        'Clients do NOT need to re-register. Broker credentials stay encrypted in platform.sqlite3.\n'
        'Capital.com open positions are NOT changed.\n'
        'Restart the robot / Start clients in Admin. Mandate re-adoption may be required '
        'on the next LIVE session (learning journal was cleared).'
    )


def main():
    data = data_folder()
    print('NEURAL TRADING AI -- FRESH LEARNING RESET (KEEP CLIENTS)', flush=True)
    print('DATA FOLDER:', data, flush=True)
    print('This moves neural weights, robot.sqlite3 journals and policy files')
    print('into a timestamped BACKUP. It does NOT contact Capital.com.')
    print('PRESERVED: platform.sqlite3 (admins/clients/broker APIs) + secret.key')
    print('Your broker positions remain OPEN if they are open at the broker.')
    print('\n1. Close ALL robot windows / Stop every client in Admin.')
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
