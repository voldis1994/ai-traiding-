"""OANDA adapter unit tests (mocked HTTP — no live API)."""
from decimal import Decimal
import httpx
import pytest

from app.broker import OandaBroker, instrument_name
from app.brokers import create_broker, list_brokers
from tests.test_integration import async_run


ACCOUNT = '101-001-1234567-001'


def test_instrument_name_normalizes():
    assert instrument_name('xau_usd') == 'XAU_USD'
    assert instrument_name('EUR/USD') == 'EUR_USD'
    with pytest.raises(ValueError):
        instrument_name('')


def test_registry_lists_oanda_only_live():
    rows = list_brokers()
    assert rows[0]['id'] == 'oanda'
    assert all(r['id'] != 'capital_com' for r in rows)
    with pytest.raises(ValueError, match='replaced'):
        create_broker('capital_com', 't', ACCOUNT, '')


def test_positions_and_accounts_mapping():
    def handle(request):
        path = request.url.path
        if path.endswith('/summary'):
            return httpx.Response(200, json={
                'account': {
                    'id': ACCOUNT, 'currency': 'USD', 'balance': '5000',
                    'NAV': '5100', 'marginAvailable': '4000', 'pl': '100',
                },
                'lastTransactionID': '9',
            })
        if path.endswith('/openTrades'):
            return httpx.Response(200, json={'trades': [{
                'id': '42', 'instrument': 'XAU_USD', 'price': '2650.5',
                'currentUnits': '-2', 'unrealizedPL': '-1.5',
                'stopLossOrder': {'price': '2660.0'},
                'takeProfitOrder': {'price': '2640.0'},
            }]})
        return httpx.Response(404)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                   base_url='https://broker.invalid')
        broker = OandaBroker('tok', ACCOUNT, '', client=client)
        await broker.connect()
        accounts = await broker.accounts()
        assert accounts['accounts'][0]['currency'] == 'USD'
        assert accounts['accounts'][0]['balance']['equity'] == 5100.0
        pos = await broker.positions()
        assert len(pos) == 1
        p = pos[0]['position']
        assert p['dealId'] == '42'
        assert p['direction'] == 'SELL'
        assert float(p['size']) == 2.0
        assert p['stopLevel'] == 2660.0
        assert p['profitLevel'] == 2640.0
        assert pos[0]['market']['epic'] == 'XAU_USD'
        await broker.close()

    async_run(scenario())


def test_candles_mapped_to_capital_price_rows():
    def handle(request):
        if '/candles' in request.url.path:
            return httpx.Response(200, json={'candles': [{
                'time': '2026-11-28T10:00:00.000000000Z', 'complete': True,
                'bid': {'o': '1', 'h': '3', 'l': '0.5', 'c': '2'},
                'ask': {'o': '1.1', 'h': '3.1', 'l': '0.6', 'c': '2.1'},
                'mid': {'o': '1.05', 'h': '3.05', 'l': '0.55', 'c': '2.05'},
            }, {
                'time': '2026-11-28T10:01:00.000000000Z', 'complete': False,
                'mid': {'o': '2', 'h': '2', 'l': '2', 'c': '2'},
            }]})
        return httpx.Response(404)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                   base_url='https://broker.invalid')
        broker = OandaBroker('tok', ACCOUNT, '', client=client)
        rows = await broker.prices('XAU_USD', maximum=10)
        assert len(rows['prices']) == 1
        bar = rows['prices'][0]
        assert bar['closePrice']['bid'] == 2.0
        assert bar['closePrice']['ask'] == 2.1
        await broker.close()

    async_run(scenario())
