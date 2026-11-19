"""Broker adapter registry — Capital.com, IG Markets, Capital-compatible hosts."""
from __future__ import annotations
from typing import Any

from app.broker import CapitalBroker
from app.brokers.ig import IgBroker

# Public ids used in admin UI / DB.
SUPPORTED_BROKERS=(
    {'id':'capital_com','label':'Capital.com','status':'live','needs_base_url':False},
    {'id':'ig_markets','label':'IG Markets (LIVE)','status':'live','needs_base_url':False},
    {'id':'ig_demo','label':'IG Markets (DEMO)','status':'live','needs_base_url':False},
    {'id':'generic_rest','label':'Other (Capital-compatible API)','status':'live',
     'needs_base_url':True},
)

_REGISTRY={
    'capital_com':CapitalBroker,
    'ig_markets':IgBroker,
}


def list_brokers() -> list[dict]:
    return [dict(row) for row in SUPPORTED_BROKERS]


def create_broker(broker_type: str, api_key: str, identifier: str, password: str,
                  *, live: bool=True, base_url: str|None=None, **kwargs: Any):
    """Build a live broker client from API key + identifier + password."""
    key=(broker_type or '').strip().lower()
    endpoint=(base_url or '').strip() or None

    if key in ('capital','capital.com','capital_com'):
        return CapitalBroker(api_key,identifier,password,live=live,
                             base_url=endpoint,**kwargs)

    if key in ('ig','ig_markets','ig.com'):
        return IgBroker(api_key,identifier,password,live=True,
                        base_url=endpoint,**kwargs)

    if key in ('ig_demo','ig-demo'):
        return IgBroker(api_key,identifier,password,live=False,
                        base_url=endpoint,**kwargs)

    if key in ('generic_rest','other','capital_compatible','slot'):
        if not endpoint:
            raise ValueError(
                'Other broker needs a Capital-compatible API base URL '
                '(e.g. https://api-capital.backend-capital.com).')
        if not endpoint.lower().startswith('https://'):
            raise ValueError('Broker base URL must start with https://')
        return CapitalBroker(api_key,identifier,password,live=live,
                             base_url=endpoint,streaming_url=None,**kwargs)

    if key in _REGISTRY:
        return _REGISTRY[key](api_key,identifier,password,live=live,
                              base_url=endpoint,**kwargs)
    raise ValueError(f'Unsupported broker type: {broker_type}')
