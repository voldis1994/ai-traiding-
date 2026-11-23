"""TP1/TP2 must be real OHLC structure — never invented entry±κ."""
from decimal import Decimal

import pytest

from app import ohlc_entry_zones as oez
from app import ohlc_scenarios as osc
from app import ohlc_regime_brackets as orb
from app import bracket_learn
from app.broker import CapitalBroker
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


def test_regime_returns_structural_tp1_and_tp2_buy():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_UP', entry_hint='BUY')
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=zones,
        entry_zone=buy, trigger='zone', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert br.get('structural_tp') is True
    assert br.get('tp1_level') is not None
    assert br.get('tp2_level') is not None
    assert float(br['tp1_level']) >= entry
    assert float(br['tp2_level']) >= float(br['tp1_level']) - 1e-9
    # Broker profit = TP2
    assert float(br['profit_level']) == pytest.approx(float(br['tp2_level']))


def test_range_tp1_is_box_geometry_not_invented_kappa():
    sc = {
        'ready': True, 'scenario': 'RANGE', 'entry_hint': 'EDGE',
        'range': {'lo': 100.0, 'hi': 110.0, 'span': 10.0},
    }
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(40):
        px = 100.5 + (i % 5) * 0.2
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': px, 'high': px + 0.3, 'low': px - 0.3, 'close': px + 0.05,
        })
    entry = 101.0
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=None,
        trigger='zone', tick=0.1, spread=0.2, min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    # Broker TP2 = opposite range edge (real OHLC). No invented κ×SL / 75% mid.
    assert float(br['tp2_level']) == pytest.approx(110.0, abs=0.2)
    assert float(br['profit_level']) == pytest.approx(float(br['tp2_level']))
    assert float(br['tp1_level']) > entry
    assert float(br['tp1_level']) <= float(br['tp2_level']) + 1e-9
    # Must not be formula entry + κ×risk (κ=2 → 101+2×1.4=103.8).
    invent = entry + 2.0 * float(br['stop_distance'])
    assert abs(float(br['tp2_level']) - invent) > 0.5


def test_decide_brackets_passes_tp_ladder_without_inventing_rr():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_UP')
    z = bracket_learn.zone_at_open(rows, multi_tf=True)
    decided = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry, action='BUY',
        candles=rows, atr=1.0, require_swing=True, entry_zone=buy,
        scenario=sc, zones=zones, trigger='zone')
    assert decided.get('ready') is True
    assert decided.get('source') == 'regime_brackets'
    assert decided.get('tp1_level') is not None
    assert decided.get('tp2_level') is not None
    assert float(decided['tp1_level']) <= float(decided['tp2_level']) + 1e-9
    assert float(decided['profit_level']) == pytest.approx(
        float(decided['tp2_level']))


def test_broker_reduce_position_posts_opposite_without_brackets():
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, headers=None, params=None, json=None):
            self.calls.append((method, path, json))

            class R:
                status_code = 200
                headers = {}

                def raise_for_status(self):
                    return None

                def json(self):
                    return {'dealReference': 'r1'}

            return R()

        async def aclose(self):
            return None

    client = FakeClient()
    b = CapitalBroker('k', 'id', 'pw', live=False, client=client)
    b.cst = 'c'; b.security = 's'

    import asyncio
    asyncio.run(b.reduce_position('GOLD', 'BUY', 0.5))
    method, path, payload = client.calls[0]
    assert method == 'POST'
    assert path == '/api/v1/positions'
    assert payload['direction'] == 'SELL'
    assert payload['size'] == 0.5
    assert 'stopDistance' not in payload
    assert 'profitDistance' not in payload


