"""Durable work queue with retry / backoff / diagnostics (audit P1-15 / item 10).

Persists in SQLite via Store.db — never silently drops unfinished work.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS work_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_queue_due
  ON work_queue(status, next_attempt_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_queue_dedupe
  ON work_queue(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('PENDING','RUNNING','FAILED');
"""

STATUS_PENDING = 'PENDING'
STATUS_RUNNING = 'RUNNING'
STATUS_DONE = 'DONE'
STATUS_FAILED = 'FAILED'
STATUS_DEAD = 'DEAD'

KIND_SETTLE_PNL = 'settle_pnl'
KIND_FUNDS_MIRROR = 'funds_mirror'
KIND_LEARN_RECOVERY = 'learn_recovery'
KIND_SHADOW_FLUSH = 'shadow_flush'
KIND_CANDLE_DELTA = 'candle_delta'


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except Exception:
        return None


def ensure_schema(store) -> None:
    db = store.db
    db.executescript(TABLE_SQL)
    db.commit()


def enqueue(
    store,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    delay_s: float = 0.0,
) -> int | None:
    ensure_schema(store)
    now = _utc_now()
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(delay_s)))).strftime(
        '%Y-%m-%dT%H:%M:%S.%fZ')
    blob = json.dumps(payload or {}, default=str)
    try:
        if dedupe_key:
            row = store.db.execute(
                "SELECT id,status FROM work_queue WHERE dedupe_key=? "
                "AND status IN ('PENDING','RUNNING','FAILED') LIMIT 1",
                (dedupe_key,)).fetchone()
            if row:
                return int(row[0])
        cur = store.db.execute(
            'INSERT INTO work_queue(kind,payload,status,attempts,next_attempt_at,'
            'last_error,created_at,updated_at,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?)',
            (kind, blob, STATUS_PENDING, 0, nxt, None, now, now, dedupe_key))
        store.db.commit()
        return int(cur.lastrowid)
    except Exception as exc:
        store.event('warning', 'work_queue enqueue failed',
                    {'kind': kind, 'type': type(exc).__name__})
        return None


def claim_due(store, *, kinds: list[str] | None = None, limit: int = 20) -> list[dict]:
    ensure_schema(store)
    now = _utc_now()
    params: list[Any] = [STATUS_PENDING, STATUS_FAILED, now]
    sql = (
        "SELECT id,kind,payload,status,attempts,next_attempt_at,last_error,"
        "created_at,dedupe_key FROM work_queue "
        "WHERE status IN (?,?) AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
    )
    if kinds:
        placeholders = ','.join('?' for _ in kinds)
        sql += f'AND kind IN ({placeholders}) '
        params.extend(kinds)
    sql += 'ORDER BY next_attempt_at ASC, id ASC LIMIT ?'
    params.append(int(limit))
    rows = store.db.execute(sql, tuple(params)).fetchall()
    out = []
    for r in rows:
        wid = int(r[0])
        store.db.execute(
            "UPDATE work_queue SET status=?, updated_at=? WHERE id=? AND status IN (?,?)",
            (STATUS_RUNNING, now, wid, STATUS_PENDING, STATUS_FAILED))
        store.db.commit()
        try:
            payload = json.loads(r[2] or '{}')
        except Exception:
            payload = {}
        out.append({
            'id': wid, 'kind': r[1], 'payload': payload, 'status': STATUS_RUNNING,
            'attempts': int(r[4] or 0), 'next_attempt_at': r[5], 'last_error': r[6],
            'created_at': r[7], 'dedupe_key': r[8],
        })
    return out


def complete(store, work_id: int) -> None:
    ensure_schema(store)
    store.db.execute(
        "UPDATE work_queue SET status=?, updated_at=?, last_error=NULL WHERE id=?",
        (STATUS_DONE, _utc_now(), int(work_id)))
    store.db.commit()


