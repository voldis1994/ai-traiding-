"""Per-teacher execution proofs + idempotence (PR #303 FINAL).

Each teacher (bracket, day_book/bank, adaptive, reentry, entry_gates, ohlc_audit)
records its own durable proof. Restart must not skip unfinished teachers or
re-run finished ones. ``teachers_settled`` is derived only when all required
proofs are present.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

KEY_PREFIX = 'teacher_proof:'

REQUIRED_TEACHERS = (
    'bracket_learn',
    'day_book_bank',
    'adaptive_close',
    'reentry_confirm',
    'entry_gates',
    'ohlc_audit',
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _key(deal_id: str) -> str:
    return KEY_PREFIX + str(deal_id)


def load(store, deal_id: str) -> dict:
    blob = store.get(_key(deal_id))
    return dict(blob) if isinstance(blob, dict) else {}


def is_done(store, deal_id: str, teacher: str) -> bool:
    blob = load(store, deal_id)
    row = blob.get(str(teacher))
    return bool(isinstance(row, dict) and row.get('done'))


def mark_done(store, deal_id: str, teacher: str, *, proof: dict | None = None) -> bool:
    """Record teacher execution. Returns True if newly marked (False if idempotent)."""
    name = str(teacher)
    blob = load(store, deal_id)
    prev = blob.get(name)
    if isinstance(prev, dict) and prev.get('done'):
        return False
    row = {'done': True, 'at': _now()}
    if proof:
        row.update({k: v for k, v in proof.items() if k not in ('done',)})
    blob[name] = row
    store.set(_key(deal_id), blob)
    return True


def clear(store, deal_id: str, teachers: Iterable[str] | None = None) -> None:
    """Clear proofs (correction re-teach)."""
    if teachers is None:
        store.set(_key(deal_id), None)
        return
    blob = load(store, deal_id)
    for t in teachers:
        blob.pop(str(t), None)
    store.set(_key(deal_id), blob or None)


def all_required_done(store, deal_id: str,
                      required: Iterable[str] = REQUIRED_TEACHERS) -> bool:
    blob = load(store, deal_id)
    for name in required:
        row = blob.get(str(name))
        if not (isinstance(row, dict) and row.get('done')):
            return False
    return True


def missing(store, deal_id: str,
            required: Iterable[str] = REQUIRED_TEACHERS) -> list[str]:
    blob = load(store, deal_id)
    out = []
    for name in required:
        row = blob.get(str(name))
        if not (isinstance(row, dict) and row.get('done')):
            out.append(str(name))
    return out


def settle_if_complete(store, deal_id: str, *, source=None, pnl=None,
                       required: Iterable[str] = REQUIRED_TEACHERS) -> bool:
    """Set teachers_settled only when every required teacher has proof."""
    if not all_required_done(store, deal_id, required=required):
        return False
    store.set('teachers_settled:' + str(deal_id), {
        'at': _now(),
        'source': source,
        'pnl': pnl,
        'proofs': load(store, deal_id),
    })
    return True
