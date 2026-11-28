"""Broker adapter registry — OANDA LIVE is the active broker."""
from __future__ import annotations
from typing import Any

from app.broker import OandaBroker

# Public ids used in admin UI / DB. Add new adapters without rewriting tenants.
SUPPORTED_BROKERS = (
    {'id': 'oanda', 'label': 'OANDA', 'status': 'live'},
    {'id': 'generic_rest', 'label': 'Other broker (slot)', 'status': 'slot'},
)

_REGISTRY = {
    'oanda': OandaBroker,
}


def list_brokers() -> list[dict[str, str]]:
    return [dict(row) for row in SUPPORTED_BROKERS]


def create_broker(broker_type: str, api_key: str, identifier: str, password: str = '',
                  *, live: bool = True, **kwargs: Any):
    """Build a live broker client. Unknown / slot types fail clearly."""
    key = (broker_type or '').strip().lower()
    if key in ('oanda', 'oanda_live', 'oanda_v20', 'fxtrade'):
        return OandaBroker(api_key, identifier, password or '', live=live, **kwargs)
    if key in ('capital', 'capital.com', 'capital_com'):
        raise ValueError(
            'Capital.com has been replaced by OANDA. Attach an OANDA account '
            '(API token + account id).')
    if key in ('generic_rest', 'other', 'slot'):
        raise ValueError(
            'This broker slot is reserved. Add an adapter in app/brokers before attaching accounts.')
    if key in _REGISTRY:
        return _REGISTRY[key](api_key, identifier, password or '', live=live, **kwargs)
    raise ValueError(f'Unsupported broker type: {broker_type}')
