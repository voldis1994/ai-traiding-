"""Learning reset: wipe neural history, keep platform clients + secret.key."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reset_learning


def _seed_platform(data: Path, *, clients=1):
    """Minimal platform registry matching PlatformStore tables."""
    db = data / 'platform.sqlite3'
    conn = sqlite3.connect(str(db))
    conn.executescript(
        '''
        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, created TEXT);
        CREATE TABLE clients (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,
            display_name TEXT, language TEXT DEFAULT 'lv', active INTEGER DEFAULT 1, created TEXT);
        CREATE TABLE broker_accounts (
            id INTEGER PRIMARY KEY, client_id INTEGER, broker_type TEXT, display_name TEXT,
            api_key_enc TEXT, identifier_enc TEXT, password_enc TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE sessions (token TEXT PRIMARY KEY, role TEXT, subject_id INTEGER, created TEXT, expires TEXT);
        INSERT INTO admins VALUES (1,'admin','hash','t');
        '''
    )
    for i in range(1, clients + 1):
        conn.execute(
            'INSERT INTO clients VALUES (?,?,?,?,?,?,?)',
            (i, f'user{i}', 'hash', f'User {i}', 'lv', 1, 't'))
        conn.execute(
            'INSERT INTO broker_accounts VALUES (?,?,?,?,?,?,?,?)',
            (i, i, 'capital_com', f'Acc {i}', 'enc_k', 'enc_i', 'enc_p', 1))
    conn.commit()
    conn.close()
    (data / 'secret.key').write_bytes(b'x' * 44)


def _seed_learning(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('robot.sqlite3', 'robot.sqlite3-wal', 'robot.sqlite3-shm',
                 'brain.npz', 'brain_abc.npz', 'policy.py', 'active_policy.py'):
        (folder / name).write_bytes(b'old-learning-' + name.encode())


class TestFreshStart(unittest.TestCase):
    def test_wipes_learning_keeps_clients_and_secret(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / 'data'
            data.mkdir()
            _seed_platform(data, clients=2)
            _seed_learning(data)
            _seed_learning(data / 'accounts' / 'client_1')
            _seed_learning(data / 'accounts' / 'client_2')
            (data / 'README.txt').write_text('old', encoding='utf-8')

            ok, msg = reset_learning.fresh_start(
                data, 'RESET', check_running=False, stamp='test')
            self.assertTrue(ok, msg)

            # Registry + encryption key untouched.
            self.assertTrue((data / 'platform.sqlite3').is_file())
            self.assertEqual((data / 'secret.key').read_bytes(), b'x' * 44)
            conn = sqlite3.connect(str(data / 'platform.sqlite3'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM broker_accounts').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT username FROM admins').fetchone()[0], 'admin')
            conn.close()

            # Learning artifacts gone from live data tree.
            for name in ('robot.sqlite3', 'brain.npz', 'active_policy.py', 'policy.py'):
                self.assertFalse((data / name).exists(), name)
                self.assertFalse((data / 'accounts' / 'client_1' / name).exists(), name)

            # Client folders remain for the same client_id paths.
            self.assertTrue((data / 'accounts' / 'client_1').is_dir())
            self.assertTrue((data / 'accounts' / 'client_2').is_dir())

            backup = Path(td) / 'NEURAL_AI_LEARNING_BACKUP_test'
            self.assertTrue(backup.is_dir())
            self.assertEqual((backup / 'brain.npz').read_bytes(), b'old-learning-brain.npz')
            self.assertEqual(
                (backup / 'accounts' / 'client_1' / 'robot.sqlite3').read_bytes(),
                b'old-learning-robot.sqlite3')
            # Must never put platform/secret into the learning backup as a wipe target.
            self.assertFalse((backup / 'platform.sqlite3').exists())
            self.assertFalse((backup / 'secret.key').exists())

    def test_per_client_scope_leaves_other_client_learning(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / 'data'
            data.mkdir()
            _seed_platform(data, clients=2)
            _seed_learning(data / 'accounts' / 'client_1')
            _seed_learning(data / 'accounts' / 'client_2')
            ok, msg = reset_learning.fresh_start(
                data, 'RESET', check_running=False, stamp='c1', client_id=1)
            self.assertTrue(ok, msg)
            self.assertFalse((data / 'accounts' / 'client_1' / 'brain.npz').exists())
            self.assertTrue((data / 'accounts' / 'client_2' / 'brain.npz').is_file())
            self.assertTrue((data / 'platform.sqlite3').is_file())

    def test_does_not_reset_without_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / 'data'
            data.mkdir()
            (data / 'brain.npz').write_bytes(b'keep')
            _seed_platform(data)
            ok, msg = reset_learning.fresh_start(data, '', check_running=False)
            self.assertFalse(ok)
            self.assertEqual((data / 'brain.npz').read_bytes(), b'keep')
            self.assertTrue((data / 'platform.sqlite3').is_file())

    def test_refuses_if_robot_port_occupied(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / 'data'
            data.mkdir()
            (data / 'robot.sqlite3').write_bytes(b'keep')
            _seed_platform(data)
            with mock.patch.object(reset_learning, 'service_running', return_value=True):
                ok, msg = reset_learning.fresh_start(data, 'RESET')
            self.assertFalse(ok)
            self.assertIn('8765', msg)
            self.assertEqual((data / 'robot.sqlite3').read_bytes(), b'keep')
            self.assertTrue((data / 'platform.sqlite3').is_file())

    def test_refuses_to_overwrite_backup(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / 'data'
            data.mkdir()
            (data / 'brain.npz').write_bytes(b'keep')
            _seed_platform(data)
            (Path(td) / 'NEURAL_AI_LEARNING_BACKUP_test').mkdir()
            ok, msg = reset_learning.fresh_start(
                data, 'RESET', check_running=False, stamp='test')
            self.assertFalse(ok)
            self.assertEqual((data / 'brain.npz').read_bytes(), b'keep')

    def test_no_existing_data(self):
        with tempfile.TemporaryDirectory() as td:
            ok, msg = reset_learning.fresh_start(
                Path(td) / 'data', 'RESET', check_running=False)
            self.assertFalse(ok)
            self.assertIn('not found', msg)

    def test_already_clean_is_ok_and_keeps_platform(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / 'data'
            data.mkdir()
            _seed_platform(data)
            ok, msg = reset_learning.fresh_start(
                data, 'RESET', check_running=False, stamp='clean')
            self.assertTrue(ok, msg)
            self.assertTrue((data / 'platform.sqlite3').is_file())
            self.assertTrue((data / 'secret.key').is_file())


if __name__ == '__main__':
    unittest.main()
