"""V3 Capital Funds — immutable cash ledger (P09–P13, P15).

Store-backed (or in-memory dict) event log. Uniqueness by
(environment, account_id, cash_id). Engine wires broker ingest here later.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping

from .realized_cash import (
    classify_pnl_source,
    format_ui_amount,
    from_minor,
    parse_broker_cash_amount,
    to_minor,
)

STORE_KEY = 'cash_ledger_v1'
OUTBOX_KEY = 'cash_ledger_outbox_v1'
ADAPTER_VERSION = 'v3.0-funds-p0'

STATUS_SETTLED = 'SETTLED'
STATUS_SETTLED_ZERO = 'SETTLED_ZERO'
STATUS_PENDING = 'PENDING'
STATUS_MATCH_UNCERTAIN = 'MATCH_UNCERTAIN'
STATUS_UNMATCHED = 'UNMATCHED'
STATUS_QUARANTINE = 'QUARANTINE'
STATUS_UNVERIFIED = 'UNVERIFIED'
STATUS_ESTIMATE = 'ESTIMATE'
STATUS_SOURCE_CONFLICT = 'SOURCE_CONFLICT'

# Count toward account Funds sum (verified cash, including unmatched).
_SUMMABLE = frozenset({
    STATUS_SETTLED, STATUS_SETTLED_ZERO, STATUS_UNMATCHED, STATUS_MATCH_UNCERTAIN,
})
# Trade-result sum excludes fees/funding (T37).
_TRADE_TYPES = frozenset({'TRADE', 'CLOSE', 'PARTIAL', 'CORRECTION'})
_FEE_TYPES = frozenset({'COMMISSION', 'FEE', 'FUNDING', 'SWAP'})


class MemoryStore:
    """Dict-backed store for unit tests (get/set like engine.Store)."""

    def __init__(self, initial: dict | None = None):
        self._d: dict = dict(initial or {})

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _store_get(store, key, default=None):
    if store is None:
        return default
    if hasattr(store, 'get'):
        return store.get(key, default)
    if isinstance(store, Mapping):
        return store.get(key, default)
    return default


def _store_set(store, key, value):
    if store is None:
        raise TypeError('store is required')
    if hasattr(store, 'set'):
        store.set(key, value)
        return
    if isinstance(store, MutableMapping):
        store[key] = value
        return
    raise TypeError(f'unsupported store type: {type(store)!r}')


def raw_hash(raw) -> str:
    blob = json.dumps(raw, sort_keys=True, default=str, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def make_cash_id(*, environment: str, account_id: str, transaction_id: str) -> str:
    """Stable ledger key material (P09)."""
    env = str(environment or '').strip()
    acct = str(account_id or '').strip()
    tx = str(transaction_id or '').strip()
    if not tx:
        raise ValueError('transaction_id required for cash_id')
    return f'{env}|{acct}|{tx}'


def cash_event(
    *,
    account_id: str,
    environment: str = 'demo',
    cash_id: str | None = None,
    transaction_id: str | None = None,
    raw_hash: str | None = None,
    transaction_type: str = 'TRADE',
    close_proof: bool | None = None,
    status: str = STATUS_SETTLED,
    amount_minor: int | None = None,
    currency: str = 'GBP',
    effective_at: str | None = None,
    observed_at: str | None = None,
    source_field: str | None = 'pnl',
    source_schema: str = ADAPTER_VERSION,
    deal_link: str | None = None,
    revision: int = 1,
    raw: Any = None,
    note: str | None = None,
) -> dict:
    """Build a CashEvent dict (P11 schema)."""
    acct = str(account_id).strip()
    env = str(environment or 'demo').strip() or 'demo'
    tx = str(transaction_id).strip() if transaction_id else None
    cid = cash_id
    if not cid:
        if not tx:
            raise ValueError('cash_id or transaction_id required')
        cid = make_cash_id(environment=env, account_id=acct, transaction_id=tx)
    rh = raw_hash
    if rh is None and raw is not None:
        rh = globals()['raw_hash'](raw)
    st = str(status or STATUS_SETTLED).strip().upper()
    # Proven £0.00 close → SETTLED_ZERO (P10 / T30).
    if amount_minor == 0 and st in (STATUS_SETTLED, STATUS_SETTLED_ZERO):
        if close_proof is False:
            st = STATUS_PENDING
            amount_minor = None
        else:
            st = STATUS_SETTLED_ZERO
    # Pending placeholder must stay null, not zero.
    if st == STATUS_PENDING:
        amount_minor = None if amount_minor == 0 else amount_minor
    # No deal link on verified non-zero cash → UNMATCHED (still sums).
    # SETTLED_ZERO stays SETTLED_ZERO even when unmatched to a deal.
    if deal_link in (None, '') and st == STATUS_SETTLED:
        st = STATUS_UNMATCHED
    return {
        'account_id': acct,
        'environment': env,
        'cash_id': cid,
        'transaction_id': tx,
        'raw_hash': rh,
        'transaction_type': str(transaction_type or 'TRADE').strip().upper(),
        'close_proof': close_proof,
        'status': st,
        'amount_minor': amount_minor,
        'currency': str(currency or '').strip().upper() or None,
        'effective_at': effective_at,
        'observed_at': observed_at or _now_iso(),
        'source_field': source_field,
        'source_schema': source_schema,
        'deal_link': deal_link or None,
        'revision': int(revision or 1),
        'note': note,
    }


def load_events(store) -> list[dict]:
    rows = _store_get(store, STORE_KEY, None)
    if not rows:
        return []
    return [dict(r) for r in rows]


def _save_events(store, events: list[dict]) -> None:
    _store_set(store, STORE_KEY, list(events))


def load_outbox(store) -> list[dict]:
    rows = _store_get(store, OUTBOX_KEY, None)
    if not rows:
        return []
    return [dict(r) for r in rows]


def _save_outbox(store, rows: list[dict]) -> None:
    _store_set(store, OUTBOX_KEY, list(rows))


def _event_key(ev: Mapping) -> tuple:
    return (
        str(ev.get('environment') or ''),
        str(ev.get('account_id') or ''),
        str(ev.get('cash_id') or ''),
        int(ev.get('revision') or 1),
    )


def append_event(store, event: Mapping, *, outbox: bool = True) -> dict:
    """Append CashEvent idempotently. Duplicate cash_id+revision → no double count.

    If the same cash_id already exists but Capital changed the amount, apply a
    correction revision so Funds amendments are not silently ignored.
    """
    ev = dict(event)
    if not ev.get('cash_id'):
        raise ValueError('event.cash_id required')
    if not ev.get('account_id'):
        raise ValueError('event.account_id required')
    if 'observed_at' not in ev or not ev.get('observed_at'):
        ev['observed_at'] = _now_iso()
    if 'revision' not in ev or ev.get('revision') is None:
        ev['revision'] = 1
    events = load_events(store)
    key = _event_key(ev)
    cid = str(ev.get('cash_id') or '')
    new_minor = ev.get('amount_minor')
    # Same cash_id+revision OR same cash_id with amended Capital amount.
    if cid and new_minor is not None and str(
            ev.get('transaction_type') or '').upper() != 'CORRECTION':
        same = [e for e in events if str(e.get('cash_id') or '') == cid]
        if same:
            current = net_for_cash_id(store, cid)
            try:
                target = int(new_minor)
            except (TypeError, ValueError):
                target = None
            if target is not None and current is not None and current != target:
                return apply_correction(
                    store, cash_id=cid, corrected_amount_minor=target,
                    currency=ev.get('currency') or 'GBP',
                    note='capital_amount_amendment', outbox=outbox)
            # Identical net / exact key — no double count.
            for existing in same:
                if _event_key(existing) == key or (
                        target is not None and current == target):
                    return {
                        'appended': False,
                        'duplicate': True,
                        'event': dict(existing),
                        'outbox_enqueued': False,
                    }
    for existing in events:
        if _event_key(existing) == key:
            return {
                'appended': False,
                'duplicate': True,
                'event': dict(existing),
                'outbox_enqueued': False,
            }
    # Same cash_id newer revision without going through apply_correction:
    # still allow explicit revision rows (corrections).
    events.append(ev)
    _save_events(store, events)
    enqueued = False
    if outbox and ev.get('status') in _SUMMABLE and ev.get('amount_minor') is not None:
        enqueued = _enqueue_outbox(store, ev)
    return {
        'appended': True,
        'duplicate': False,
        'event': ev,
        'outbox_enqueued': enqueued,
    }


def _enqueue_outbox(store, ev: Mapping) -> bool:
    """Idempotent outbox by cash_id+revision (T39 crash safety)."""
    box = load_outbox(store)
    oid = f"{ev.get('cash_id')}|{int(ev.get('revision') or 1)}"
    for row in box:
        if row.get('outbox_id') == oid:
            return False
    box.append({
        'outbox_id': oid,
        'cash_id': ev.get('cash_id'),
        'revision': int(ev.get('revision') or 1),
        'account_id': ev.get('account_id'),
        'amount_minor': ev.get('amount_minor'),
        'currency': ev.get('currency'),
        'status': 'PENDING_TEACH',
        'enqueued_at': _now_iso(),
    })
    _save_outbox(store, box)
    return True


def ack_outbox(store, cash_id: str, revision: int = 1) -> bool:
    """Mark outbox row delivered — re-run after crash does not re-teach."""
    oid = f'{cash_id}|{int(revision)}'
    box = load_outbox(store)
    changed = False
    for row in box:
        if row.get('outbox_id') == oid and row.get('status') != 'DELIVERED':
            row['status'] = 'DELIVERED'
            row['delivered_at'] = _now_iso()
            changed = True
    if changed:
        _save_outbox(store, box)
    return changed


def pending_outbox(store) -> list[dict]:
    return [r for r in load_outbox(store) if r.get('status') == 'PENDING_TEACH']


def _in_period(ev: Mapping, period) -> bool:
    if period is None:
        return True
    ts = ev.get('effective_at') or ev.get('observed_at')
    if not ts:
        return False
    stamp = str(ts)[:19]
    if isinstance(period, (list, tuple)) and len(period) == 2:
        start, end = period
        if start and stamp < str(start)[:19]:
            return False
        if end and stamp > str(end)[:19]:
            return False
        return True
    if isinstance(period, Mapping):
        start = period.get('from') or period.get('start')
        end = period.get('to') or period.get('end')
        if start and stamp < str(start)[:19]:
            return False
        if end and stamp > str(end)[:19]:
            return False
        return True
    # Single UTC date prefix, e.g. '2026-11-24'
    return stamp.startswith(str(period)[:10])


def list_account_events(store, account_id: str, currency: str | None = None,
                        *, environment: str | None = None,
                        period=None, include_fees: bool = True) -> list[dict]:
    acct = str(account_id).strip()
    ccy = (currency or '').strip().upper() or None
    env = (environment or '').strip() or None
    out = []
    for ev in load_events(store):
        if str(ev.get('account_id') or '') != acct:
            continue
        if env and str(ev.get('environment') or '') != env:
            continue
        if ccy and str(ev.get('currency') or '').upper() != ccy:
            continue
        if not _in_period(ev, period):
            continue
        t = str(ev.get('transaction_type') or 'TRADE').upper()
        if not include_fees and t in _FEE_TYPES:
            continue
        out.append(ev)
    return out


def sum_account(store, account_id: str, currency: str, period=None,
                *, environment: str | None = None,
                include_fees: bool = True) -> int:
    """SUM unique verified event minors for account+currency (P13).

    PENDING / QUARANTINE / ESTIMATE excluded. UNMATCHED included once.
    """
    total = 0
    for ev in list_account_events(
            store, account_id, currency, environment=environment, period=period,
            include_fees=include_fees):
        st = str(ev.get('status') or '')
        if st not in _SUMMABLE:
            continue
        minor = ev.get('amount_minor')
        if minor is None:
            continue
        total += int(minor)
    return total


def sum_trade_results(store, account_id: str, currency: str, period=None,
                      *, environment: str | None = None) -> int:
    """Trade closes + corrections only — commissions stay separate (T37)."""
    total = 0
    for ev in list_account_events(
            store, account_id, currency, environment=environment, period=period,
            include_fees=False):
        t = str(ev.get('transaction_type') or 'TRADE').upper()
        if t in _FEE_TYPES:
            continue
        if t not in _TRADE_TYPES and t not in ('', 'TRADE'):
            # Allow unknown non-fee as trade unless marked fee.
            if t in ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER'):
                continue
        st = str(ev.get('status') or '')
        if st not in _SUMMABLE:
            continue
        minor = ev.get('amount_minor')
        if minor is None:
            continue
        total += int(minor)
    return total


def net_for_cash_id(store, cash_id: str) -> int | None:
    """Net minor units across revisions for one cash_id lineage."""
    total = 0
    found = False
    for ev in load_events(store):
        if str(ev.get('cash_id') or '') != str(cash_id):
            continue
        st = str(ev.get('status') or '')
        if st not in _SUMMABLE and st != STATUS_SETTLED:
            continue
        minor = ev.get('amount_minor')
        if minor is None:
            continue
        total += int(minor)
        found = True
    return total if found else None


def apply_correction(store, *, cash_id: str, corrected_amount_minor: int,
                     currency: str = 'GBP', observed_at: str | None = None,
                     note: str | None = None, outbox: bool = True) -> dict:
    """Append revision delta so lineage nets to corrected_amount_minor (P12/T34)."""
    events = [e for e in load_events(store) if str(e.get('cash_id') or '') == str(cash_id)]
    if not events:
        raise ValueError(f'unknown cash_id: {cash_id}')
    base = events[0]
    current = net_for_cash_id(store, cash_id)
    if current is None:
        current = 0
    delta = int(corrected_amount_minor) - int(current)
    if delta == 0:
        return {
            'appended': False,
            'duplicate': True,
            'event': events[-1],
            'net_minor': current,
            'delta_minor': 0,
        }
    rev = max(int(e.get('revision') or 1) for e in events) + 1
    # Correction keeps deal_link from latest non-empty.
    deal = None
    for e in reversed(events):
        if e.get('deal_link'):
            deal = e['deal_link']
            break
    st = STATUS_SETTLED_ZERO if corrected_amount_minor == 0 and deal else STATUS_SETTLED
    if deal is None and corrected_amount_minor == 0:
        st = STATUS_SETTLED_ZERO
    if deal is None and corrected_amount_minor != 0:
        st = STATUS_UNMATCHED
    elif deal is None:
        st = STATUS_UNMATCHED if corrected_amount_minor != 0 else STATUS_SETTLED_ZERO
    corr = cash_event(
        account_id=base['account_id'],
        environment=base.get('environment') or 'demo',
        cash_id=cash_id,
        transaction_id=base.get('transaction_id'),
        transaction_type='CORRECTION',
        close_proof=True,
        status=STATUS_SETTLED if delta != 0 else st,
        amount_minor=delta,
        currency=currency or base.get('currency') or 'GBP',
        effective_at=observed_at or _now_iso(),
        observed_at=observed_at,
        source_field='correction',
        deal_link=deal,
        revision=rev,
        note=note or f'correct_to_{corrected_amount_minor}',
        raw={'corrected_amount_minor': corrected_amount_minor, 'delta': delta},
    )
    # Force status: correction deltas are summable settled rows.
    if deal:
        corr['status'] = STATUS_SETTLED
    else:
        corr['status'] = STATUS_UNMATCHED
    if corrected_amount_minor == 0 and current + delta == 0:
        # Net zero after correction — delta row remains summable.
        pass
    result = append_event(store, corr, outbox=outbox)
    result['net_minor'] = net_for_cash_id(store, cash_id)
    result['delta_minor'] = delta
    return result


def link_deal(store, cash_id: str, deal_id: str) -> dict:
    """Attach unique deal proof; clears MATCH_UNCERTAIN / UNMATCHED (T32)."""
    deal = str(deal_id or '').strip()
    if not deal:
        raise ValueError('deal_id required')
    events = load_events(store)
    updated = None
    for ev in events:
        if str(ev.get('cash_id') or '') != str(cash_id):
            continue
        ev['deal_link'] = deal
        if ev.get('status') in (STATUS_UNMATCHED, STATUS_MATCH_UNCERTAIN):
            if ev.get('amount_minor') == 0:
                ev['status'] = STATUS_SETTLED_ZERO
            else:
                ev['status'] = STATUS_SETTLED
        updated = ev
    if updated is None:
        raise ValueError(f'unknown cash_id: {cash_id}')
    _save_events(store, events)
    return dict(updated)


def deal_realized(
    store,
    deal_id: str,
    *,
    currency: str | None = None,
    account_id: str | None = None,
    environment: str | None = None,
) -> dict:
    """Resolve deal PnL from ledger links.

    No unique deal_link → MATCH_UNCERTAIN (amount_minor null for teach).
    Multiple candidates without unique proof → MATCH_UNCERTAIN.
    Filters account_id + LIVE/DEMO environment when provided.
    """
    deal = str(deal_id or '').strip()
    acct = str(account_id).strip() if account_id else None
    env = str(environment).strip().lower() if environment else None
    matches = []
    uncertain = False
    for ev in load_events(store):
        if str(ev.get('deal_link') or '') != deal:
            continue
        # Never teach from synthetic learn:* duplicates (legacy double-count).
        txid = str(ev.get('transaction_id') or '')
        if txid.startswith('learn:'):
            continue
        if acct and str(ev.get('account_id') or '') not in ('', acct):
            continue
        if env and str(ev.get('environment') or '').lower() not in ('', env):
            continue
        if currency and str(ev.get('currency') or '').upper() != currency.upper():
            continue
        st = str(ev.get('status') or '')
        # Never teach from uncertain / unmatched / quarantine / estimate.
        if st in (STATUS_QUARANTINE, STATUS_PENDING, STATUS_ESTIMATE,
                  STATUS_UNVERIFIED, STATUS_MATCH_UNCERTAIN, STATUS_UNMATCHED,
                  STATUS_SOURCE_CONFLICT):
            if st == STATUS_MATCH_UNCERTAIN:
                uncertain = True
            continue
        if st not in (STATUS_SETTLED, STATUS_SETTLED_ZERO):
            continue
        matches.append(ev)
    if not matches:
        return {
            'deal_id': deal,
            'amount_minor': None,
            'currency': currency,
            'status': STATUS_MATCH_UNCERTAIN,
            'events': [],
            'may_teach': False,
            'uncertain_candidates': bool(uncertain),
        }
    total = sum(int(e['amount_minor']) for e in matches if e.get('amount_minor') is not None)
    ccy = matches[0].get('currency')
    return {
        'deal_id': deal,
        'amount_minor': total,
        'currency': ccy,
        'status': STATUS_SETTLED_ZERO if total == 0 else STATUS_SETTLED,
        'events': matches,
        'may_teach': True,
        'ui': format_ui_amount(total, ccy or 'GBP'),
    }


def unmatched_deal_pnl(candidates_without_link: list[Mapping]) -> dict:
    """When verified cash rows exist but no safe deal bind (T32)."""
    return {
        'amount_minor': None,
        'status': STATUS_MATCH_UNCERTAIN,
        'may_teach': False,
        'label': None,
        'candidate_cash_ids': [c.get('cash_id') for c in candidates_without_link],
    }


def ingest_funds_row(store, row: Mapping, *, account_id: str, environment: str = 'demo',
                     account_currency: str = 'GBP', deal_link: str | None = None,
                     transaction_id: str | None = None,
                     close_proof: bool = True,
                     opening: bool = False) -> dict:
    """Parse + append one Capital Funds / transactions row."""
    parsed = parse_broker_cash_amount(dict(row), currency_expected=account_currency)
    if parsed['status'] == 'QUARANTINE':
        ev = cash_event(
            account_id=account_id,
            environment=environment,
            transaction_id=transaction_id or str(
                row.get('reference') or row.get('transactionId') or raw_hash(row)[:16]),
            transaction_type=str(row.get('transactionType') or 'TRADE'),
            close_proof=False,
            status=STATUS_QUARANTINE,
            amount_minor=None,
            currency=parsed.get('currency') or account_currency,
            source_field=parsed.get('source_field'),
            deal_link=deal_link,
            raw=row,
            note=parsed.get('reason'),
        )
        return append_event(store, ev, outbox=False)

    if parsed['status'] == 'SOURCE_CONFLICT':
        ev = cash_event(
            account_id=account_id,
            environment=environment,
            transaction_id=transaction_id or str(
                row.get('reference') or row.get('transactionId') or raw_hash(row)[:16]),
            transaction_type=str(row.get('transactionType') or 'TRADE'),
            close_proof=False,
            status=STATUS_SOURCE_CONFLICT,
            amount_minor=None,
            currency=parsed.get('currency') or account_currency,
            source_field=parsed.get('source_field'),
            deal_link=deal_link,
            raw=row,
            note='source_conflict',
        )
        # SOURCE_CONFLICT is not in cash_event status normalizer — set after.
        ev['status'] = STATUS_SOURCE_CONFLICT
        return append_event(store, ev, outbox=False)

    if parsed['status'] == 'PENDING' or opening or not close_proof:
        ev = cash_event(
            account_id=account_id,
            environment=environment,
            transaction_id=transaction_id or str(
                row.get('reference') or row.get('transactionId') or raw_hash(row)[:16]),
            transaction_type=str(row.get('transactionType') or 'TRADE'),
            close_proof=False,
            status=STATUS_PENDING,
            amount_minor=None,
            currency=parsed.get('currency') or account_currency,
            effective_at=row.get('dateUtc') or row.get('dateUTC') or row.get('date'),
            source_field=parsed.get('source_field'),
            deal_link=deal_link,
            raw=row,
            note=parsed.get('reason') or 'pending',
        )
        return append_event(store, ev, outbox=False)

    ccy = parsed.get('currency') or account_currency
    # Foreign currency on account → do not relabel (P10/T33).
    if ccy and account_currency and ccy.upper() != account_currency.upper():
        ev = cash_event(
            account_id=account_id,
            environment=environment,
            transaction_id=transaction_id or str(
                row.get('reference') or row.get('transactionId') or raw_hash(row)[:16]),
            transaction_type=str(row.get('transactionType') or 'TRADE'),
            close_proof=close_proof,
            status=STATUS_PENDING,
            amount_minor=parsed.get('amount_minor'),
            currency=ccy,
            effective_at=row.get('dateUtc') or row.get('dateUTC') or row.get('date'),
            source_field=parsed.get('source_field'),
            deal_link=deal_link,
            raw=row,
            note='currency_mismatch_await_funds',
        )
        # Keep foreign amount visible but not as account GBP sum / teach.
        ev['status'] = STATUS_PENDING
        return append_event(store, ev, outbox=False)

    minor = parsed['amount_minor']
    tx_type = str(row.get('transactionType') or row.get('type') or 'TRADE').upper()
    st = STATUS_SETTLED_ZERO if minor == 0 else STATUS_SETTLED
    ev = cash_event(
        account_id=account_id,
        environment=environment,
        transaction_id=transaction_id or str(
            row.get('reference') or row.get('transactionId') or ''),
        transaction_type=tx_type,
        close_proof=True,
        status=st,
        amount_minor=minor,
        currency=ccy,
        effective_at=row.get('dateUtc') or row.get('dateUTC') or row.get('date'),
        source_field=parsed.get('source_field') or 'pnl',
        deal_link=deal_link,
        raw=row,
    )
    return append_event(store, ev, outbox=True)


def consumer_snapshot(store, account_id: str, currency: str = 'GBP',
                      period=None) -> dict:
    """Single sum for API / day_book / stats / teacher (P13/P17)."""
    trade = sum_trade_results(store, account_id, currency, period=period)
    full = sum_account(store, account_id, currency, period=period, include_fees=True)
    return {
        'account_id': account_id,
        'currency': currency,
        'trade_result_minor': trade,
        'account_net_minor': full,
        'ui_trade': format_ui_amount(trade, currency),
        'ui_net': format_ui_amount(full, currency),
        'events': list_account_events(store, account_id, currency, period=period),
        'pending_outbox': pending_outbox(store),
    }


# Re-exports for engine wiring convenience.
__all__ = [
    'ADAPTER_VERSION', 'MemoryStore', 'STORE_KEY', 'OUTBOX_KEY',
    'STATUS_SETTLED', 'STATUS_SETTLED_ZERO', 'STATUS_PENDING',
    'STATUS_MATCH_UNCERTAIN', 'STATUS_UNMATCHED', 'STATUS_QUARANTINE',
    'STATUS_UNVERIFIED', 'STATUS_ESTIMATE', 'STATUS_SOURCE_CONFLICT',
    'cash_event', 'make_cash_id', 'raw_hash',
    'append_event', 'apply_correction', 'link_deal', 'deal_realized',
    'unmatched_deal_pnl', 'sum_account', 'sum_trade_results',
    'net_for_cash_id', 'load_events', 'load_outbox', 'ack_outbox',
    'pending_outbox', 'ingest_funds_row', 'consumer_snapshot',
    'list_account_events', 'classify_pnl_source', 'to_minor', 'from_minor',
    'format_ui_amount', 'parse_broker_cash_amount',
]
