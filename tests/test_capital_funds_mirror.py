"""Capital Funds → Desk CLIENT CLOSES mirror."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from app.engine import Store, SingleTradeEngine, AdaptiveBrain


@pytest.fixture(autouse=True)
def _skip_slow_adaptive_boot(monkeypatch):
    """Engine __init__ calls adaptive_system.boot → evo sample scan (can hang)."""
    monkeypatch.setattr(
        'app.adaptive_system.boot',
        lambda *a, **k: {'ok': True, 'booted': True})


def new_brain(d):
    store = Store(Path(d) / 'db.sqlite3')
    brain = AdaptiveBrain(store, n=8, data_dir=d, seed=3)
    return store, brain


class FundsBroker:
    def __init__(self, tx=None):
        self.tx = list(tx or [])

    async def transactions(self, *, last_period=600, from_time=None, to_time=None,
                           tx_type='TRADE'):
        return {'transactions': list(self.tx)}

    async def activity(self, **kwargs):
        return {'activities': []}


def test_mirror_inserts_orphan_capital_funds_closes():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            broker = FundsBroker([
                {'reference': 'F1', 'instrumentName': 'Gold',
                 'pnl': -0.27, 'currency': 'GBP', 'size': -0.1,
                 'openLevel': 3616.0, 'closeLevel': 3614.5,
                 'dateUtc': '2026-11-23T12:09:00Z'},
                {'reference': 'F2', 'instrumentName': 'Gold Spot',
                 'pnl': 0.33, 'currency': 'GBP', 'size': 0.1,
                 'openLevel': 3610.0, 'closeLevel': 3612.0,
                 'dateUtc': '2026-11-23T11:00:00Z'},
            ])
            e.attach(broker)
            n = await e._mirror_capital_funds_closes()
            assert n == 2
            rows = store.trades(10)
            assert len(rows) == 2
            pnls = {round(float(r['pnl']), 2) for r in rows}
            assert pnls == {-0.27, 0.33}
            assert all(r['pnl_source'] == 'broker' for r in rows)
            assert all(str(r['deal_id']).startswith('cap:') for r in rows)
            # Idempotent — second pass must not duplicate.
            n2 = await e._mirror_capital_funds_closes()
            assert n2 == 0
            assert len(store.trades(20)) == 2
    asyncio.run(scenario())


def test_mirror_upgrades_upl_estimate_to_broker():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.upsert_trade({
                'dealId': 'deal-abc', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 3616,
            })
            store.close_trade(
                'deal-abc', 3614.5, -0.15, currency='GBP',
                source='upl_estimate', overwrite=True)
            broker = FundsBroker([{
                'dealId': 'deal-abc', 'instrumentName': 'Gold',
                'pnl': -0.27, 'currency': 'GBP', 'size': -0.1,
                'openLevel': 3616.0, 'closeLevel': 3614.5,
                'dateUtc': '2026-11-23T12:09:00Z',
            }])
            e.attach(broker)
            n = await e._mirror_capital_funds_closes()
            assert n == 1
            row = next(r for r in store.trades(5) if r['deal_id'] == 'deal-abc')
            assert row['pnl'] == pytest.approx(-0.27)
            assert row['pnl_source'] == 'broker'
    asyncio.run(scenario())


def test_mirror_fixes_zero_poison_broker_to_capital_funds():
    """Desk +0.00 while Capital Funds shows -£0.12 must be overwritten."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.upsert_trade({
                'dealId': 'deal-zero', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 3625,
            })
            store.close_trade(
                'deal-zero', 3624.8, 0.0, currency='GBP',
                source='broker', overwrite=True)
            broker = FundsBroker([{
                'dealId': 'deal-zero', 'instrumentName': 'Gold',
                'pnl': -0.12, 'currency': 'GBP', 'size': -0.1,
                'openLevel': 3625.0, 'closeLevel': 3624.8,
                'dateUtc': '2026-11-23T13:12:00Z',
            }])
            e.attach(broker)
            n = await e._mirror_capital_funds_closes()
            assert n == 1
            row = next(r for r in store.trades(5) if r['deal_id'] == 'deal-zero')
            assert row['pnl'] == pytest.approx(-0.12)
            assert row['pnl_source'] == 'broker'
    asyncio.run(scenario())


def test_zero_poison_reaudit_lets_cycle_see_capital_minus():
    """First +0.00 audit must not block re-diagnose after Funds −£0.12."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            # Cycle ran on poison £0 → "no mistakes".
            first = e._run_mistake_audit_close(
                'deal-reaudit', pnl=0.0, direction='BUY', entry=3625.0,
                source='live')
            assert first and first.get('audited')
            assert not (first.get('mistakes') or [])
            # Capital Funds £ lands — cycle must re-run and see the hurt.
            second = e._run_mistake_audit_close(
                'deal-reaudit', pnl=-0.12, direction='BUY', entry=3625.0,
                source='live_funds_mirror')
            assert second and second.get('audited')
            assert second.get('hurt') is True or (second.get('mistakes') or [])
            # Backfill also picks journal losses.
            store.upsert_trade({
                'dealId': 'deal-loss', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'SELL', 'level': 3620,
            })
            store.close_trade(
                'deal-loss', 3622, -0.65, currency='GBP',
                source='broker', overwrite=True)
            n = e._backfill_mistake_audits_from_journal(limit=8)
            assert n >= 1
            from app import shadow_mistake_audit as sma
            lab = sma.lab_payload(store)
            assert lab['hurt_n'] >= 1
    asyncio.run(scenario())


def test_mirror_upgrades_stale_broker_pence_to_funds():
    """Desk −0.81 broker must become Funds −0.89 (phone truth)."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.upsert_trade({
                'dealId': 'deal-stale', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 4188.5,
            })
            store.close_trade(
                'deal-stale', 4187.9, -0.81, currency='GBP',
                source='broker', overwrite=True)
            broker = FundsBroker([{
                'dealId': 'deal-stale', 'instrumentName': 'Gold',
                'pnl': -0.89, 'currency': 'GBP', 'size': -0.1,
                'openLevel': 4188.5, 'closeLevel': 4187.9,
                'dateUtc': '2026-11-23T15:49:00Z',
            }])
            e.attach(broker)
            n = await e._mirror_capital_funds_closes()
            assert n == 1
            row = next(r for r in store.trades(5) if r['deal_id'] == 'deal-stale')
            assert row['pnl'] == pytest.approx(-0.89)
            assert row['pnl_source'] == 'broker'
    asyncio.run(scenario())


