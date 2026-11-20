"""Strategy generator — harvest library EV → soft LIVE cards."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import strategy_gen as sg
from app import structure_shadow as ss


def _store(tmp):
    return Store(Path(tmp) / 't.sqlite3')


def _seed(store, *, structure='TREND_DOWN', buy_pnl=-40.0, sell_pnl=20.0,
          buy_n=50, sell_n=40):
    lib = ss.load_library(store)
    buckets = dict(lib.get('buckets') or {})
    buckets[ss.frame_key(epic='GOLD', structure=structure, direction='BUY')] = {
        'samples': buy_n, 'total_pnl': buy_pnl, 'win_count': 0, 'loss_count': buy_n,
    }
    buckets[ss.frame_key(epic='GOLD', structure=structure, direction='SELL')] = {
        'samples': sell_n, 'total_pnl': sell_pnl, 'win_count': sell_n - 5,
        'loss_count': 5,
    }
    lib['buckets'] = buckets
    store.set(ss.STORE_KEY, lib)


def test_harvest_activates_paying_side_card():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed(s)
        out = sg.harvest(s, epic='GOLD')
        assert out['ok'] is True
        assert out['activated']
        cards = sg.load(s)['cards']
        # Prefer SELL on TREND_DOWN
        assert any(c.get('prefer') == 'SELL' and c.get('structure') == 'TREND_DOWN'
                   for c in cards.values())


def test_soft_nudge_uses_active_card_on_live_structure():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed(s)
        sg.harvest(s, epic='GOLD')
        scores = {'BUY': 0.70, 'SELL': 0.20, 'HOLD': 0.10}
        nudged, meta = sg.soft_nudge_scores(
            scores, structure='TREND_DOWN', epic='GOLD', store=s)
        assert meta['nudged'] is True
        assert nudged['SELL'] > nudged['BUY']
        assert abs(sum(nudged.values()) - 1.0) < 1e-9


def test_observe_close_attributes_live_pnl():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed(s)
        sg.harvest(s, epic='GOLD')
        before = sg.load(s)
        cid = before['active_ids'][0]
        sg.observe_close(
            s, structure='TREND_DOWN', direction='SELL', broker_pnl=1.5, epic='GOLD')
        after = sg.load(s)
        assert after['cards'][cid]['live_n'] >= 1
        assert float(after['cards'][cid]['live_pnl']) > 0