def fail(
    store,
    work_id: int,
    error: str,
    *,
    attempts: int,
    max_attempts: int = 12,
    base_delay_s: float = 2.0,
) -> dict:
    ensure_schema(store)
    attempts = int(attempts) + 1
    now = datetime.now(timezone.utc)
    if attempts >= int(max_attempts):
        status = STATUS_DEAD
        nxt = None
    else:
        status = STATUS_FAILED
        # Exponential backoff with cap 15 minutes.
        delay = min(900.0, float(base_delay_s) * (2 ** min(10, attempts - 1)))
        nxt = (now + timedelta(seconds=delay)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    store.db.execute(
        "UPDATE work_queue SET status=?, attempts=?, next_attempt_at=?, "
        "last_error=?, updated_at=? WHERE id=?",
        (status, attempts, nxt, str(error)[:500], _utc_now(), int(work_id)))
    store.db.commit()
    return {'status': status, 'attempts': attempts, 'next_attempt_at': nxt, 'error': error}


def diagnostics(store) -> dict[str, Any]:
    ensure_schema(store)
    counts = {}
    for st, n in store.db.execute(
            'SELECT status, COUNT(*) FROM work_queue GROUP BY status'):
        counts[str(st)] = int(n)
    oldest = store.db.execute(
        "SELECT id,kind,created_at,attempts,last_error FROM work_queue "
        "WHERE status IN ('PENDING','FAILED','RUNNING') "
        "ORDER BY created_at ASC LIMIT 5").fetchall()
    return {
        'counts': counts,
        'oldest': [
            {'id': r[0], 'kind': r[1], 'created_at': r[2],
             'attempts': r[3], 'last_error': r[4]}
            for r in oldest
        ],
        'size': sum(counts.values()),
    }


def requeue_stale_running(store, *, older_than_s: float = 120.0) -> int:
    """Crash recovery: RUNNING jobs older than threshold → PENDING with backoff."""
    ensure_schema(store)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=float(older_than_s))).strftime(
        '%Y-%m-%dT%H:%M:%S.%fZ')
    rows = store.db.execute(
        "SELECT id,attempts FROM work_queue WHERE status=? AND updated_at<=?",
        (STATUS_RUNNING, cutoff)).fetchall()
    n = 0
    for wid, attempts in rows:
        fail(store, int(wid), 'stale_running_recovered',
             attempts=int(attempts or 0))
        n += 1
    return n


def deal_in_backoff(store, deal_id: str) -> bool:
    """True when durable settle job for deal is not yet due (respect backoff)."""
    ensure_schema(store)
    now = _utc_now()
    row = store.db.execute(
        "SELECT status,next_attempt_at FROM work_queue "
        "WHERE dedupe_key=? AND status IN ('PENDING','FAILED','RUNNING') LIMIT 1",
        ('settle:' + str(deal_id),)).fetchone()
    if not row:
        return False
    status, nxt = row[0], row[1]
    if status == STATUS_RUNNING:
        return True  # already claimed this cycle
    if nxt and str(nxt) > now:
        return True
    return False


def store_open_settle_payloads(store) -> list[dict[str, Any]]:
    """Open settle job payloads for Desk mirror (no Capital I/O)."""
    ensure_schema(store)
    rows = store.db.execute(
        "SELECT payload,status,attempts,next_attempt_at,last_error FROM work_queue "
        "WHERE kind=? AND status IN ('PENDING','FAILED','RUNNING') "
        "ORDER BY id ASC LIMIT 500",
        (KIND_SETTLE_PNL,)).fetchall()
    out: list[dict[str, Any]] = []
    for payload, status, attempts, nxt, err in rows:
        try:
            data = json.loads(payload or '{}')
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {'deal_id': str(data)}
        data = dict(data)
        data['queue_status'] = status
        data['attempts'] = int(attempts or 0)
        data['next_attempt_at'] = nxt
        data['error'] = err
        out.append(data)
    return out


def migrate_legacy_pending_pnl(store) -> int:
    """Move legacy pending_broker_pnl_deals JSON into durable queue once."""
    ensure_schema(store)
    if store.get('work_queue_migrated_v1'):
        return 0
    pending = list(store.get('pending_broker_pnl_deals') or [])
    n = 0
    for item in pending:
        if isinstance(item, dict):
            deal = str(item.get('deal_id') or '')
            payload = dict(item)
        else:
            deal = str(item)
            payload = {'deal_id': deal}
        if not deal:
            continue
        enqueue(
            store, KIND_SETTLE_PNL, payload,
            dedupe_key=f'settle:{deal}', delay_s=0.0)
        n += 1
    store.set('work_queue_migrated_v1', {'at': _utc_now(), 'migrated': n})
    return n
