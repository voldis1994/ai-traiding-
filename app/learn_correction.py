"""Identify and controlled-correct trades learned from bad cash (item 8).

When a later Funds correction flips sign or material amount vs the taught
source, restore pre-learn neural weights (when checkpoint exists) and re-teach
from verified Cash Ledger £. Simple retrain without undo is not enough.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
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
                'checkpoint': meta.get('checkpoint'),
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
            'checkpoint': meta.get('checkpoint'),
        }
    return None


def flag_for_replay(store, deal_id: str, info: dict[str, Any]) -> dict:
    """Persist correction flag; clear COMMITTED so recovery can replay once."""
    key = 'learn_correction:' + str(deal_id)
    payload = dict(info)
    payload['at'] = _utc_now()
    payload['status'] = 'QUEUED'
    # Preserve weight checkpoint for undo before re-teach.
    att = la.get_attempt(store, deal_id)
    if att and att.get('checkpoint') and not payload.get('checkpoint'):
        payload['checkpoint'] = att.get('checkpoint')
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
            'checkpoint': payload.get('checkpoint'),
        })
    store.event('learning', 'Learn correction queued', payload)
    return payload


def _undo_weights(engine, checkpoint: Any) -> bool:
    """Restore neural weights from pre-learn checkpoint when available."""
    if not checkpoint or engine is None:
        return False
    path = None
    if isinstance(checkpoint, dict):
        path = checkpoint.get('weights_path') or checkpoint.get('path')
    elif isinstance(checkpoint, (str, Path)):
        path = str(checkpoint)
    if not path:
        return False
    brain = getattr(engine, 'brain', None)
    if brain is None or not hasattr(brain, 'restore_weights'):
        return False
    try:
        ok = bool(brain.restore_weights(path))
        if ok:
            engine.store.event('learning', 'Bad-learn weights undone from checkpoint',
                               {'path': str(path)})
        return ok
    except Exception as exc:
        engine.store.event('warning', 'Weight undo failed',
                           {'type': type(exc).__name__, 'path': str(path)})
        return False


def apply_queued_corrections(engine, *, limit: int = 8) -> list[dict]:
    """Undo bad weights (if checkpoint) and re-teach from ledger cash."""
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
        # 1) Undo prior incorrect ΔW when we have a checkpoint.
        undone = _undo_weights(engine, flag.get('checkpoint'))
        ccy = realized.get('currency') or engine._account_currency() or 'GBP'
        try:
            pnl = float(from_minor(int(realized['amount_minor']), ccy))
        except Exception:
            continue
        # 2) Re-teach from verified Funds £.
        conf = {
            'profit': pnl, 'pnl_currency': ccy, 'pnl_source': 'broker',
            'source': 'learn_correction', 'level': None,
        }
        try:
            engine._learn_closed_trade(str(deal_id), conf)
        except Exception as exc:
            store.event('warning', 'Learn correction re-teach failed',
                        {'deal_id': deal_id, 'type': type(exc).__name__})
            continue
        flag = dict(flag)
        flag['status'] = 'APPLIED'
        flag['applied_at'] = _utc_now()
        flag['undone'] = undone
        flag['ledger_pnl'] = pnl
        store.set(key, flag)
        applied.append(flag)
        store.event('learning', 'Learn correction applied', flag)
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
        work_queue.enqueue(
            store, work_queue.KIND_LEARN_RECOVERY,
            {'scan': True}, dedupe_key='learn_recovery:boot', delay_s=0.5)
    except Exception:
        pass
    # Pending settle list.
    pending = list(store.get('pending_broker_pnl_deals') or [])
    deal_ids = set()
    for item in pending:
        deal = str(item.get('deal_id') if isinstance(item, dict) else item)
        if deal:
            deal_ids.add(deal)
    # Also scan recent closed trades for in-flight learn attempts.
    try:
        rows = store.db.execute(
            "SELECT deal_id FROM trades WHERE closed IS NOT NULL "
            "ORDER BY closed DESC LIMIT 120").fetchall()
        for (deal_id,) in rows:
            deal_ids.add(str(deal_id))
    except Exception:
        pass
    # Correction-queued deals.
    for deal in list(deal_ids):
        corr = store.get('learn_correction:' + deal)
        if isinstance(corr, dict) and corr.get('status') == 'QUEUED':
            deal_ids.add(deal)
    for deal in deal_ids:
        att = la.recover_incomplete(store, deal)
        if att and att.get('needs_recovery'):
            flagged.append(att)
    return flagged
