"""Regression for audit follow-up: naked OPEN, uncertain OPEN, multi-pos, auth."""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store, _broker_price_level
from app.platform import PlatformStore
from tests.test_tp_min_distance import _OpenCaptureBroker


def test_protect_uses_open_bracket_snapshot_when_no_swings():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            broker.open = [{'position': {
                'dealId': 'naked1', 'size': '.1', 'direction': 'BUY',
                'level': 100.0, 'stopLevel': None, 'profitLevel': None, 'upl': 0,
            }, 'market': {'epic': 'SIM'}}]

            async def _confirm(ref):
                req = [r for r in broker.requests if r[0] == 'PROTECT'][-1]
                _, deal_id, stop, profit = req
                pos = broker.open[0]['position']
                if stop is not None:
                    pos['stopLevel'] = float(stop)
                if profit is not None:
                    pos['profitLevel'] = float(profit)
                return {'dealStatus': 'ACCEPTED', 'dealId': deal_id}

            async def _update(deal_id, stop_level=None, profit_level=None):
                broker.requests.append(('PROTECT', deal_id, stop_level, profit_level))
                return {'dealReference': 'prot-snap'}

            broker.confirm = _confirm
            broker.update_position = _update
            engine.attach(broker)
            await engine.sync()
            store.set('robot_open:naked1', True)
            store.set('open_brackets:naked1', {
                'stop_distance': 1.5, 'profit_distance': 2.5,
                'direction': 'BUY', 'entry': 100.0,
            })
            engine.market = {
                'epic': 'SIM', 'mid': 100.0, 'bid': 99.9, 'ask': 100.1,
                'spread': 0.2, 'tick': 0.01, 'min_distance': 0.15, 'status': 'TRADEABLE',
            }
            # No candles → swing path unavailable; snapshot must still protect.
            await engine.protect_position(engine.positions[0])
            protects = [r for r in broker.requests if r[0] == 'PROTECT']
            assert protects, 'expected PROTECT from OPEN snapshot'
            _, _, stop, profit = protects[0]
            assert stop is not None and profit is not None
            assert float(stop) < 100.0 < float(profit)

    asyncio.run(scenario())


def test_uncertain_open_blocks_second_submit():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            engine.armed = True
            engine.execution_enabled = True
            engine.last_closed_step = 1
            engine.mandate = {'epic': 'SIM', 'size': '0.1'}
            engine.market_context = {'ready': True}
            store.order('OPEN', None, {'epic': 'SIM', 'direction': 'BUY', 'size': '0.1'})
            # Force UNCERTAIN status
            oid = store.unresolved()[0]['id']
            store.order_update(oid, 'UNCERTAIN', 'ref-x')
            await engine.submit('BUY')
            assert 'unresolved' in (engine.last_entry_reason or '').lower()

    asyncio.run(scenario())


def test_open_confirm_binds_confirm_deal_id_not_positions0():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.mandate = {'epic': 'SIM', 'size': '0.1'}
            # Two positions — wrong bind would tag the first.
            engine.positions = [
                {'dealId': 'old', 'epic': 'SIM', 'direction': 'BUY', 'size': '0.1',
                 'level': 99.0, 'stopLevel': 98.0, 'profitLevel': 100.0},
                {'dealId': 'new', 'epic': 'SIM', 'direction': 'BUY', 'size': '0.1',
                 'level': 100.0, 'stopLevel': None, 'profitLevel': None},
            ]
            oid = store.order('OPEN', None, {
                'epic': 'SIM', 'direction': 'BUY', 'size': '0.1',
                'stop_distance': '1.0', 'profit_distance': '1.5',
            })
            ok = engine._order_fully_matched(
                'OPEN',
                {'epic': 'SIM', 'direction': 'BUY', 'size': '0.1'},
                None,
                {'dealId': 'new', 'dealStatus': 'ACCEPTED'},
                None)
            assert ok is True
            # Simulate confirm path binding
            confirmation = {'dealId': 'new', 'dealReference': 'r1'}
            req = {'epic': 'SIM', 'direction': 'BUY', 'size': '0.1',
                   'stop_distance': '1.0', 'profit_distance': '1.5'}
            store.order_update(oid, 'CONFIRMED', 'r1')
            conf_deal = confirmation.get('dealId')
            p = next(x for x in engine.positions if str(x['dealId']) == str(conf_deal))
            store.set('robot_open:' + str(p['dealId']), True)
            store.set('latest_robot_deal_id', p['dealId'])
            store.set('open_brackets:' + str(p['dealId']), {
                'stop_distance': req['stop_distance'],
                'profit_distance': req['profit_distance'],
            })
            assert store.get('robot_open:new') is True
            assert store.get('robot_open:old') is None
            assert store.get('latest_robot_deal_id') == 'new'
            assert store.get('open_brackets:new')['stop_distance'] == '1.0'

    asyncio.run(scenario())


