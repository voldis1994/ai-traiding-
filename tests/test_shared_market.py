"""Same-epic MTF/OHLC perception shared across clients."""
import tempfile
from pathlib import Path

from app import shared_market as sm
from app.engine import Store


def setup_function():
    sm.clear()


def teardown_function():
    sm.clear()


def test_publish_and_adopt_same_epic():
    analysis = {
        'ready': True, 'mode': 'SHIFT', 'preferred': 'BUY',
        'confidence': 0.74, 'epic': 'GOLD', 'bars_m1': 500,
        'stop_hint_atr14': 6.69,
    }
    pub = sm.publish_mtf(
        'GOLD', analysis, bars_m1=500, last_at='2026-11-19T17:34:00+00:00',
        source='client:2')
    assert pub['mode'] == 'SHIFT'
    assert pub['shared'] is True
    got = sm.adopt_mtf(
        'GOLD', local_bars=480, local_last_at='2026-11-19T17:34:00+00:00')
    assert got is not None
    assert got['mode'] == 'SHIFT'
    assert got['preferred'] == 'BUY'
    assert got['shared'] is True


def test_adopt_rejects_stale_head():
    sm.publish_mtf(
        'GOLD',
        {'ready': True, 'mode': 'HOLD', 'preferred': 'HOLD', 'epic': 'GOLD'},
        bars_m1=400, last_at='2026-11-19T17:30:00+00:00')
    assert sm.adopt_mtf(
        'GOLD', local_bars=400,
        local_last_at='2026-11-19T17:35:00+00:00') is None


def test_richer_peer_wins_publish():
    sm.publish_mtf(
        'GOLD',
        {'ready': True, 'mode': 'BUY', 'preferred': 'BUY', 'epic': 'GOLD'},
        bars_m1=600, last_at='2026-11-19T17:34:00+00:00', source='a')
    # Thinner tape must not overwrite.
    out = sm.publish_mtf(
        'GOLD',
        {'ready': True, 'mode': 'SELL', 'preferred': 'SELL', 'epic': 'GOLD'},
        bars_m1=200, last_at='2026-11-19T17:34:00+00:00', source='b')
    assert out['mode'] == 'BUY'
    assert out.get('shared_kept') == 'richer_peer'


def test_mirror_historical_candles_to_peer_store():
    with tempfile.TemporaryDirectory() as d:
        a = Store(Path(d) / 'a.sqlite3')
        b = Store(Path(d) / 'b.sqlite3')

        class Eng:
            def __init__(self, store, epic):
                self.store = store
                self.mandate = {'epic': epic}

        bars = [{
            'at': '2026-11-19T17:00:00+00:00',
            'open': 3680.0, 'high': 3681.0, 'low': 3679.0, 'close': 3680.5,
        }]
        peer = Eng(b, 'GOLD')
        n = sm.mirror_historical_candles([peer], 'GOLD', bars)
        assert n == 1
        got = b.candles('GOLD', n=5, historical_only=True)
        assert len(got) == 1
        assert float(got[0]['close']) == 3680.5


def test_fingerprint_uses_last_bar():
    rows = [
        {'at': '2026-11-19T17:33:00+00:00', 'close': 1.0},
        {'at': '2026-11-19T17:34:00+00:00', 'close': 2.5},
    ]
    assert sm.fingerprint(rows) == (2, '2026-11-19T17:34:00+00:00', 2.5)
