"""Unified adaptive system — boot → OPEN nudge → CLOSE learn."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import adaptive_system as ads
from app import strategy_gen as sg


def test_nudge_open_tilts_toward_paying_combo():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        for _ in range(5):
            sg.record_combo(
                store, epic='GOLD', structure='TREND_DOWN', direction='BUY',
                broker_pnl=-0.5)
            sg.record_combo(
                store, epic='GOLD', structure='TREND_DOWN', direction='SELL',
                broker_pnl=0.4)
        scores = {'BUY': 0.55, 'SELL': 0.30, 'HOLD': 0.15}
        nudged, meta = ads.nudge_open(
            store, scores, epic='GOLD', structure='TREND_DOWN', trend='DOWN')
        assert meta['system'] == 'adaptive_system'
        assert meta['nudged'] is True
        assert 'strategy_gen' in meta['stages']
        assert nudged['SELL'] > nudged['BUY']
        assert abs(sum(nudged.values()) - 1.0) < 1e-9


def test_learn_close_banks_combo_and_can_flip():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        cid = sg.card_id(epic='GOLD', structure='TREND_DOWN', prefer='BUY')
        bag = sg.load(store)
        bag['cards'][cid] = {
            'id': cid, 'epic': 'GOLD', 'structure': 'TREND_DOWN',
            'prefer': 'BUY', 'avoid': 'SELL', 'mean_pnl': 0.1, 'samples': 8,
            'status': 'active', 'live_pnl': 0.0, 'live_n': 0, 'source': 'test',
        }
        bag['active_ids'] = [cid]
        sg.save(store, bag)
        for _ in range(3):
            out = ads.learn_close(
                store, structure='TREND_DOWN', direction='BUY',
                broker_pnl=-0.6, epic='GOLD')
        assert out['ok']
        assert (out.get('strategy') or {}).get('flips')
        snap = ads.snapshot(store)
        assert snap['combo_count'] >= 1
        bleeding = [r for r in snap['bleeding'] if 'BUY' in r['key']]
        assert bleeding


def test_boot_sets_status():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        out = ads.boot(store, epic='GOLD')
        assert out['ok']
        assert out['booted']
        snap = ads.snapshot(store)
        assert snap['system'] == 'adaptive_system'
