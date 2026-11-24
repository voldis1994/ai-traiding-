"""Persistent trade / analysis cycle state machine (audit P0-12).

Stages 01–10 with durable status, attempts, errors, and version stamps.
HOLD ends the analysis cycle cleanly; SETTLEMENT_PENDING does not block
independent market / position work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

STAGES = (
    'PERCEIVE',
    'ANALYZE',
    'DECIDE',
    'EXECUTE',
    'MANAGE',
    'CLOSE',
    'SETTLE',
    'LEARN',
    'VALIDATE',
    'COMPLETE',
)

# Required DONE stages for analysis-only (HOLD) path — no EXECUTE..SETTLE.
HOLD_COMPLETE_STAGES = frozenset({'PERCEIVE', 'ANALYZE', 'DECIDE'})

STATUS_PENDING = 'PENDING'
STATUS_RUNNING = 'RUNNING'
STATUS_DONE = 'DONE'
STATUS_FAILED = 'FAILED'
STATUS_SKIPPED = 'SKIPPED'
STATUS_SETTLEMENT_PENDING = 'SETTLEMENT_PENDING'


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def new_cycle_id() -> str:
    return uuid4().hex


def _blank_stage() -> dict[str, Any]:
    return {
        'status': STATUS_PENDING,
        'started_at': None,
        'ended_at': None,
        'duration_ms': None,
        'attempts': 0,
        'error_code': None,
        'input_version': None,
        'result_version': None,
        'proof': None,
    }


def new_cycle(
    *,
    account_id: str | None = None,
    deal_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    return {
        'cycle_id': cycle_id or new_cycle_id(),
        'account_id': account_id,
        'deal_id': deal_id,
        'created_at': _utc_now(),
        'updated_at': _utc_now(),
        'decision': None,
        'stages': {name: _blank_stage() for name in STAGES},
        'complete': False,
        'hold_only': False,
    }


def start_stage(
    cycle: dict[str, Any],
    stage: str,
    *,
    input_version: str | None = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f'unknown stage: {stage}')
    st = dict(cycle['stages'].get(stage) or _blank_stage())
    st['status'] = STATUS_RUNNING
    st['started_at'] = _utc_now()
    st['ended_at'] = None
    st['duration_ms'] = None
    st['attempts'] = int(st.get('attempts') or 0) + 1
    st['error_code'] = None
    if input_version is not None:
        st['input_version'] = input_version
    cycle = dict(cycle)
    stages = dict(cycle.get('stages') or {})
    stages[stage] = st
    cycle['stages'] = stages
    cycle['updated_at'] = _utc_now()
    return cycle


def finish_stage(
    cycle: dict[str, Any],
    stage: str,
    *,
    status: str = STATUS_DONE,
    error_code: str | None = None,
    result_version: str | None = None,
    proof: Any = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f'unknown stage: {stage}')
    st = dict(cycle['stages'].get(stage) or _blank_stage())
    st['status'] = status
    st['ended_at'] = _utc_now()
    if st.get('started_at'):
        try:
            t0 = datetime.fromisoformat(str(st['started_at']).replace('Z', '+00:00'))
            t1 = datetime.fromisoformat(str(st['ended_at']).replace('Z', '+00:00'))
            st['duration_ms'] = int((t1 - t0).total_seconds() * 1000)
        except Exception:
            st['duration_ms'] = None
    st['error_code'] = error_code
    if result_version is not None:
        st['result_version'] = result_version
    if proof is not None:
        st['proof'] = proof
    cycle = dict(cycle)
    stages = dict(cycle.get('stages') or {})
    stages[stage] = st
    cycle['stages'] = stages
    cycle['updated_at'] = _utc_now()
    return cycle


def set_decision(cycle: dict[str, Any], decision: str, *, reason: str | None = None) -> dict[str, Any]:
    decision_u = str(decision or '').upper()
    cycle = dict(cycle)
    cycle['decision'] = decision_u
    cycle['decision_reason'] = reason
    cycle['hold_only'] = decision_u == 'HOLD'
    cycle['updated_at'] = _utc_now()
    if decision_u == 'HOLD':
        # HOLD closes the analysis cycle — skip EXECUTE..LEARN path.
        for skip in ('EXECUTE', 'MANAGE', 'CLOSE', 'SETTLE', 'LEARN', 'VALIDATE'):
            st = dict(cycle['stages'].get(skip) or _blank_stage())
            if st.get('status') == STATUS_PENDING:
                st['status'] = STATUS_SKIPPED
                st['proof'] = 'hold_no_trade'
                cycle['stages'][skip] = st
        cycle = finish_stage(cycle, 'DECIDE', status=STATUS_DONE, proof=reason or 'HOLD')
        cycle = mark_complete(cycle)
    return cycle


def mark_settlement_pending(cycle: dict[str, Any], *, error_code: str | None = None) -> dict[str, Any]:
    cycle = finish_stage(
        cycle, 'SETTLE',
        status=STATUS_SETTLEMENT_PENDING,
        error_code=error_code or 'funds_delayed',
    )
    cycle['complete'] = False
    return cycle


def mark_complete(cycle: dict[str, Any]) -> dict[str, Any]:
    """COMPLETE only when all required stages are DONE or SKIPPED."""
    cycle = dict(cycle)
    stages = cycle.get('stages') or {}
    required = HOLD_COMPLETE_STAGES if cycle.get('hold_only') else frozenset(STAGES) - {'COMPLETE'}
    for name in required:
        st = stages.get(name) or {}
        status = st.get('status')
        if status not in (STATUS_DONE, STATUS_SKIPPED):
            cycle['complete'] = False
            cycle['updated_at'] = _utc_now()
            return cycle
        if status == STATUS_SETTLEMENT_PENDING:
            cycle['complete'] = False
            cycle['updated_at'] = _utc_now()
            return cycle
    cycle = finish_stage(cycle, 'COMPLETE', status=STATUS_DONE, proof='all_required_done')
    cycle['complete'] = True
    cycle['updated_at'] = _utc_now()
    return cycle


def persist(store, cycle: dict[str, Any], *, key_prefix: str = 'trade_cycle:') -> str:
    cid = str(cycle.get('cycle_id') or new_cycle_id())
    cycle = dict(cycle)
    cycle['cycle_id'] = cid
    store.set(f'{key_prefix}{cid}', cycle)
    # Latest pointer for restart recovery.
    store.set(f'{key_prefix}latest', {'cycle_id': cid, 'at': _utc_now()})
    deal = cycle.get('deal_id')
    if deal:
        store.set(f'{key_prefix}by_deal:{deal}', {'cycle_id': cid, 'at': _utc_now()})
    return cid


def load(store, cycle_id: str, *, key_prefix: str = 'trade_cycle:') -> dict[str, Any] | None:
    raw = store.get(f'{key_prefix}{cycle_id}')
    return raw if isinstance(raw, dict) else None


def load_latest(store, *, key_prefix: str = 'trade_cycle:') -> dict[str, Any] | None:
    ptr = store.get(f'{key_prefix}latest')
    if not isinstance(ptr, dict) or not ptr.get('cycle_id'):
        return None
    return load(store, str(ptr['cycle_id']), key_prefix=key_prefix)


def pending_stages(cycle: dict[str, Any]) -> list[dict[str, Any]]:
    """Visible reasons for incomplete work (acceptance: 0 unexplained stalls)."""
    out = []
    for name in STAGES:
        st = (cycle.get('stages') or {}).get(name) or {}
        status = st.get('status')
        if status in (STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED, STATUS_SETTLEMENT_PENDING):
            out.append({
                'stage': name,
                'status': status,
                'error_code': st.get('error_code'),
                'attempts': st.get('attempts') or 0,
                'next_attempt_hint': 'retry' if status != STATUS_PENDING else 'await_start',
            })
    return out