def test_sell_has_single_tp_no_ladder():
    """Shorts: one structural TP — no TP1/TP2 scale-out ladder."""
    rows, mid = supply_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    sell = zones['sell_entry']
    entry = (float(sell['lo']) + float(sell['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_DOWN', entry_hint='SELL')
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='SELL', scenario=sc, zones=zones,
        entry_zone=sell, trigger='zone', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert br.get('tp_ladder') is False
    assert float(br.get('scale_out_frac') or 0) == 0.0
    assert float(br['profit_level']) < entry
    # Collapsed ladder: tp1 == tp2 == broker profit.
    assert float(br['tp1_level']) == pytest.approx(float(br['tp2_level']))
    assert float(br['profit_level']) == pytest.approx(float(br['tp2_level']))


def test_buy_keeps_tp_ladder_and_scale_out():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_UP', entry_hint='BUY')
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=zones,
        entry_zone=buy, trigger='zone', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert br.get('tp_ladder') is True
    assert float(br.get('scale_out_frac') or 0) == pytest.approx(0.5)
    assert float(br['tp1_level']) <= float(br['tp2_level']) + 1e-9


def test_engine_scale_out_tp1_reduces_and_moves_sl_to_be():
    import asyncio
    import tempfile
    from pathlib import Path

    from app.engine import AdaptiveBrain, SingleTradeEngine, Store

    class Cap:
        def __init__(self):
            self.requests = []
            self.open = [{'position': {
                'dealId': 'd-tp1', 'size': '1.0', 'direction': 'BUY',
                'level': 100.0, 'stopLevel': 98.0, 'profitLevel': 106.0,
                'upl': 2.0,
            }, 'market': {'epic': 'SIM'}}]

        async def positions(self):
            return list(self.open)

        async def market(self, epic):
            return {
                'snapshot': {'bid': 103.0, 'offer': 103.02,
                             'marketStatus': 'TRADEABLE'},
                'instrument': {'onePipMeans': 0.01},
                'dealingRules': {
                    'minDealSize': {'unit': 'AMOUNT', 'value': '0.1'},
                    'minSizeIncrement': {'unit': 'AMOUNT', 'value': '0.1'},
                    'minStopOrProfitDistance': {'unit': 'POINTS', 'value': '0.15'},
                },
            }

        async def accounts(self):
            return {'accounts': [{'preferred': True, 'accountId': 'SIM',
                                  'currency': 'USD',
                                  'balance': {'balance': 1000}}]}

        async def confirm(self, ref):
            return {'dealStatus': 'ACCEPTED', 'dealId': 'd-tp1',
                    'dealReference': ref}

        async def reduce_position(self, epic, open_direction, size):
            self.requests.append(('REDUCE', epic, open_direction, size))
            # Shrink open size (netting).
            pos = self.open[0]['position']
            pos['size'] = str(float(pos['size']) - float(size))
            return {'dealReference': 'red1'}

        async def update_position(self, deal_id, stop_level=None, profit_level=None):
            self.requests.append(('PROTECT', deal_id, stop_level, profit_level))
            p = self.open[0]['position']
            if stop_level is not None:
                p['stopLevel'] = float(stop_level)
            if profit_level is not None:
                p['profitLevel'] = float(profit_level)
            return {'dealReference': 'prot1'}

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.mandate = {'epic': 'SIM', 'size': '1.0'}
            engine.execution_enabled = True
            broker = Cap()
            engine.attach(broker)
            await engine.sync()
            engine._last_broker_market = await broker.market('SIM')
            store.set('entry_brain:d-tp1', {
                'entry': 100.0, 'direction': 'BUY',
                'tp1_level': 103.0, 'tp2_level': 106.0,
                'scale_out_frac': 0.5, 'scale_out_done': False,
                'tick_size': 0.01,
            })
            engine.market = {
                'epic': 'SIM', 'mid': 103.01, 'bid': 103.0, 'ask': 103.02,
                'spread': 0.02, 'tick': 0.01, 'min_distance': 0.15,
                'status': 'TRADEABLE',
            }
            await engine._maybe_scale_out_tp1(engine.positions[0])
            reduces = [r for r in broker.requests if r[0] == 'REDUCE']
            protects = [r for r in broker.requests if r[0] == 'PROTECT']
            assert reduces, 'expected opposite reduce at TP1'
            assert float(reduces[0][3]) == pytest.approx(0.5)
            assert protects, 'expected BE protect after TP1'
            _, deal, stop, profit = protects[0]
            assert deal == 'd-tp1'
            assert float(stop) >= 100.0  # BE (+ tick)
            assert float(profit) == pytest.approx(106.0)
            brain_after = store.get('entry_brain:d-tp1')
            assert brain_after.get('scale_out_done') is True
            assert brain_after.get('be_after_tp1') is True

    asyncio.run(scenario())
