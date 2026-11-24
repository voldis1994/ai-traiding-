"""LIVE system diagnostics — measure, do not invent trades (PR #303 FINAL).

Reports P&L agreement with Cash Ledger, Capital API load, event-loop lag,
full-cycle completion, and unfinished-task reasons from existing store state.
"""
from __future__ import annotations

import time
from typing import Any


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def pnl_ledger_agreement(store, *, account_id: str | None = None,
                         currency: str | None = None,
                         environment: str | None = None) -> dict:
    """Compare journal closed £ vs authoritative Cash Ledger aggregator."""
    from . import cash_ledger
    from .realized_cash import from_minor

    acct = str(account_id or store.get('capital_account_id') or 'live')
    ccy = str(currency or store.get('account_currency') or 'GBP').upper()
    env = str(environment or store.get('capital_environment') or 'live').lower()
    by_deal = cash_ledger.aggregate_verified_by_deal(
        store, account_id=acct, currency=ccy, environment=env)
    rows = store.db.execute(
        "SELECT deal_id,pnl,pnl_source,pnl_currency FROM trades "
        "WHERE closed IS NOT NULL").fetchall()
    matched = 0
    mismatched = []
    journal_only = []
    ledger_only = set(by_deal.keys())
    for deal_id, pnl, src, jccy in rows:
        deal = str(deal_id)
        ledger_only.discard(deal)
        agg = by_deal.get(deal)
        if agg is None:
            if src in ('broker', 'live_funds_mirror', 'cash_ledger'):
                journal_only.append({
                    'deal_id': deal, 'pnl': pnl, 'pnl_source': src,
                    'reason': 'journal_without_ledger',
                })
            continue
        try:
            ledger_major = float(from_minor(int(agg['amount_minor']),
                                            agg.get('currency') or ccy))
        except Exception:
            continue
        if pnl is None:
            mismatched.append({
                'deal_id': deal, 'journal': None, 'ledger': ledger_major,
                'reason': 'journal_null',
            })
            continue
        if abs(float(pnl) - ledger_major) < 0.005:
            matched += 1
        else:
            mismatched.append({
                'deal_id': deal, 'journal': float(pnl), 'ledger': ledger_major,
                'reason': 'amount_diff',
            })
    return {
        'account_id': acct,
        'currency': ccy,
        'environment': env,
        'matched': matched,
        'mismatched_n': len(mismatched),
        'mismatched': mismatched[:40],
        'journal_only_n': len(journal_only),
        'journal_only': journal_only[:40],
        'ledger_only_n': len(ledger_only),
        'ledger_only': sorted(ledger_only)[:40],
        'ledger_deals': len(by_deal),
    }


