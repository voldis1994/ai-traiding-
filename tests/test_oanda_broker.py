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


def test_closed_trade_realized_pl_from_order_fill():
    """SL/TP close must expose broker cash keyed by OANDA trade id (= dealId)."""
    def handle(request):
        path = request.url.path
        if path.endswith('/summary'):
            return httpx.Response(200, json={
                'account': {
                    'id': ACCOUNT, 'currency': 'USD', 'balance': '5000',
                    'NAV': '5000', 'marginAvailable': '4000',
                },
                'lastTransactionID': '120',
            })
        if path.endswith('/transactions/idrange'):
            return httpx.Response(200, json={
                'lastTransactionID': '120',
                'transactions': [{
                    'id': '118', 'type': 'ORDER_FILL', 'time': '2026-11-28T12:00:00.000000000Z',
                    'instrument': 'XAU_USD', 'pl': '-3.40', 'accountID': ACCOUNT,
                    'tradesClosed': [
                        {'tradeID': '77', 'units': '-1', 'realizedPL': '-3.40'},
                    ],
                }, {
                    'id': '119', 'type': 'ORDER_FILL', 'time': '2026-11-28T12:01:00.000000000Z',
                    'instrument': 'XAU_USD', 'pl': '1.10', 'accountID': ACCOUNT,
                    'tradesClosed': [
                        {'tradeID': '88', 'units': '2', 'realizedPL': '1.10'},
                    ],
                }],
            })
        return httpx.Response(404)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                   base_url='https://broker.invalid')
        broker = OandaBroker('tok', ACCOUNT, '', client=client)
        await broker.connect()
        acts = await broker.activity(deal_id='77', last_period=3600)
        assert len(acts['activities']) == 1
        assert acts['activities'][0]['dealId'] == '77'
        assert acts['activities'][0]['profit'] == pytest.approx(-3.40)
        assert acts['activities'][0]['currency'] == 'USD'
        txs = await broker.transactions(last_period=3600)
        by_deal = {t['dealId']: t for t in txs['transactions']}
        assert by_deal['88']['pnl'] == pytest.approx(1.10)
        await broker.close()

    async_run(scenario())


def test_engine_resolves_oanda_broker_cash_pnl():
    """End-to-end: vanish → activity realizedPL → pnl_source=broker."""
    import asyncio
    import tempfile
    from pathlib import Path
    from app.autonomy import AutoEvolution
    from app.engine import SingleTradeEngine
    from tests.test_learning_integrity import new_brain

    class OandaHistoryBroker:
        def __init__(self):
            self.open = []
            self._currency = 'USD'
        async def positions(self):
            return list(self.open)
        async def accounts(self):
            return {'accounts': [{'preferred': True, 'currency': 'USD', 'accountId': '1',
                                  'balance': {'balance': 1000}}]}
        async def activity(self, *, last_period=600, from_time=None, to_time=None,
                           detailed=True, deal_id=None):
            rows = [{
                'dealId': '77',
                'details': {'profit': -3.40, 'currency': 'USD'},
                'profit': -3.40,
                'currency': 'USD',
            }]
            if deal_id:
                rows = [r for r in rows if str(r['dealId']) == str(deal_id)]
            return {'activities': rows}
        async def transactions(self, *, last_period=600, from_time=None, to_time=None,
                               tx_type='TRADE'):
            return {'transactions': [{
                'dealId': '77', 'reference': '77', 'pnl': -3.40, 'currency': 'USD',
            }]}

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(100, .05, .01, 'XAU_USD')
            e = SingleTradeEngine(store, brain)
            e.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = OandaHistoryBroker()
            broker.open = [{'position': {
                'dealId': '77', 'size': '1', 'direction': 'SELL',
                'level': 2651, 'upl': -0.5, 'currency': 'USD',
            }, 'market': {'epic': 'XAU_USD'}}]
            e.attach(broker)
            e.account = {'accounts': [{'preferred': True, 'currency': 'USD', 'accountId': '1'}]}
            await e.sync()
            e.market = {'epic': 'XAU_USD', 'mid': 2650.2, 'tick': .1, 'spread': .3}
            store.set('entry_brain:77', {
                'epic': 'XAU_USD', 'direction': 'SELL', 'entry': 2651,
                'tick_size': .1,
                'transition': brain._json_transition(brain.core.last_transition),
            })
            store.upsert_trade({
                'dealId': '77', 'epic': 'XAU_USD', 'size': '1',
                'direction': 'SELL', 'level': 2651,
            })
            broker.open = []
            await e.sync()
            row = store.trades(1)[0]
            assert row['pnl'] == pytest.approx(-3.40)
            assert row['pnl_source'] == 'broker'
            assert row['pnl_currency'] == 'USD'

    asyncio.run(scenario())


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
