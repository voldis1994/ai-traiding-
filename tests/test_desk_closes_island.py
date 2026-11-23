"""Desk CLIENT CLOSES island: journal upsert + edge-hist fallback + samples cards."""
import tempfile
from pathlib import Path

from app.engine import Store
from app.runtime_hub import RuntimeHub
from app import unified_cycle


def test_close_trade_inserts_when_open_row_missing():
    """Capital close must journal even if OPEN upsert was missed."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.close_trade(
            'orphan-1', 3615.2, -0.28, currency='GBP', source='broker',
            overwrite=True, epic='GOLD', size='0.1', direction='SELL',
            entry=3618.0)
        rows = store.trades(5)
        assert any(r['deal_id'] == 'orphan-1' for r in rows)
        row = next(r for r in rows if r['deal_id'] == 'orphan-1')
        assert row['closed'] is not None
        assert row['pnl'] == -0.28
        assert row['epic'] == 'GOLD'
        assert row['direction'] == 'SELL'
        assert row['pnl_source'] == 'broker'


def test_close_trade_updates_existing_without_duplicate():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.upsert_trade({
            'dealId': 'live-1', 'epic': 'GOLD', 'size': '0.1',
            'direction': 'BUY', 'level': 3600,
        })
        store.close_trade('live-1', 3605, 0.64, currency='GBP', source='broker')
        store.close_trade(
            'live-1', 3605, 0.64, currency='GBP', source='broker', overwrite=True)
        rows = [r for r in store.trades(20) if r['deal_id'] == 'live-1']
        assert len(rows) == 1
        assert rows[0]['pnl'] == 0.64


def test_trade_stats_falls_back_to_edge_hist_when_journal_empty():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.set(unified_cycle.EDGE_OUTCOME_HIST_KEY, [
            {'pnl': 0.30, 'epic': 'GOLD', 'direction': 'SELL',
             'deal_id': 'e1', 'note': 'ease_floor_after_win'},
            {'pnl': -0.13, 'epic': 'GOLD', 'direction': 'BUY',
             'deal_id': 'e2', 'note': 'raise_floor_after_loss'},
        ])
        hub = RuntimeHub.__new__(RuntimeHub)
        stats = hub._trade_stats(store, 'GBP')
        assert stats['closed_trades'] == 0
        recent = stats['recent_trades']
        assert len(recent) == 2
        assert recent[0]['from_edge_hist'] is True
        assert recent[0]['pnl_source'] == 'broker'
        assert {r['pnl'] for r in recent} == {0.30, -0.13}


def test_desk_samples_active_n_follows_strategy_cards():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        from app import strategy_gen, sample_strategies
        strategy_gen.save(store, {
            'active_ids': [
                'GOLD|TREND_DOWN|SELL',
                'GOLD|BREAKOUT_DOWN|SELL',
                'GOLD|TREND_UP|BUY',
                'GOLD|RANGE|HOLD',
                'GOLD|BREAKOUT_UP|BUY',
            ],
        })
        # Sample flag left stale at active_n=0 (Desk used to show "cards 0").
        store.set(sample_strategies.STORE_FLAG, {
            'done': True, 'library_samples': 1, 'trades': 0,
            'source': None, 'active_n': 0,
        })
        hub = RuntimeHub.__new__(RuntimeHub)

        class _RT:
            pass

        rt = _RT()
        rt.store = store
        out = hub._desk_system_payload(rt, snap={})
        assert out['cards']['n'] == 5
        assert out['samples']['active_n'] == 5
        assert out['samples']['source'] == 'strategy_cards'
        assert out['samples']['library_samples'] == 1
        assert out['samples']['display_n'] == 1  # library wins when >0


def test_desk_samples_display_n_not_zero_when_cards_live():
    """Bare library 0 must not headline the Samples tile while cards exist."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        from app import strategy_gen, sample_strategies
        strategy_gen.save(store, {
            'active_ids': ['GOLD|TREND_UP|BUY', 'OIL|BREAKOUT_UP|BUY'],
        })
        store.set(sample_strategies.STORE_FLAG, {
            'done': False, 'library_samples': 0, 'trades': 0,
            'source': None, 'active_n': 0,
        })
        store.upsert_trade({
            'dealId': 'c1', 'epic': 'OIL_CRUDE', 'size': '0.1',
            'direction': 'SELL', 'level': 83,
        })
        store.close_trade('c1', 82.8, -0.22, currency='EUR', source='upl_estimate')
        hub = RuntimeHub.__new__(RuntimeHub)

        class _RT:
            pass

        rt = _RT()
        rt.store = store
        # Stale snap shadow at 0 must lose to empty live journal (both 0 OK),
        # but samples headline must not be 0.
        out = hub._desk_system_payload(rt, snap={
            'self_development': {
                'shadow_paper': {
                    'paper_net': 0.0, 'trades': 0, 'cash_trades': 0,
                    'wins': 0, 'losses': 0,
                },
            },
        })
        assert out['cards']['n'] == 2
        assert out['samples']['library_samples'] == 0
        assert out['samples']['active_n'] == 2
        assert out['samples']['closed_trades'] == 1
        assert out['samples']['display_n'] == 2
        assert out['samples']['display_n'] > 0


def test_observe_close_hist_carries_desk_fields():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        unified_cycle.set_calibrated_floor(store, 0.11, method='test')
        unified_cycle.observe_close(
            store, broker_pnl=-0.39, spread=0.5,
            entry_meta={
                'score_edge': 0.05, 'deal_id': 'd9', 'epic': 'GOLD',
                'direction': 'SELL', 'currency': 'GBP',
            })
        hist = store.get(unified_cycle.EDGE_OUTCOME_HIST_KEY) or []
        assert hist
        last = hist[-1]
        assert last['deal_id'] == 'd9'
        assert last['epic'] == 'GOLD'
        assert last['direction'] == 'SELL'
        assert last['source'] == 'broker'
