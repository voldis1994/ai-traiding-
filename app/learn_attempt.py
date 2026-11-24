"""Durable LEARN attempt states (audit P0-13 / P0-14).

CLAIMED → RUNNING → APPLIED → COMMITTED
Crash after CLAIMED but before COMMITTED is recoverable — never silent skip.
UPL / estimate sources must not enter this pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

STATE_CLAIMED = 'CLAIMED'
STATE_RUNNING = 'RUNNING'
STATE_APPLIED = 'APPLIED'
STATE_COMMITTED = 'COMMITTED'
STATE_FAILED = 'FAILED'
STATE_ABORTED = 'ABORTED'

KEY_PREFIX = 'learn_attempt:'
LEGACY_LEARNED_PREFIX = 'learned_trade:'

# Sources that may never trigger LIVE cash learning.
FORBIDDEN_TEACH_SOURCES = frozenset({
    'capital', 'upl', 'upl_estimate', 'last_upl', 'open_upl',
    'estimate', 'pending_broker_pnl', 'pending_close_sync',
    'ticks_only', 'price_size', 'position_disappeared', 'confirm.profit',
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def may_teach(source: str | None) -> bool:
    s = str(source or '').strip().lower()
    if not s:
        return False
    if s in FORBIDDEN_TEACH_SOURCES:
        return False
    if 'upl' in s or 'estimate' in s or s.startswith('pending'):
        return False
    return s in ('broker', 'funds', 'cash_ledger', 'verified_funds',
                 'capital_funds', 'live_funds_mirror', 'cash')


def _key(deal_id: str) -> str:
    return KEY_PREFIX + str(deal_id)


def get_attempt(store, deal_id: str) -> dict[str, Any] | None:
    raw = store.get(_key(deal_id))
    if isinstance(raw, dict):
        return raw
    # Legacy bridge: claimed/learned flags.
    legacy = store.get(LEGACY_LEARNED_PREFIX + str(deal_id))
    if isinstance(legacy, dict):
        if legacy.get('learned'):
            return {
                'deal_id': str(deal_id),
                'attempt_id': legacy.get('attempt_id') or 'legacy',
                'state': STATE_COMMITTED,
                'source': legacy.get('source'),
                'pnl': legacy.get('pnl'),
                'model_version': legacy.get('model_version'),
                'checkpoint': legacy.get('checkpoint'),
                'at': legacy.get('at') or _utc_now(),
                'legacy': True,
            }
        if legacy.get('claimed'):
            return {
                'deal_id': str(deal_id),
                'attempt_id': legacy.get('attempt_id') or 'legacy',
                'state': STATE_CLAIMED,
                'source': legacy.get('source'),
                'pnl': legacy.get('pnl'),
                'at': legacy.get('at') or _utc_now(),
                'legacy': True,
                'needs_recovery': True,
            }
    return None


def is_committed(store, deal_id: str) -> bool:
    att = get_attempt(store, deal_id)
    return bool(att and att.get('state') == STATE_COMMITTED)


def is_in_flight(store, deal_id: str) -> bool:
    """True for active lease without needs_recovery — not a finished teach."""
    att = get_attempt(store, deal_id)
    if not att or att.get('needs_recovery'):
        return False
    return att.get('state') in (
        STATE_CLAIMED, STATE_RUNNING, STATE_APPLIED)


def claim(
    store,
    deal_id: str,
    *,
    source: str | None,
    pnl: float | None = None,
    model_version: str | None = None,
    checkpoint: Any = None,
) -> dict[str, Any] | None:
    """Claim learn lease. Returns attempt dict or None if already committed/in-flight."""
    if not may_teach(source):
        return None
    existing = get_attempt(store, deal_id)
    if existing and existing.get('state') == STATE_COMMITTED:
        return None
    if existing and existing.get('state') in (STATE_CLAIMED, STATE_RUNNING, STATE_APPLIED):
        # Incomplete prior attempt — mark recovery needed; do not silent-skip.
        if existing.get('needs_recovery'):
            return existing
        existing = dict(existing)
        existing['needs_recovery'] = True
        existing['at'] = _utc_now()
        store.set(_key(deal_id), existing)
        return existing
    attempt = {
        'deal_id': str(deal_id),
        'attempt_id': uuid4().hex,
        'state': STATE_CLAIMED,
        'source': source,
        'pnl': pnl,
        'model_version': model_version,
        'checkpoint': checkpoint,
        'at': _utc_now(),
        'needs_recovery': False,
    }
    store.set(_key(deal_id), attempt)
    # Keep legacy key in sync for old readers — claimed but not learned.
    store.set(LEGACY_LEARNED_PREFIX + str(deal_id), {
        'claimed': True, 'learned': False, 'at': attempt['at'],
        'attempt_id': attempt['attempt_id'], 'source': source, 'pnl': pnl,
        'model_version': model_version,
    })
    return attempt


def transition(
    store,
    deal_id: str,
    new_state: str,
    *,
    model_version: str | None = None,
    checkpoint: Any = None,
    error_code: str | None = None,
    pnl: float | None = None,
    source: str | None = None,
) -> dict[str, Any] | None:
    att = get_attempt(store, deal_id)
    if not isinstance(att, dict):
        return None
    att = dict(att)
    att['state'] = new_state
    att['at'] = _utc_now()
    if model_version is not None:
        att['model_version'] = model_version
    if checkpoint is not None:
        att['checkpoint'] = checkpoint
    if error_code is not None:
        att['error_code'] = error_code
    if pnl is not None:
        att['pnl'] = pnl
    if source is not None:
        att['source'] = source
    if new_state == STATE_COMMITTED:
        att['needs_recovery'] = False
        store.set(LEGACY_LEARNED_PREFIX + str(deal_id), {
            'claimed': True, 'learned': True, 'at': att['at'],
            'attempt_id': att.get('attempt_id'),
            'mode': att.get('mode'), 'pnl': att.get('pnl'),
            'source': att.get('source'),
            'model_version': att.get('model_version'),
        })
    store.set(_key(deal_id), att)
    return att


def recover_incomplete(store, deal_id: str) -> dict[str, Any] | None:
    """On restart: expose incomplete attempts so caller can replay or abort."""
    att = get_attempt(store, deal_id)
    if not att:
        return None
    if att.get('state') == STATE_COMMITTED:
        return att
    if att.get('state') in (STATE_CLAIMED, STATE_RUNNING, STATE_APPLIED):
        att = dict(att)
        att['needs_recovery'] = True
        att['at'] = _utc_now()
        store.set(_key(deal_id), att)
        return att
    return att


def abort(store, deal_id: str, *, error_code: str = 'aborted') -> dict[str, Any] | None:
    return transition(store, deal_id, STATE_ABORTED, error_code=error_code)
