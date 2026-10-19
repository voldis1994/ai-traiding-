"""Fresh-reset safety tests: no broker connection and no destructive delete."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reset_learning

class TestFreshStart(unittest.TestCase):
    def test_backup_corrupt_and_brain_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            data=Path(td)/'data';data.mkdir()
            for name in ('robot.sqlite3','robot.sqlite3-wal','robot.sqlite3-shm','brain.npz',
                         'brain_abc.npz','active_policy.py','settings.txt'):
                (data/name).write_bytes(b'old-corrupt-but-preserved')
            ok, msg=reset_learning.fresh_start(data, 'RESET', check_running=False,stamp='test')
            self.assertTrue(ok,msg)
            self.assertEqual([p.name for p in data.iterdir()], ['README.txt'])
            backup=Path(td)/'NEURAL_AI_DATA_BACKUP_test'
            for name in ('robot.sqlite3','robot.sqlite3-wal','robot.sqlite3-shm','brain.npz',
                         'brain_abc.npz','active_policy.py','settings.txt'):
                self.assertEqual((backup/name).read_bytes(), b'old-corrupt-but-preserved')
    def test_does_not_reset_without_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            data=Path(td)/'data';data.mkdir();(data/'brain.npz').write_bytes(b'keep')
            ok,msg=reset_learning.fresh_start(data,'',check_running=False)
            self.assertFalse(ok)
            self.assertEqual((data/'brain.npz').read_bytes(),b'keep')
    def test_refuses_if_robot_port_occupied(self):
        with tempfile.TemporaryDirectory() as td:
            data=Path(td)/'data';data.mkdir();(data/'robot.sqlite3').write_bytes(b'keep')
            with mock.patch.object(reset_learning,'service_running',return_value=True):
                ok,msg=reset_learning.fresh_start(data,'RESET')
            self.assertFalse(ok)
            self.assertIn('8765',msg)
            self.assertEqual((data/'robot.sqlite3').read_bytes(),b'keep')
    def test_refuses_to_overwrite_backup(self):
        with tempfile.TemporaryDirectory() as td:
            data=Path(td)/'data';data.mkdir();(data/'brain.npz').write_bytes(b'keep')
            (Path(td)/'NEURAL_AI_DATA_BACKUP_test').mkdir()
            ok,msg=reset_learning.fresh_start(data,'RESET',check_running=False,stamp='test')
            self.assertFalse(ok)
            self.assertEqual((data/'brain.npz').read_bytes(),b'keep')
    def test_no_existing_data(self):
        with tempfile.TemporaryDirectory() as td:
            ok,msg=reset_learning.fresh_start(Path(td)/'data','RESET',check_running=False)
            self.assertFalse(ok)
            self.assertIn('not found',msg)

if __name__=='__main__':unittest.main()
