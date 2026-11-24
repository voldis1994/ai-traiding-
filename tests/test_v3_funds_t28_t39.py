"""V3 Capital Funds regression tests T28–T39 (fixtures only — no live Capital)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.cash_ledger import (
    STATUS_MATCH_UNCERTAIN,
    STATUS_PENDING,
    STATUS_QUARANTINE,
    STATUS_SETTLED,
    STATUS_SETTLED_ZERO,
    STATUS_SOURCE_CONFLICT,
    STATUS_UNMATCHED,
    MemoryStore,
    ack_outbox,
    append_event,
    apply_correction,
    cash_event,
    consumer_snapshot,
    deal_realized,
    ingest_funds_row,
    link_deal,
    load_events,
    load_outbox,
    net_for_cash_id,
    pending_outbox,
    sum_account,
    sum_trade_results,
    unmatched_deal_pnl,
)
from app.realized_cash import (
    account_currency_gate,
    classify_pnl_source,
    format_ui_amount,
    from_minor,
    is_forbidden_live_source,
    may_teach_live_cash,
    parse_broker_cash_amount,
    pick_live_realized,
    resolve_actual_exit_price,
    to_minor,
)


# --- helpers -----------------------------------------------------------------

def _funds(pnl, currency='GBP', **extra):
    row = {
        'reference': extra.pop('reference', 'TX1'),
        'pnl': pnl,
        'currency': currency,
        'transactionType': extra.pop('transactionType', 'TRADE'),
        'status': extra.pop('status', 'PROCESSED'),
        'note': extra.pop('note', 'Trade closed'),
    }
    row.update(extra)
    return row


# --- T28–T31: amount invariance ---------------------------------------------

def test_t28_verified_funds_wins_over_confirm_upl_proxy():
    """GBP +0.50 Funds; ignore confirm +0.48, UPL +0.81, mid proxy +0.73 → 50p."""
    picked = pick_live_realized(
        funds=_funds(Decimal('0.50'), reference='F50'),
        confirm_profit=Decimal('0.48'),
        last_upl=Decimal('0.81'),
        mid_proxy=Decimal('0.73'),
    )
    assert picked['status'] == 'OK'
    assert picked['amount_minor'] == 50
    assert picked['ui'] == '£0.50'
    assert picked['source'] == 'cash'

    store = MemoryStore()
    r = ingest_funds_row(
        store, _funds(Decimal('0.50'), reference='F50'),
        account_id='acc-gbp', account_currency='GBP', deal_link='deal-1')
    assert r['appended'] is True
    # Restart / re-ingest same cash_id must not double-count.
    r2 = ingest_funds_row(
        store, _funds(Decimal('0.50'), reference='F50'),
        account_id='acc-gbp', account_currency='GBP', deal_link='deal-1')
    assert r2['duplicate'] is True
    assert sum_account(store, 'acc-gbp', 'GBP') == 50
    snap = consumer_snapshot(store, 'acc-gbp', 'GBP')
    assert snap['trade_result_minor'] == 50
    assert snap['ui_trade'] == '£0.50'
    assert len(load_events(store)) == 1
    assert deal_realized(store, 'deal-1')['amount_minor'] == 50


def test_t29_sell_close_keeps_sign_ignores_proxy_and_quote_usd():
    """SELL close −0.50; GOLD quote USD / proxy +0.10 must not flip sign."""
    picked = pick_live_realized(
        funds=_funds(Decimal('-0.50'), reference='S50'),
        mid_proxy=Decimal('0.10'),
    )
    assert picked['amount_minor'] == -50
    assert from_minor(-50, 'GBP') == Decimal('-0.50')

    store = MemoryStore()
    ingest_funds_row(
        store, _funds(Decimal('-0.50'), reference='S50'),
        account_id='acc-gbp', account_currency='GBP', deal_link='sell-1')
    assert sum_account(store, 'acc-gbp', 'GBP') == -50
    # Quote currency on the instrument is not cash currency.
    gate = account_currency_gate(50, 'USD', 'GBP')
    assert gate['accepted'] is False
    assert classify_pnl_source('mid_proxy') == 'proxy'


def test_t30_settled_zero_vs_pending_placeholder():
    """Final close £0.00 → SETTLED_ZERO; opening/pending £0.00 → null, no teach."""
    store = MemoryStore()
    r = ingest_funds_row(
        store,
        _funds(Decimal('0.00'), reference='Z0', note='Trade closed'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-zero',
        close_proof=True)
    assert r['event']['status'] == STATUS_SETTLED_ZERO
    assert r['event']['amount_minor'] == 0
    assert sum_account(store, 'acc-gbp', 'GBP') == 0
    assert deal_realized(store, 'd-zero')['amount_minor'] == 0

    store2 = MemoryStore()
    r_open = ingest_funds_row(
        store2,
        {
            'reference': 'OPEN1',
            'pnl': 0.0,
            'currency': 'GBP',
            'transactionType': 'TRADE',
            'note': 'Trade opened',
            'placeholder': True,
            'status': 'PROCESSED',
        },
        account_id='acc-gbp', account_currency='GBP', opening=True,
        close_proof=False)
    assert r_open['event']['status'] == STATUS_PENDING
    assert r_open['event']['amount_minor'] is None
    assert sum_account(store2, 'acc-gbp', 'GBP') == 0
    assert may_teach_live_cash('pending_broker_pnl') is False
    assert pending_outbox(store2) == []


def test_t31_upl_confirm_activity_without_funds_are_null():
    """UPL / confirm.profit / activity without Funds → null; no cash event."""
    for kwargs in (
        dict(last_upl=Decimal('0.50')),
        dict(confirm_profit=Decimal('0.50')),
        dict(funds=None, source='upl'),
        dict(source='confirm.profit'),
        dict(source=''),
        dict(source='blank'),
    ):
        picked = pick_live_realized(**kwargs)
        assert picked['amount_minor'] is None
        assert picked['status'] in ('PENDING', 'QUARANTINE')

    assert is_forbidden_live_source('upl')
    assert is_forbidden_live_source('confirm.profit')
    assert is_forbidden_live_source('')
    assert classify_pnl_source('') == 'unverified'
    assert classify_pnl_source(None) == 'unverified'
    assert classify_pnl_source('upl_estimate') == 'estimate'
    assert may_teach_live_cash('') is False
    assert may_teach_live_cash('broker') is True

    # Activity row without whitelist cash field → pending, not invent.
    parsed = parse_broker_cash_amount({
        'activity': True, 'dealId': 'x', 'details': {'profit': 0.50},
    })
    assert parsed['status'] in ('PENDING', 'QUARANTINE')
    assert parsed['amount_minor'] is None


# --- T32–T35: identity, currency, precision ---------------------------------

def test_t32_two_closes_without_deal_link_match_uncertain():
    """Two similar GOLD closes without safe deal bind → account sum once each;
    deal PnL MATCH_UNCERTAIN; after unique ID link, teach correctly."""
    store = MemoryStore()
    a = ingest_funds_row(
        store, _funds(Decimal('0.50'), reference='G-A'),
        account_id='acc-gbp', account_currency='GBP', deal_link=None)
    b = ingest_funds_row(
        store, _funds(Decimal('-0.30'), reference='G-B'),
        account_id='acc-gbp', account_currency='GBP', deal_link=None)
    assert a['event']['status'] == STATUS_UNMATCHED
    assert b['event']['status'] == STATUS_UNMATCHED
    assert sum_account(store, 'acc-gbp', 'GBP') == 20  # +50 -30

    uncertain = unmatched_deal_pnl([a['event'], b['event']])
    assert uncertain['status'] == STATUS_MATCH_UNCERTAIN
    assert uncertain['amount_minor'] is None
    assert uncertain['may_teach'] is False
    assert uncertain['label'] is None

    # Still no deal_realized until linked.
    assert deal_realized(store, 'mystery')['status'] == STATUS_MATCH_UNCERTAIN

    link_deal(store, a['event']['cash_id'], 'deal-gold-a')
    link_deal(store, b['event']['cash_id'], 'deal-gold-b')
    assert deal_realized(store, 'deal-gold-a')['amount_minor'] == 50
    assert deal_realized(store, 'deal-gold-b')['amount_minor'] == -30


def test_t33_usd_or_unknown_currency_not_relabeled_gbp():
    """USD 0.50 or unknown ccy on GBP account → not GBP 0.50; await Funds GBP."""
    store = MemoryStore()
    r = ingest_funds_row(
        store, _funds(Decimal('0.50'), currency='USD', reference='U50'),
        account_id='acc-gbp', account_currency='GBP')
    assert r['event']['status'] == STATUS_PENDING
    assert r['event']['currency'] == 'USD'
    assert sum_account(store, 'acc-gbp', 'GBP') == 0
    assert sum_account(store, 'acc-gbp', 'USD') == 0  # pending excluded

    gate = account_currency_gate(50, 'USD', 'GBP')
    assert gate['accepted'] is False
    assert gate['reason'] == 'currency_mismatch_await_funds'

    unknown = account_currency_gate(50, None, 'GBP')
    assert unknown['accepted'] is False
    parsed = parse_broker_cash_amount('0.50')  # no currency proof
    assert parsed['status'] == 'QUARANTINE'
    assert parsed['reason'] == 'missing_currency'


def test_t34_corrections_exact_pence_no_epsilon():
    """+£0.49 → +£0.50 → +£0.48 via revision deltas; net exactly 48 pence."""
    store = MemoryStore()
    r = ingest_funds_row(
        store, _funds(Decimal('0.49'), reference='C49'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-corr')
    cid = r['event']['cash_id']
    assert net_for_cash_id(store, cid) == 49

    c1 = apply_correction(store, cash_id=cid, corrected_amount_minor=50)
    assert c1['delta_minor'] == 1
    assert c1['net_minor'] == 50
    assert sum_account(store, 'acc-gbp', 'GBP') == 50

    c2 = apply_correction(store, cash_id=cid, corrected_amount_minor=48)
    assert c2['delta_minor'] == -2
    assert c2['net_minor'] == 48
    snap = consumer_snapshot(store, 'acc-gbp', 'GBP')
    assert snap['trade_result_minor'] == 48
    assert snap['ui_trade'] == '£0.48'
    # Duplicate correction to same amount → no extra row.
    c3 = apply_correction(store, cash_id=cid, corrected_amount_minor=48)
    assert c3['duplicate'] is True
    assert sum_account(store, 'acc-gbp', 'GBP') == 48
    assert deal_realized(store, 'd-corr')['amount_minor'] == 48


def test_t35_duplicate_restart_and_per_account_isolation():
    """Duplicate/restart/overlap; same reference on two accounts → once each."""
    store = MemoryStore()
    row = _funds(Decimal('0.25'), reference='SAME-REF')
    ingest_funds_row(store, row, account_id='acc-a', environment='demo',
                     account_currency='GBP')
    ingest_funds_row(store, row, account_id='acc-a', environment='demo',
                     account_currency='GBP')
    ingest_funds_row(store, row, account_id='acc-b', environment='demo',
                     account_currency='GBP')
    assert sum_account(store, 'acc-a', 'GBP') == 25
    assert sum_account(store, 'acc-b', 'GBP') == 25
    assert len(load_events(store)) == 2

    # Explicit append_event idempotency.
    ev = cash_event(
        account_id='acc-a', environment='demo', transaction_id='SAME-REF',
        amount_minor=25, currency='GBP', close_proof=True, deal_link='x')
    again = append_event(store, ev)
    assert again['duplicate'] is True


# --- T36–T39: fill, partials, errors, consumers ------------------------------

def test_t36_fill_and_funds_are_independent():
    """actual fill 103.15 + Funds −£0.50; missing fill → null exit, cash still −50."""
    exit_px = resolve_actual_exit_price(actual_fill=Decimal('103.15'), mid=103.40)
    assert exit_px == Decimal('103.15')
    missing = resolve_actual_exit_price(actual_fill=None, mid=103.40, sl_tp=103.00)
    assert missing is None

    store = MemoryStore()
    ingest_funds_row(
        store, _funds(Decimal('-0.50'), reference='FILL1'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-fill')
    realized = deal_realized(store, 'd-fill')
    assert realized['amount_minor'] == -50
    # No path / proxy label for LIVE cash.
    assert classify_pnl_source('ticks_only') == 'proxy'
    assert may_teach_live_cash('ticks_only') is False


def test_t37_partials_and_separate_commission():
    """Partials +0.20 +0.30 = 50; duplicate partial no double; fee separate."""
    store = MemoryStore()
    ingest_funds_row(
        store, _funds(Decimal('0.20'), reference='P1', transactionType='PARTIAL'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-part')
    ingest_funds_row(
        store, _funds(Decimal('0.30'), reference='P2', transactionType='PARTIAL'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-part')
    # Repeat partial row.
    ingest_funds_row(
        store, _funds(Decimal('0.20'), reference='P1', transactionType='PARTIAL'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-part')
    assert sum_trade_results(store, 'acc-gbp', 'GBP') == 50
    assert deal_realized(store, 'd-part')['amount_minor'] == 50

    ingest_funds_row(
        store,
        _funds(Decimal('-0.05'), reference='FEE1', transactionType='COMMISSION'),
        account_id='acc-gbp', account_currency='GBP')
    assert sum_trade_results(store, 'acc-gbp', 'GBP') == 50
    assert sum_account(store, 'acc-gbp', 'GBP', include_fees=True) == 45


def test_t38_legacy_upl_and_blank_then_funds():
    """Legacy UPL £0.80 + blank source → nothing; later Funds +£0.50 → only 50."""
    assert classify_pnl_source('') == 'unverified'
    assert classify_pnl_source('upl') in ('estimate', 'unverified')
    picked = pick_live_realized(last_upl=Decimal('0.80'), source='')
    assert picked['amount_minor'] is None
    assert may_teach_live_cash(picked.get('source')) is False

    store = MemoryStore()
    assert sum_account(store, 'acc-gbp', 'GBP') == 0
    ingest_funds_row(
        store, _funds(Decimal('0.50'), reference='LATE50'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-late')
    assert sum_account(store, 'acc-gbp', 'GBP') == 50
    assert len(load_events(store)) == 1


def test_t39_quarantine_and_outbox_crash_no_double_teach():
    """Bad field / ambiguous locale / conflict / pending → quarantine; outbox idempotent."""
    bad = parse_broker_cash_amount('1,234')
    assert bad['status'] == 'QUARANTINE'
    assert bad['reason'] == 'ambiguous_locale'
    assert bad['amount_minor'] is None

    nan = parse_broker_cash_amount(float('nan'), currency_expected='GBP')
    assert nan['status'] == 'QUARANTINE'

    conflict = parse_broker_cash_amount({
        'pnl': Decimal('0.50'),
        'profitAndLoss': Decimal('0.40'),
        'currency': 'GBP',
        'status': 'PROCESSED',
    })
    assert conflict['status'] == 'SOURCE_CONFLICT'

    pending = parse_broker_cash_amount({
        'pnl': Decimal('0.50'), 'currency': 'GBP', 'status': 'PENDING',
    })
    assert pending['status'] == 'PENDING'

    store = MemoryStore()
    q = ingest_funds_row(
        store, {'reference': 'BAD', 'pnl': '1,234', 'currency': 'GBP'},
        account_id='acc-gbp', account_currency='GBP')
    assert q['event']['status'] == STATUS_QUARANTINE
    assert sum_account(store, 'acc-gbp', 'GBP') == 0

    # Happy path outbox: crash between ledger write and teach ack.
    r = ingest_funds_row(
        store, _funds(Decimal('0.12'), reference='OBX'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-obx')
    assert r['outbox_enqueued'] is True
    assert len(pending_outbox(store)) == 1
    # Replay append (restart) — still one event, outbox not duplicated.
    r2 = ingest_funds_row(
        store, _funds(Decimal('0.12'), reference='OBX'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-obx')
    assert r2['duplicate'] is True
    assert len(pending_outbox(store)) == 1
    assert sum_account(store, 'acc-gbp', 'GBP') == 12
    ack_outbox(store, r['event']['cash_id'], revision=1)
    assert pending_outbox(store) == []
    # Re-ack / re-enqueue path after deliver: duplicate ingest still no teach row.
    ingest_funds_row(
        store, _funds(Decimal('0.12'), reference='OBX'),
        account_id='acc-gbp', account_currency='GBP', deal_link='d-obx')
    assert pending_outbox(store) == []
    assert len([e for e in load_outbox(store)
                if e.get('cash_id') == r['event']['cash_id']]) == 1


# --- unit smoke for money helpers --------------------------------------------

def test_to_minor_from_minor_gbp():
    assert to_minor(Decimal('0.50'), 'GBP') == 50
    assert to_minor('-0.50', 'GBP') == -50
    assert to_minor(0.5, 'GBP') == 50
    assert to_minor(float('nan'), 'GBP') is None
    assert to_minor(float('inf'), 'GBP') is None
    assert from_minor(50, 'GBP') == Decimal('0.50')
    assert format_ui_amount(None, 'GBP') is None
    assert format_ui_amount(0, 'GBP') == '£0.00'


def test_parse_ok_pound_string():
    p = parse_broker_cash_amount('£0.50')
    assert p['status'] == 'OK'
    assert p['amount_minor'] == 50
    assert p['currency'] == 'GBP'
