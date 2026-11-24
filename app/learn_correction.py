"""Identify and controlled-correct trades learned from bad cash (item 8).

Never restore a pre-deal-A weight checkpoint after deals B/C have correctly
trained — that would wipe good ΔW. Instead: compensatory re-teach from
verified Cash Ledger £ (opposite of the bad teach when possible), and only
mark APPLIED when the re-teach actually commits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import learn_attempt as la
from . import cash_ledger
from .realized_cash import from_minor, may_teach_live_cash


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def taught_meta(store, deal_id: str) -> dict[str, Any] | None:
    att = la.get_attempt(store, deal_id)
    if att and att.get('state') == la.STATE_COMMITTED:
        return att
    legacy = store.get('learned_trade:' + str(deal_id))
    if isinstance(legacy, dict) and legacy.get('learned'):
        return legacy
    return None


def _newer_committed_exists(store, deal_id: str, after_at: str | None) -> bool:
    """True when another deal COMMITTED learn after this deal's teach time."""
    if not after_at:
        return True  # unknown → refuse full rewind
    try:
        rows = store.db.execute(
            "SELECT deal_id FROM trades WHERE closed IS NOT NULL "
            "ORDER BY closed DESC LIMIT 80").fetchall()
    except Exception:
        return True
    for (other,) in rows:
        oid = str(other)
        if oid == str(deal_id):
            continue
        att = la.get_attempt(store, oid)
        if not att or att.get('state') != la.STATE_COMMITTED:
            continue
        at = str(att.get('at') or '')
        if at and at > str(after_at):
            return True
    return False


def needs_correction(store, deal_id: str) -> dict[str, Any] | None:
    """Compare taught pnl/source vs cash_ledger truth."""
    meta = taught_meta(store, deal_id)
    if not meta:
        return None
    taught_src = str(meta.get('source') or '')
    taught_pnl = meta.get('pnl')
    bad_source = (not may_teach_live_cash(taught_src)) or taught_src in (
        'capital', 'upl_estimate', 'estimate')
    realized = cash_ledger.deal_realized(store, str(deal_id))
    if not realized.get('may_teach'):
        if bad_source:
            return {
                'deal_id': str(deal_id),
                'reason': 'taught_from_non_cash_awaiting_funds',
                'taught_source': taught_src,
                'taught_pnl': taught_pnl,
                'ledger_status': realized.get('status'),
                'taught_at': meta.get('at'),
            }
        return None
    ccy = realized.get('currency') or 'GBP'
    try:
        ledger_pnl = float(from_minor(int(realized['amount_minor']), ccy))
    except Exception:
        return None
    try:
        taught_f = float(taught_pnl) if taught_pnl is not None else None
    except (TypeError, ValueError):
        taught_f = None
    material = False
    if taught_f is None:
        material = True
    else:
        if (taught_f > 0) != (ledger_pnl > 0) and abs(ledger_pnl) >= 1e-9:
            material = True
        elif abs(taught_f - ledger_pnl) >= 0.02:
            material = True
    if bad_source or material:
        return {
            'deal_id': str(deal_id),
            'reason': 'funds_correction' if material else 'bad_source',
            'taught_source': taught_src,
            'taught_pnl': taught_f,
            'ledger_pnl': ledger_pnl,
            'ledger_status': realized.get('status'),
            'taught_at': meta.get('at'),
        }
    return None


def flag_for_replay(store, deal_id: str, info: dict[str, Any]) -> dict:
    """Persist correction flag; clear COMMITTED so recovery can replay once."""
    key = 'learn_correction:' + str(deal_id)
    payload = dict(info)
    payload['at'] = _utc_now()
    payload['status'] = 'QUEUED'
    payload['mode'] = 'compensatory'  # never full weight rewind by default
    att = la.get_attempt(store, deal_id)
    store.set(key, payload)
    if att and att.get('state') == la.STATE_COMMITTED:
        la.transition(
            store, deal_id, la.STATE_ABORTED,
            error_code='correction_queued',
            pnl=info.get('ledger_pnl'),
            source='broker')
        store.set('learned_trade:' + str(deal_id), {
            'claimed': False, 'learned': False, 'at': _utc_now(),
            'correction': True, 'prev': att,
        })
        # Allow re-teach past teachers_settled gate.
        store.set('teachers_settled:' + str(deal_id), None)
    store.event('learning', 'Learn correction queued', payload)
    return payload


