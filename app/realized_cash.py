"""V3 Capital Funds — RealizedCash helpers (P05–P10, P14).

LIVE realized money is verified broker Funds only. Decimal / minor units;
blank source is never cash. Engine may call these without importing ledger.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

CURRENCY_EXPONENT: dict[str, int] = {
    'GBP': 2, 'USD': 2, 'EUR': 2, 'AUD': 2, 'CAD': 2, 'CHF': 2,
    'NZD': 2, 'SGD': 2, 'HKD': 2, 'SEK': 2, 'NOK': 2, 'DKK': 2,
    'JPY': 0, 'PLN': 2, 'CZK': 2, 'HUF': 2, 'TRY': 2, 'ZAR': 2,
}

# Explicit LIVE realized forgeries (P07). Matching is case-insensitive.
FORBIDDEN_LIVE_REALIZED_SOURCES = frozenset({
    'upl', 'upl_estimate', 'last_upl', 'open_upl',
    'accounts.balance.profitloss', 'profitloss', 'balance_profitloss',
    'confirm.profit', 'confirm_profit', 'confirmation.profit',
    'mid_x_size', 'mid×size', 'midxsize', 'mid_proxy', 'price_size',
    'bid_ask_x_size', 'bid/ask×size', 'ticks', 'ticks_only',
    'paper', 'paper_pnl', 'scratch', 'fx_estimate', 'currency_estimate',
    'estimate', 'diagnostic_estimate', 'blank', 'other_trade',
    '±1', '+1', '-1', 'label', 'position_disappeared',
})

_CASH_SOURCES = frozenset({
    'broker', 'funds', 'capital', 'live_funds_mirror', 'cash_ledger',
    'capital_funds', 'verified_funds',
})
_PENDING_SOURCES = frozenset({
    'pending_close_sync', 'pending_broker_pnl', 'pending_upl_teach',
    'pending_funds', 'pending',
})
_PROXY_SOURCES = frozenset({
    'ticks_only', 'price_size', 'position_disappeared', 'mid_proxy',
    'mid_x_size', 'bid_ask_x_size',
})
_ESTIMATE_SOURCES = frozenset({
    'upl_estimate', 'estimate', 'diagnostic_estimate', 'fx_estimate',
    'currency_estimate', 'scratch',
})
_UNVERIFIED_SOURCES = frozenset({
    'unverified', 'legacy', 'legacy_broker', 'blank', 'none', 'null',
})

# Cash field whitelist — ordered preference, versioned (P08). No invent scan.
_CASH_FIELD_WHITELIST = (
    'pnl', 'profitAndLoss', 'profit_and_loss', 'realizedPnl', 'realized_pnl',
)

_CURRENCY_SYMBOLS = {
    '£': 'GBP', '$': 'USD', '€': 'EUR', '¥': 'JPY',
}

_AMBIGUOUS_GROUPING = re.compile(
    r'^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$'  # 1,234 or 1,234.56 — locale unclear
)
_EU_DECIMAL_COMMA = re.compile(r'^[+-]?\d+,\d{1,2}$')  # 0,50 with ≤2 dp
_PLAIN_NUMBER = re.compile(r'^[+-]?\d+(\.\d+)?$')


def currency_exponent(currency: str | None) -> int | None:
    if currency is None:
        return None
    c = str(currency).strip().upper()
    if not c:
        return None
    return CURRENCY_EXPONENT.get(c)


def to_minor(amount, currency: str = 'GBP') -> int | None:
    """Major units → integer minor (pence). None on NaN / bad / unknown ccy."""
    exp = currency_exponent(currency)
    if exp is None:
        return None
    try:
        if isinstance(amount, Decimal):
            d = amount
        elif isinstance(amount, bool):
            return None
        elif isinstance(amount, int):
            d = Decimal(amount)
        elif isinstance(amount, float):
            if not math.isfinite(amount):
                return None
            # Prefer exact decimal text when float is a clean binary value.
            d = Decimal(str(amount))
        elif isinstance(amount, str):
            text = amount.strip()
            if not text or text.lower() in ('nan', 'inf', '+inf', '-inf',
                                            'infinity', '+infinity', '-infinity'):
                return None
            d = Decimal(text)
        else:
            d = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None
    if not d.is_finite():
        return None
    scale = Decimal(10) ** exp
    scaled = d * scale
    # Exact minor only — reject non-representable precision (P06).
    try:
        as_int = int(scaled)
    except (ValueError, OverflowError):
        return None
    if scaled != Decimal(as_int):
        # Allow exact half-even only when already on the minor grid after
        # quantize to exponent (API floats like 0.1 → Decimal('0.1') ok).
        q = d.quantize(Decimal(10) ** -exp, rounding=ROUND_HALF_EVEN)
        if q != d and abs(d - q) > Decimal('0'):
            # Float dust: accept if string path already normalized; else if
            # difference is pure binary noise under half a minor unit.
            if abs(scaled - Decimal(as_int)) >= Decimal('0.5'):
                return None
            # Re-quantize major then convert.
            d = q
            scaled = d * scale
            as_int = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
            if (d * scale) != Decimal(as_int) and abs(
                    d * scale - Decimal(as_int)) >= Decimal('0.5'):
                return None
            return as_int
        as_int = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
    return as_int


def from_minor(minor, currency: str = 'GBP') -> Decimal:
    """Integer minor → Decimal major units."""
    exp = currency_exponent(currency)
    if exp is None:
        raise ValueError(f'unknown currency: {currency!r}')
    if minor is None:
        raise ValueError('minor is None')
    return Decimal(int(minor)) / (Decimal(10) ** exp)


def format_ui_amount(minor: int | None, currency: str = 'GBP') -> str | None:
    """UI string: GBP → £0.50; null stays null (not £0.00)."""
    if minor is None:
        return None
    ccy = (currency or 'GBP').strip().upper()
    major = from_minor(minor, ccy)
    exp = currency_exponent(ccy) or 2
    text = f'{major:.{exp}f}'
    if ccy == 'GBP':
        return f'£{text}'
    if ccy == 'USD':
        return f'${text}'
    if ccy == 'EUR':
        return f'€{text}'
    return f'{text} {ccy}'


def is_forbidden_live_source(source) -> bool:
    s = (source or '').strip().lower()
    if not s:
        return True  # blank is forbidden as LIVE realized (P07 / T38)
    if s in FORBIDDEN_LIVE_REALIZED_SOURCES:
        return True
    # Substring forms used in legacy journals.
    for bad in ('upl', 'confirm.profit', 'paper', 'ticks_only', 'mid_x_size'):
        if bad in s:
            return True
    return False


def classify_pnl_source(source) -> str:
    """Map journal/source flag → cash|pending|estimate|proxy|unverified|other.

    Blank / empty is NOT cash (P05/P06, T38).
    """
    if source is None:
        return 'unverified'
    s = str(source).strip().lower()
    if not s:
        return 'unverified'
    if s in _PENDING_SOURCES or s.startswith('pending'):
        return 'pending'
    if s in _PROXY_SOURCES:
        return 'proxy'
    if s in _ESTIMATE_SOURCES or 'estimate' in s:
        return 'estimate'
    if s in _UNVERIFIED_SOURCES or s in ('blank',):
        return 'unverified'
    if s in FORBIDDEN_LIVE_REALIZED_SOURCES:
        if 'upl' in s or 'estimate' in s:
            return 'estimate'
        if s in _PROXY_SOURCES:
            return 'proxy'
        return 'unverified'
    if s in _CASH_SOURCES:
        return 'cash'
    return 'other'


def may_teach_live_cash(source) -> bool:
    """LIVE cash teacher only from verified cash sources."""
    return classify_pnl_source(source) == 'cash'


def resolve_actual_exit_price(*, actual_fill=None, mid=None, sl_tp=None,
                              bid=None, ask=None):
    """P05: only broker fill is actual_exit_price; mid/SL/TP are estimates."""
    for candidate in (actual_fill,):
        if candidate is None or candidate == '':
            continue
        try:
            d = candidate if isinstance(candidate, Decimal) else Decimal(str(candidate))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if d.is_finite():
            return d
    return None


def diagnostic_exit_estimate(*, mid=None, sl_tp=None, bid=None, ask=None):
    """Non-authoritative exit guess for diagnostics only — never teach-as-fill."""
    for candidate in (mid, sl_tp, bid, ask):
        if candidate is None or candidate == '':
            continue
        try:
            d = candidate if isinstance(candidate, Decimal) else Decimal(str(candidate))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if d.is_finite():
            return d
    return None


def _strip_currency_prefix(text: str) -> tuple[str, str | None]:
    t = text.strip()
    ccy = None
    for sym, code in _CURRENCY_SYMBOLS.items():
        if t.startswith(sym):
            ccy = code
            t = t[len(sym):].strip()
            break
    # Trailing ISO code: "0.50 GBP"
    parts = t.split()
    if len(parts) == 2 and parts[1].isalpha() and len(parts[1]) == 3:
        ccy = parts[1].upper()
        t = parts[0]
    return t, ccy


def _parse_number_text(text: str) -> tuple[Decimal | None, str | None]:
    """Return (value, reject_reason). reject_reason set → quarantine."""
    raw = text.strip()
    if not raw:
        return None, 'empty'
    low = raw.lower()
    if low in ('nan', 'inf', '+inf', '-inf', 'infinity', '+infinity', '-infinity'):
        return None, 'non_finite'
    cleaned, _ = _strip_currency_prefix(raw)
    cleaned = cleaned.replace(' ', '')
    if not cleaned:
        return None, 'empty'
    # Ambiguous thousands grouping without locale proof (T39 / P06).
    if _AMBIGUOUS_GROUPING.match(cleaned):
        return None, 'ambiguous_locale'
    if _EU_DECIMAL_COMMA.match(cleaned):
        # Only accept comma as decimal when ≤2 fractional digits (common EU cash).
        cleaned = cleaned.replace(',', '.')
    elif ',' in cleaned and '.' in cleaned:
        return None, 'ambiguous_locale'
    elif ',' in cleaned:
        return None, 'ambiguous_locale'
    if not _PLAIN_NUMBER.match(cleaned):
        return None, 'unclear_format'
    try:
        d = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None, 'invalid_decimal'
    if not d.is_finite():
        return None, 'non_finite'
    return d, None


def _extract_currency(raw: Any, currency_expected: str | None) -> str | None:
    if isinstance(raw, dict):
        for key in ('currency', 'currencyCode', 'ccy', 'accountCurrency'):
            v = raw.get(key)
            if v is not None and str(v).strip():
                return str(v).strip().upper()
    if currency_expected:
        return str(currency_expected).strip().upper()
    return None


def _field_values(row: dict) -> list[tuple[str, Any]]:
    found = []
    for key in _CASH_FIELD_WHITELIST:
        if key in row and row[key] is not None and row[key] != '':
            found.append((key, row[key]))
    return found


def parse_broker_cash_amount(raw, *, currency_expected=None) -> dict:
    """Parse broker cash into minor units with status (P06/P08/P10/T39).

    status:
      OK               — verified numeric cash
      PENDING          — broker pending / placeholder (amount_minor may be null)
      SOURCE_CONFLICT  — whitelist fields disagree
      QUARANTINE       — NaN, invent, unclear locale, bad field
    """
    out = {
        'amount_minor': None,
        'currency': None,
        'status': 'QUARANTINE',
        'source_field': None,
        'raw': raw,
        'reason': None,
        'settled_zero': False,
    }

    if raw is None:
        out['reason'] = 'null_raw'
        out['status'] = 'PENDING'
        return out

    # Explicit pending markers.
    if isinstance(raw, dict):
        st = str(raw.get('status') or raw.get('transactionStatus') or '').strip().upper()
        note = str(raw.get('note') or '').strip().lower()
        tx_type = str(raw.get('transactionType') or raw.get('type') or '').strip().upper()
        if st in ('PENDING', 'RECEIVED', 'WAITING') or raw.get('pending') is True:
            out['status'] = 'PENDING'
            out['currency'] = _extract_currency(raw, currency_expected)
            out['reason'] = 'broker_pending'
            return out
        # Opening TRADE / placeholder zero is not final zero (P10 / T30).
        placeholder = bool(raw.get('placeholder') or raw.get('opening')
                           or note in ('trade opened', 'opening')
                           or (tx_type == 'TRADE' and raw.get('close_proof') is False))
        if placeholder and _looks_zero_placeholder(raw):
            out['status'] = 'PENDING'
            out['currency'] = _extract_currency(raw, currency_expected)
            out['amount_minor'] = None
            out['reason'] = 'opening_or_pending_zero'
            return out

        row_ccy = _extract_currency(raw, None)
        # Prefer row currency for labeling; currency_expected only gates later.
        parse_ccy = row_ccy or (
            str(currency_expected).strip().upper() if currency_expected else None)

        fields = _field_values(raw)
        # Also accept explicit 'amount' only when marked as cash source field.
        if not fields and raw.get('amount') is not None and raw.get('source_field') in (
                None, 'amount', 'pnl', 'profitAndLoss'):
            # Bare amount without whitelist proof → quarantine unless source_field set.
            if raw.get('source_field') in ('amount', 'pnl', 'profitAndLoss',
                                           'profit_and_loss', 'realizedPnl',
                                           'realized_pnl'):
                fields = [(raw.get('source_field') or 'amount', raw.get('amount'))]
            else:
                out['reason'] = 'amount_without_whitelist_proof'
                out['currency'] = row_ccy or (
                    str(currency_expected).strip().upper() if currency_expected else None)
                return out

        if not fields:
            out['reason'] = 'no_cash_field'
            out['currency'] = parse_ccy
            # Activity/confirm without cash field is pending funds, not invent.
            if raw.get('confirm') or raw.get('activity') or raw.get('upl') is not None:
                out['status'] = 'PENDING'
                out['reason'] = 'non_funds_row'
            return out

        parsed_vals: list[tuple[str, int, str | None]] = []
        for field, val in fields:
            one = _coerce_amount(val, parse_ccy)
            if one['status'] == 'QUARANTINE':
                out.update({
                    'status': 'QUARANTINE',
                    'reason': one.get('reason') or 'bad_field',
                    'source_field': field,
                    'currency': parse_ccy,
                })
                return out
            if one['status'] == 'PENDING':
                out['status'] = 'PENDING'
                out['currency'] = one.get('currency') or parse_ccy
                out['source_field'] = field
                out['reason'] = one.get('reason') or 'pending'
                return out
            parsed_vals.append((field, one['amount_minor'], one.get('currency')))

        minors = {m for _, m, _ in parsed_vals}
        if len(minors) > 1:
            out['status'] = 'SOURCE_CONFLICT'
            out['reason'] = 'conflicting_cash_fields'
            out['currency'] = parse_ccy
            out['source_field'] = ','.join(f for f, _, _ in parsed_vals)
            return out

        field, minor, ccy = parsed_vals[0]
        ccy = row_ccy or ccy or parse_ccy
        return _finalize_ok(out, minor, ccy, field, raw)

    # Scalar raw
    return _coerce_amount(raw, currency_expected)


def _looks_zero_placeholder(row: dict) -> bool:
    for key in _CASH_FIELD_WHITELIST + ('amount', 'profit'):
        if key not in row:
            continue
        val = row[key]
        if val is None or val == '':
            continue
        try:
            d = Decimal(str(val).replace('£', '').replace(',', '').strip() or 'x')
        except (InvalidOperation, ValueError):
            continue
        if d.is_finite() and d == 0:
            return True
    return False


def _coerce_amount(val, currency: str | None) -> dict:
    out = {
        'amount_minor': None,
        'currency': (str(currency).strip().upper() if currency else None),
        'status': 'QUARANTINE',
        'source_field': None,
        'raw': val,
        'reason': None,
        'settled_zero': False,
    }
    if val is None or val == '':
        out['status'] = 'PENDING'
        out['reason'] = 'empty_amount'
        return out
    if isinstance(val, bool):
        out['reason'] = 'bool_not_cash'
        return out
    if isinstance(val, float) and not math.isfinite(val):
        out['reason'] = 'non_finite'
        return out
    if isinstance(val, (int, float, Decimal)):
        if isinstance(val, float) and not math.isfinite(val):
            out['reason'] = 'non_finite'
            return out
        ccy = out['currency'] or 'GBP'
        if currency_exponent(ccy) is None:
            out['reason'] = 'unknown_currency'
            return out
        minor = to_minor(val, ccy)
        if minor is None:
            out['reason'] = 'precision_or_nan'
            return out
        return _finalize_ok(out, minor, ccy, None, val)

    if isinstance(val, str):
        text, sym_ccy = _strip_currency_prefix(val)
        ccy = sym_ccy or out['currency']
        num, reason = _parse_number_text(text)
        if reason:
            out['reason'] = reason
            if reason == 'empty':
                out['status'] = 'PENDING'
            return out
        if ccy is None:
            # No currency proof — quarantine rather than invent GBP (P10).
            out['reason'] = 'missing_currency'
            out['amount_minor'] = None
            return out
        if currency_exponent(ccy) is None:
            out['reason'] = 'unknown_currency'
            out['currency'] = ccy
            return out
        minor = to_minor(num, ccy)
        if minor is None:
            out['reason'] = 'precision_or_nan'
            out['currency'] = ccy
            return out
        return _finalize_ok(out, minor, ccy, None, val)

    out['reason'] = 'unsupported_type'
    return out


def _finalize_ok(out: dict, minor: int, ccy: str | None, field, raw) -> dict:
    # Currency expected mismatch handled by caller / account_currency_gate.
    out['amount_minor'] = minor
    out['currency'] = ccy
    out['source_field'] = field
    out['raw'] = raw
    out['status'] = 'OK'
    out['reason'] = None
    out['settled_zero'] = (minor == 0)
    return out


def account_currency_gate(amount_minor, currency, account_currency) -> dict:
    """P10/T33: foreign or unknown cash currency is not account cash."""
    acct = (account_currency or '').strip().upper() or None
    ccy = (currency or '').strip().upper() or None
    if amount_minor is None:
        return {
            'accepted': False,
            'amount_minor': None,
            'currency': ccy,
            'status': 'PENDING',
            'reason': 'no_amount',
        }
    if not ccy:
        return {
            'accepted': False,
            'amount_minor': None,
            'currency': None,
            'status': 'QUARANTINE',
            'reason': 'unknown_currency',
        }
    if acct and ccy != acct:
        return {
            'accepted': False,
            'amount_minor': amount_minor,
            'currency': ccy,
            'status': 'PENDING',
            'reason': 'currency_mismatch_await_funds',
        }
    return {
        'accepted': True,
        'amount_minor': amount_minor,
        'currency': ccy,
        'status': 'OK',
        'reason': None,
    }


def pick_live_realized(*, funds=None, confirm_profit=None, last_upl=None,
                       mid_proxy=None, paper=None, source=None) -> dict:
    """Select LIVE realized minor units — Funds only (T28/T29/T31/T38)."""
    if source is not None and is_forbidden_live_source(source) and funds is None:
        return {
            'amount_minor': None,
            'currency': None,
            'status': 'PENDING',
            'source': classify_pnl_source(source),
            'reason': 'forbidden_source_without_funds',
        }
    if funds is None:
        return {
            'amount_minor': None,
            'currency': None,
            'status': 'PENDING',
            'source': 'pending',
            'reason': 'awaiting_funds',
            'ignored': {
                'confirm_profit': confirm_profit,
                'last_upl': last_upl,
                'mid_proxy': mid_proxy,
                'paper': paper,
            },
        }
    parsed = parse_broker_cash_amount(funds)
    if parsed['status'] != 'OK':
        return {
            'amount_minor': parsed.get('amount_minor'),
            'currency': parsed.get('currency'),
            'status': parsed['status'],
            'source': 'pending' if parsed['status'] == 'PENDING' else 'unverified',
            'reason': parsed.get('reason'),
        }
    return {
        'amount_minor': parsed['amount_minor'],
        'currency': parsed['currency'],
        'status': 'OK',
        'source': 'cash',
        'reason': None,
        'ui': format_ui_amount(parsed['amount_minor'], parsed['currency'] or 'GBP'),
        'settled_zero': parsed.get('settled_zero', False),
    }
