"""Broker adapter registry — Capital.com first; other brokers plug in here."""
from __future__ import annotations
from typing import Any

from app.broker import CapitalBroker

# Public ids used in admin UI / DB. Add new adapters without rewriting tenants.
SUPPORTED_BROKERS=(
    {'id':'capital_com','label':'Capital.com','status':'live'},
    {'id':'generic_rest','label':'Other broker (slot)','status':'slot'},
)

_REGISTRY={
    'capital_com':CapitalBroker,
}


def list_brokers() -> list[dict[str,str]]:
    return [dict(row) for row in SUPPORTED_BROKERS]


def create_broker(broker_type: str, api_key: str, identifier: str, password: str,
                  *, live: bool=True, **kwargs: Any):
    """Build a live broker client. Unknown / slot types fail clearly."""
    key=(broker_type or '').strip().lower()
    if key in ('capital','capital.com','capital_com'):
        return CapitalBroker(api_key,identifier,password,live=live,**kwargs)
    if key in ('generic_rest','other','slot'):
        raise ValueError(
            'This broker slot is reserved. Add an adapter in app/brokers before attaching accounts.')
    if key in _REGISTRY:
        return _REGISTRY[key](api_key,identifier,password,live=live,**kwargs)
    raise ValueError(f'Unsupported broker type: {broker_type}')