def test_mirror_fuzzy_binds_funds_onto_uuid_when_ref_misses():
    """Funds reference ≠ UUID — still overwrite stale confirm £ on the robot deal."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            store.upsert_trade({
                'dealId': 'uuid-abc-111', 'epic': 'GOLD', 'size': '0.1',
                'direction': 'BUY', 'level': 4188.5,
            })
            store.close_trade(
                'uuid-abc-111', 4187.9, -0.81, currency='GBP',
                source='broker', overwrite=True)
            # Force closed stamp near Funds time so fuzzy scores.
            store.db.execute(
                "UPDATE trades SET closed=? WHERE deal_id=?",
                ('2026-11-23T15:49:05Z', 'uuid-abc-111'))
            store.db.commit()
            broker = FundsBroker([{
                'reference': 'DIFFFF', 'instrumentName': 'Gold',
                'pnl': -0.89, 'currency': 'GBP', 'size': -0.1,
                'openLevel': 4188.5, 'closeLevel': 4187.9,
                'dateUtc': '2026-11-23T15:49:00Z',
            }])
            e.attach(broker)
            n = await e._mirror_capital_funds_closes()
            assert n >= 1
            row = next(r for r in store.trades(8) if r['deal_id'] == 'uuid-abc-111')
            assert row['pnl'] == pytest.approx(-0.89)
            # Must not leave Desk "last" as a separate orphan with the same £
            # while UUID stays stale — UUID is the operator's last close.
            assert row['pnl_source'] == 'broker'
    asyncio.run(scenario())


def test_reaudit_on_penny_funds_upgrade():
    """Cycle must re-diagnose when Funds moves −0.81 → −0.89."""
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            first = e._run_mistake_audit_close(
                'deal-penny', pnl=-0.81, direction='BUY', entry=4188.0,
                source='live')
            assert first and first.get('audited')
            second = e._run_mistake_audit_close(
                'deal-penny', pnl=-0.89, direction='BUY', entry=4188.0,
                source='live_funds_mirror')
            assert second and second.get('audited')
    asyncio.run(scenario())


def test_backfill_analyses_all_journal_closes_not_just_last():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            brain.tick(3600, .05, .01, 'GOLD')
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD', 'size': '0.1'}
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            for i, pnl in enumerate((-0.89, 0.62, -0.27, 0.33)):
                deal = f'deal-all-{i}'
                store.upsert_trade({
                    'dealId': deal, 'epic': 'GOLD', 'size': '0.1',
                    'direction': 'BUY' if pnl > 0 else 'SELL', 'level': 4100 + i,
                })
                store.close_trade(
                    deal, 4100 + i + 0.5, pnl, currency='GBP',
                    source='broker', overwrite=True)
                closed = (now - timedelta(minutes=10 * i)).isoformat()
                store.db.execute(
                    'UPDATE trades SET closed=? WHERE deal_id=?', (closed, deal))
            store.db.commit()
            n = e._backfill_mistake_audits_from_journal(limit=20, hours=24)
            assert n >= 4
            from app import shadow_mistake_audit as sma
            lab = sma.lab_payload(store)
            assert int((lab.get('window_24h') or {}).get('n') or 0) >= 4
            assert len(lab.get('recent_audits') or []) >= 4
    asyncio.run(scenario())


def test_trade_stats_shows_mirrored_funds_rows():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store, brain = new_brain(d)
            e = SingleTradeEngine(store, brain)
            e.mandate = {'epic': 'GOLD'}
            e.account = {'accounts': [
                {'preferred': True, 'currency': 'GBP', 'accountId': '1'}]}
            broker = FundsBroker([{
                'reference': 'FX', 'instrumentName': 'Gold',
                'pnl': -0.18, 'currency': 'GBP', 'size': 0.1,
                'dateUtc': '2026-11-23T11:15:00Z',
            }])
            e.attach(broker)
            await e._mirror_capital_funds_closes()
            from app.runtime_hub import RuntimeHub
            hub = RuntimeHub.__new__(RuntimeHub)
            stats = hub._trade_stats(store, 'GBP')
            assert stats['cash_trades'] >= 1
            assert stats['closed_trades'] >= 1
            assert any(abs(float(t['pnl']) - (-0.18)) < 1e-9
                       for t in stats['recent_trades'])
            assert all(t['pnl_source'] == 'broker'
                       for t in stats['recent_trades'])
    asyncio.run(scenario())
