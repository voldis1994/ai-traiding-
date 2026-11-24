"""Identify and controlled-correct trades learned from bad cash (item 8).

When a later Funds correction flips sign or material amount vs the taught
source, mark the learn attempt for replay from checkpoint when available.
Never silently leave neural weights on contradicted UPL/estimate cash.
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


def needs_correction(store, deal_id: str) -> dict[str, Any] | None:
    """Compare taught pnl/source vs cash_ledger truth."""
    meta = taught_meta(store, deal_id)
    if not meta:
        return None
    taught_src = str(meta.get('source') or '')
    taught_pnl = meta.get('pnl')
    # Taught from forbidden/estimate → always correct when Funds arrives.
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
        # Sign flip or >2p absolute delta.
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
        }
    return None


def flag_for_replay(store, deal_id: str, info: dict[str, Any]) -> dict:
    """Persist correction flag; clear COMMITTED so recovery can replay once."""
    key = 'learn_correction:' + str(deal_id)
    payload = dict(info)
    payload['at'] = _utc_now()
    payload['status'] = 'QUEUED'
    store.set(key, payload)
    # Allow one controlled re-learn from verified Funds.
    att = la.get_attempt(store, deal_id)
    if att and att.get('state') == la.STATE_COMMITTED:
        la.transition(
            store, deal_id, la.STATE_ABORTED,
            error_code='correction_queued',
            pnl=info.get('ledger_pnl'),
            source='broker')
        # Fresh claim path will run on next settle teach with broker source.
        store.set('learned_trade:' + str(deal_id), {
            'claimed': False, 'learned': False, 'at': _utc_now(),
            'correction': True, 'prev': att,
        })
    store.event('learning', 'Learn correction queued', payload)
    return payload


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
    """Item 7: resume CLAIMED/RUNNING/APPLIED after restart."""
    flagged = []
    # Scan known learned_trade:* keys via events is hard; use work_queue + corrections.
    try:
        from . import work_queue
        work_queue.ensure_schema(store)
        # Enqueue recovery scanner.
        work_queue.enqueue(
            store, work_queue.KIND_LEARN_RECOVERY,
            {'scan': True}, dedupe_key='learn_recovery:boot', delay_s=0.5)
    except Exception:
        pass
    # Mark any in-flight attempts found via correction/legacy keys in pending list.
    pending = list(store.get('pending_broker_pnl_deals') or [])
    for item in pending:
        deal = str(item.get('deal_id') if isinstance(item, dict) else item)
        if not deal:
            continue
        att = la.recover_incomplete(store, deal)
        if att and att.get('needs_recovery'):
            flagged.append(att)
    return flagged
