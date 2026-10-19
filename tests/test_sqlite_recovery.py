"""Failsafe for malformed trading journals; no silent LIVE state reset."""
import sqlite3
from pathlib import Path

import pytest

from app.engine import Store
import repair_database


def test_corruption_never_silently_recreates_order_journal(tmp_path):
    db_file = tmp_path / 'robot.sqlite3'
    original = b'not a valid SQLite db; preserve me'
    db_file.write_bytes(original)
    with pytest.raises(RuntimeError, match='repair_database_windows.bat'):
        Store(db_file)
    assert db_file.read_bytes() == original
    assert repair_database.run(['--database', str(db_file), '--check-only']) == 1


def test_clean_db_recovery_preserves_critical_tables(tmp_path):
    db_file = tmp_path / 'robot.sqlite3'
    store = Store(db_file)
    store.set('initial_mobile_trade_seen', True)
    store.order('OPEN', 'deal1', {'direction':'SELL','size':'0.25'})
    store.upsert_trade({'dealId':'deal1','epic':'GOLD','size':'0.25','direction':'SELL','level':4156})
    store.event('order', 'broker status observed')
    store.db.execute('INSERT INTO signals(time,epic,action,scores,price,model_step,label) VALUES(?,?,?,?,?,?,?)',
                     ('t','GOLD','SELL','{"SELL":0.5}',4150,1,None))
    store.db.commit()
    # Explicit WAL checkpoint/close happens only with all runtime users stopped.
    store.db.close()
    snapshot = tmp_path / 'backup'
    repair_database.backup_files(db_file, snapshot)
    recovered = snapshot / 'recovered.sqlite3'
    report, valid, problems = repair_database.recover(snapshot / db_file.name, recovered)
    assert valid, problems
    assert all(error is None for _, error in report.values()), report
    rebuilt = Store(recovered)
    assert rebuilt.get('initial_mobile_trade_seen') is True
    assert rebuilt.unresolved()[0]['deal_id'] == 'deal1'
    assert rebuilt.trades()[0]['deal_id'] == 'deal1'
    assert rebuilt.signals()[0]['action'] == 'SELL'
    rebuilt.db.close()


def test_clean_database_check_only_makes_no_backup(tmp_path):
    db_file = tmp_path / 'robot.sqlite3'
    s = Store(db_file)
    s.db.close()
    assert repair_database.run(['--database', str(db_file), '--check-only']) == 0
    assert not list(tmp_path.glob('database_recovery_*'))