def api_load_snapshot(broker=None, rest_manager=None) -> dict:
    """Capital REST load from the shared rate manager (existing traffic only)."""
    mgr = rest_manager
    if mgr is None and broker is not None:
        mgr = getattr(broker, 'rest', None) or getattr(broker, '_rest', None)
    if mgr is None:
        return {'available': False}
    stats = dict(getattr(mgr, 'stats', {}) or {})
    cps = 0.0
    if hasattr(mgr, 'calls_per_second'):
        try:
            cps = float(mgr.calls_per_second(1.0))
        except Exception:
            cps = 0.0
    lat = list(getattr(mgr, 'latency_ms', []) or [])
    p50 = sorted(lat)[len(lat) // 2] if lat else None
    from . import capital_rest as _cr
    max_ps = int(getattr(_cr, 'MAX_PER_SECOND', 10) or 10)
    return {
        'available': True,
        'calls_per_second': cps,
        'max_per_second': max_ps,
        'under_limit': cps < float(max_ps),
        'stats': stats,
        'latency_p50_ms': p50,
        'latency_samples': len(lat),
    }


def event_loop_lag(samples: int = 5, sleep_s: float = 0.01) -> dict:
    """Measure event-loop scheduling lag with short sleeps (no broker calls)."""
    import asyncio

    async def _run():
        lags = []
        for _ in range(max(1, int(samples))):
            t0 = time.monotonic()
            await asyncio.sleep(sleep_s)
            lags.append(max(0.0, (time.monotonic() - t0) - sleep_s))
        return {
            'samples': len(lags),
            'lag_max_s': max(lags) if lags else 0.0,
            'lag_mean_s': (sum(lags) / len(lags)) if lags else 0.0,
            'lag_p95_s': sorted(lags)[int(0.95 * (len(lags) - 1))] if lags else 0.0,
        }

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Caller is already async — return a sync estimate via time.
            lags = []
            for _ in range(max(1, int(samples))):
                t0 = time.monotonic()
                time.sleep(sleep_s)
                lags.append(max(0.0, (time.monotonic() - t0) - sleep_s))
            return {
                'samples': len(lags),
                'lag_max_s': max(lags) if lags else 0.0,
                'lag_mean_s': (sum(lags) / len(lags)) if lags else 0.0,
                'mode': 'sync_fallback',
            }
        return asyncio.run(_run())
    except Exception as exc:
        return {'error': type(exc).__name__}


def cycle_completion(store) -> dict:
    """Full-cycle COMPLETE rate + unfinished reasons from FSM + learn attempts."""
    import json

    from . import learn_attempt as la
    from . import trade_cycle_fsm as fsm
    from . import work_queue as wq

    cycles = []
    try:
        rows = store.db.execute(
            "SELECT key,value FROM state WHERE key LIKE 'trade_cycle:%'"
        ).fetchall()
        for _key, val in rows:
            try:
                cyc = json.loads(val) if isinstance(val, str) else val
            except Exception:
                continue
            if isinstance(cyc, dict) and cyc.get('cycle_id'):
                cycles.append(cyc)
    except Exception:
        cycles = []
    complete = 0
    incomplete = []
    for cyc in cycles:
        if cyc.get('complete'):
            complete += 1
            continue
        stages = cyc.get('stages') or {}
        pending = [
            name for name, st in stages.items()
            if isinstance(st, dict)
            and st.get('status') not in (fsm.STATUS_DONE, fsm.STATUS_SKIPPED)
        ]
        skip_bad = [
            name for name, st in stages.items()
            if isinstance(st, dict)
            and st.get('status') == fsm.STATUS_SKIPPED
            and not str(st.get('proof') or st.get('skip_reason') or '').strip()
        ]
        incomplete.append({
            'cycle_id': cyc.get('cycle_id'),
            'deal_id': cyc.get('deal_id'),
            'pending_stages': pending,
            'skipped_without_reason': skip_bad,
        })

    learn_incomplete = []
    terminal = {
        la.STATE_COMMITTED,
        getattr(la, 'STATE_COMPLETE', 'COMPLETE'),
        la.STATE_ABORTED,
        getattr(la, 'STATE_FAILED', 'FAILED'),
    }
    try:
        rows = store.db.execute(
            "SELECT key,value FROM state WHERE key LIKE 'learn_attempt:%'"
        ).fetchall()
        for key, val in rows:
            try:
                att = json.loads(val) if isinstance(val, str) else val
            except Exception:
                continue
            if not isinstance(att, dict):
                continue
            st = str(att.get('state') or '')
            if st and st not in terminal:
                learn_incomplete.append({
                    'deal_id': str(key).split(':', 1)[-1],
                    'state': st,
                    'attempt_id': att.get('attempt_id'),
                })
    except Exception:
        pass

    queue_pending = []
    try:
        wq.ensure_schema(store)
        rows = store.db.execute(
            "SELECT id,kind,status,last_error,payload FROM work_queue "
            "WHERE status IN ('PENDING','RUNNING','FAILED') "
            "ORDER BY id DESC LIMIT 80"
        ).fetchall()
        for wid, kind, status, err, payload in rows:
            try:
                data = json.loads(payload or '{}')
            except Exception:
                data = {}
            queue_pending.append({
                'id': wid,
                'kind': kind,
                'status': status,
                'last_error': err,
                'deal_id': (data or {}).get('deal_id') if isinstance(data, dict) else None,
            })
    except Exception:
        pass

    return {
        'cycles_seen': len(cycles),
        'cycles_complete': complete,
        'cycles_incomplete_n': len(incomplete),
        'cycles_incomplete': incomplete[:40],
        'learn_incomplete_n': len(learn_incomplete),
        'learn_incomplete': learn_incomplete[:40],
        'queue_unfinished_n': len(queue_pending),
        'queue_unfinished': queue_pending[:40],
    }


def collect(store, *, broker=None, account_id=None, currency=None,
            environment=None) -> dict[str, Any]:
    """One diagnostic blob for LIVE health — uses existing data only."""
    return {
        'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'pnl_agreement': pnl_ledger_agreement(
            store, account_id=account_id, currency=currency,
            environment=environment),
        'api_load': api_load_snapshot(broker=broker),
        'event_loop': event_loop_lag(),
        'cycle_completion': cycle_completion(store),
    }