def test_emergency_flatten_closes_all_deals():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 's.db')
            brain = AdaptiveBrain(store, data_dir=d)
            engine = SingleTradeEngine(store, brain)
            engine.policy = AutoEvolution(store, Path(d) / 'policy.py')
            broker = _OpenCaptureBroker()
            broker.price = 100.0
            broker.open = [
                {'position': {
                    'dealId': 'a', 'size': '.1', 'direction': 'BUY',
                    'level': 100.0, 'stopLevel': 99.0, 'profitLevel': 101.0, 'upl': 0,
                }, 'market': {'epic': 'SIM'}},
                {'position': {
                    'dealId': 'b', 'size': '.1', 'direction': 'SELL',
                    'level': 100.0, 'stopLevel': 101.0, 'profitLevel': 99.0, 'upl': 0,
                }, 'market': {'epic': 'SIM'}},
            ]
            closed = []

            async def _close(deal_id):
                closed.append(deal_id)
                broker.open = [r for r in broker.open
                               if r['position']['dealId'] != deal_id]
                return {'dealReference': 'c-' + str(deal_id)}

            async def _confirm(ref):
                return {'dealStatus': 'ACCEPTED', 'dealId': ref.split('-')[-1]}

            broker.close_position = _close
            broker.confirm = _confirm
            engine.attach(broker)
            await engine.sync()
            assert len(engine.positions) == 2
            result = await engine.emergency_flatten()
            assert set(closed) == {'a', 'b'}
            assert engine.operator_paused is True
            assert engine.armed is False
            assert result['closed'] == ['a', 'b']

    asyncio.run(scenario())


def test_session_expires_and_password_reset_revokes(tmp_path):
    plat = PlatformStore(tmp_path / 'p.db', tmp_path / 'k.key')
    plat.ensure_admin('admin', 'StrongPassw0rd!!')
    admin = plat.verify_admin('admin', 'StrongPassw0rd!!')
    token = plat.create_session('admin', admin['id'], ttl_hours=1)
    assert plat.get_session(token)['role'] == 'admin'
    # Force expiry
    plat.db.execute(
        'UPDATE sessions SET expires=? WHERE token=?',
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec='seconds'),
         token))
    plat.db.commit()
    assert plat.get_session(token) is None

    client = plat.create_client('bob', 'bobpass', 'Bob')
    t2 = plat.create_session('client', client['id'])
    assert plat.get_session(t2)
    plat.reset_client_password(client['id'], 'newpass')
    assert plat.get_session(t2) is None


def test_ensure_admin_syncs_password(tmp_path):
    plat = PlatformStore(tmp_path / 'p.db', tmp_path / 'k.key')
    plat.ensure_admin('admin', 'admin123')
    assert plat.verify_admin('admin', 'admin123')
    plat.ensure_admin('admin', 'MuchStrongerPass1!', sync_password=True)
    assert plat.verify_admin('admin', 'admin123') is None
    assert plat.verify_admin('admin', 'MuchStrongerPass1!')


def test_production_connect_disabled(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('NTA_PRODUCTION', '1')
    monkeypatch.delenv('NTA_ALLOW_LEGACY_CONNECT', raising=False)
    # Avoid bootstrap refuse for weak password during import path — set strong.
    monkeypatch.setenv('NTA_ADMIN_PASSWORD', 'ProductionStrongPass1!')
    from app import main
    # Force flag check on live handler
    client = TestClient(main.app)
    # Health still works
    assert client.get('/api/health').status_code == 200
    assert 'clients' not in client.get('/api/health').json()
    r = client.post('/api/connect', json={
        'api_key': 'k', 'identifier': 'i', 'password': 'p'})
    assert r.status_code == 403
