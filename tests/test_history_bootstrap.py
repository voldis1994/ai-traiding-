"""Eager broker history bootstrap — no waiting for live M1 accumulation."""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import Store, SingleTradeEngine, AdaptiveBrain
from app.feature_validation import recommended_live_history_bars


BASE = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _price_row(i, *, px=2000.0):
    t = BASE - timedelta(minutes=300 - i)
    mid = px + i * 0.01
    return {
        'snapshotTimeUTC': t.strftime('%Y-%m-%dT%H:%M:%S'),
        'openPrice': {'bid': mid - 0.01, 'ask': mid + 0.01},
        'highPrice': {'bid': mid, 'ask': mid + 0.02},
        'lowPrice': {'bid': mid - 0.02, 'ask': mid},
        'closePrice': {'bid': mid - 0.005, 'ask': mid + 0.005},
    }


class FakeBroker:
    def __init__(self, n=300):
        self.n = n
        self.calls = []

    async def prices(self, epic, resolution='MINUTE', maximum=80,
                     from_time=None, to_time=None):
        self.calls.append({
            'epic': epic, 'maximum': maximum,
            'from_time': from_time, 'to_time': to_time,
        })
        # Most-recent `maximum` bars (or all if fewer).
        rows = [_price_row(i) for i in range(self.n)]
        return {'prices': rows[-int(maximum):]}

    async def accounts(self):
        return {'accounts': [{'accountId': '1', 'currency': 'GBP'}]}


def test_bootstrap_pulls_full_need_in_one_shot_without_live_wait():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        eng = SingleTradeEngine(store, brain)
        need = recommended_live_history_bars(store)
        eng.broker = FakeBroker(n=max(400, need + 50))
        eng.mandate = {'epic': 'GOLD', 'size': '0.1'}
        assert need >= 260
        assert eng._historical_candle_count('GOLD') == 0
        got = asyncio.run(eng._bootstrap_historical_candles('GOLD', need=need))
        assert got >= need
        assert eng._historical_candle_count('GOLD') >= need
        assert eng.broker.calls[0]['maximum'] == need
        # First page had enough — no from/to paging required.
        assert eng.broker.calls[0]['from_time'] is None


def test_account_and_candles_eager_when_history_thin():
    """Even mid-cadence, thin history forces bootstrap (no 20-tick wait)."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        eng = SingleTradeEngine(store, brain)
        need = recommended_live_history_bars(store)
        eng.broker = FakeBroker(n=max(400, need + 50))
        eng.mandate = {'epic': 'GOLD', 'size': '0.1'}
        eng.tick_count = 1
        eng.last_candle_tick = 0  # would NOT be due under 20-tick rule
        eng.last_account_tick = 1
        asyncio.run(eng._account_and_candles())
        assert eng._historical_candle_count('GOLD') >= need
        assert len(eng.broker.calls) >= 1


def test_account_and_candles_forces_full_pull_on_missing_minute_gap():
    """After bootstrap_done, a 60m hole must re-download NOW — not wait 60 polls."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        eng = SingleTradeEngine(store, brain)
        need = recommended_live_history_bars(store)
        eng.broker = FakeBroker(n=max(400, need + 50))
        eng.mandate = {'epic': 'GOLD', 'size': '0.1'}
        # Pretend bootstrap already finished (count-based) and sparse cadence
        # would skip (last pull was this tick).
        store.set('candle_bootstrap_done:GOLD', {'at': '2026-11-15T12:00:00+00:00',
                                                 'need': need, 'have': need})
        # Mid sparse window (gap=60): OLD path would skip; hole must force pull.
        eng.tick_count = 100
        eng.last_candle_tick = 50
        eng.last_account_tick = 100
        eng.market_context = {
            'ready': False,
            'reason': 'Missing MINUTE candles in 60-minute context; waiting for contiguous history',
        }
        before = len(eng.broker.calls)
        asyncio.run(eng._account_and_candles())
        assert len(eng.broker.calls) > before
        assert eng.broker.calls[-1]['maximum'] == need
        assert eng._historical_candle_count('GOLD') >= need