def apply_queued_corrections(engine, *, limit: int = 8) -> list[dict]:
    """Compensatory re-teach from ledger — never wipe later correct ΔW.

    APPLIED only when `_learn_closed_trade` actually commits a new learn.
    """
    store = engine.store
    applied = []
    try:
        rows = store.db.execute(
            "SELECT deal_id FROM trades WHERE closed IS NOT NULL "
            "ORDER BY closed DESC LIMIT ?", (int(limit) * 3,)).fetchall()
    except Exception:
        return applied
    for (deal_id,) in rows:
        if len(applied) >= limit:
            break
        key = 'learn_correction:' + str(deal_id)
        flag = store.get(key)
        if not isinstance(flag, dict) or flag.get('status') != 'QUEUED':
            continue
        realized = cash_ledger.deal_realized(store, str(deal_id))
        if not realized.get('may_teach') or realized.get('amount_minor') is None:
            continue
        if str(realized.get('status') or '') not in (
                cash_ledger.STATUS_SETTLED, cash_ledger.STATUS_SETTLED_ZERO):
            continue
        ccy = realized.get('currency') or engine._account_currency() or 'GBP'
        try:
            pnl = float(from_minor(int(realized['amount_minor']), ccy))
        except Exception:
            continue
        # Refuse full weight restore when newer correct learns exist.
        taught_at = flag.get('taught_at')
        if _newer_committed_exists(store, str(deal_id), taught_at):
            flag = dict(flag)
            flag['mode'] = 'compensatory'
            flag['rewind_refused'] = 'newer_committed_exists'
        # Clear gates that would skip re-teach.
        store.set('teachers_settled:' + str(deal_id), None)
        store.set('learned_trade:' + str(deal_id), {
            'claimed': False, 'learned': False, 'at': _utc_now(),
            'correction': True,
        })
        # Compensatory delta: opposite of (taught − ledger) when sign/material
        # error — still not full unlearning, but cancels the bad cash label.
        taught_pnl = flag.get('taught_pnl')
        try:
            taught_f = float(taught_pnl) if taught_pnl is not None else None
        except (TypeError, ValueError):
            taught_f = None
        compensatory_delta = None
        if taught_f is not None:
            compensatory_delta = float(pnl) - taught_f
        conf = {
            'profit': pnl, 'pnl_currency': ccy, 'pnl_source': 'broker',
            'source': 'learn_correction', 'level': None,
            'correction': True,
            'compensatory_delta': compensatory_delta,
        }
        before = la.get_attempt(store, str(deal_id))
        before_state = (before or {}).get('state')
        try:
            engine._learn_closed_trade(str(deal_id), conf)
        except Exception as exc:
            store.event('warning', 'Learn correction re-teach failed',
                        {'deal_id': deal_id, 'type': type(exc).__name__})
            flag = dict(flag)
            flag['status'] = 'FAILED'
            flag['error'] = type(exc).__name__
            flag['at'] = _utc_now()
            store.set(key, flag)
            continue
        after = la.get_attempt(store, str(deal_id))
        committed = bool(after and after.get('state') == la.STATE_COMMITTED)
        # Also accept teachers_settled if neural claim was scratch-skipped but
        # ledger cash teachers ran.
        teachers = store.get('teachers_settled:' + str(deal_id))
        ok = committed or (
            isinstance(teachers, dict)
            and teachers.get('source') in ('broker', 'cash_ledger', 'funds')
            and before_state != la.STATE_COMMITTED)
        flag = dict(flag)
        if ok:
            flag['status'] = 'APPLIED'
            flag['applied_at'] = _utc_now()
            flag['ledger_pnl'] = pnl
            flag['compensatory_delta'] = compensatory_delta
            flag['committed'] = committed
            flag['mode'] = 'compensatory'
            applied.append(flag)
            store.event('learning', 'Learn correction applied', flag)
        else:
            flag['status'] = 'FAILED'
            flag['error'] = 'reteach_did_not_commit'
            flag['at'] = _utc_now()
            store.event('warning', 'Learn correction did not commit', flag)
        store.set(key, flag)
    return applied


def scan_and_flag(store, *, limit: int = 80) -> list[dict]:
    """Scan recent closed journal deals for contradicted learns."""
    out = []
    try:
        rows = store.db.execute(
            "SELECT deal_id FROM trades WHERE closed IS NOT NULL "
            "ORDER BY closed DESC LIMIT ?", (int(limit),)).fetchall()
    except Exception:
        return out
    for (deal_id,) in rows:
        info = needs_correction(store, str(deal_id))
        if not info:
            continue
        existing = store.get('learn_correction:' + str(deal_id))
        if isinstance(existing, dict) and existing.get('status') in (
                'QUEUED', 'APPLIED'):
            continue
        out.append(flag_for_replay(store, str(deal_id), info))
    return out


def recover_incomplete_on_boot(store) -> list[dict]:
    """Item 7: resume CLAIMED/RUNNING/APPLIED after restart — all sources."""
    flagged = []
    try:
        from . import work_queue
        work_queue.ensure_schema(store)
        work_queue.requeue_stale_running(store)
        work_queue.enqueue(
            store, work_queue.KIND_LEARN_RECOVERY,
            {'scan': True}, dedupe_key='learn_recovery:boot', delay_s=0.5)
    except Exception:
        pass
    pending = list(store.get('pending_broker_pnl_deals') or [])
    deal_ids = set()
    for item in pending:
        deal = str(item.get('deal_id') if isinstance(item, dict) else item)
        if deal:
            deal_ids.add(deal)
    try:
        rows = store.db.execute(
            "SELECT deal_id FROM trades WHERE closed IS NOT NULL "
            "ORDER BY closed DESC LIMIT 120").fetchall()
        for (deal_id,) in rows:
            deal_ids.add(str(deal_id))
    except Exception:
        pass
    for deal in list(deal_ids):
        corr = store.get('learn_correction:' + deal)
        if isinstance(corr, dict) and corr.get('status') == 'QUEUED':
            deal_ids.add(deal)
    for deal in deal_ids:
        att = la.recover_incomplete(store, deal)
        if att and att.get('needs_recovery'):
            flagged.append(att)
    return flagged
