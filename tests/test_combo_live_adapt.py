"""Learn from wins+losses → combinations → flip live when −EV."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import strategy_gen as sg


def _store(tmp):
    return Store(Path(tmp) / 't.sqlite3')


def test_record_combo_learns_wins_and_losses():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        sg.record_combo(
            s, epic='GOLD', structure='TREND_DOWN', direction='BUY',
            broker_pnl=-1.0)
        sg.record_combo(
            s, epic='GOLD', structure='TREND_DOWN', direction='BUY',
            broker_pnl=-0.5)
        sg.record_combo(
            s, epic='GOLD', structure='TREND_DOWN', direction='SELL',
            broker_pnl=0.8)
        bag = sg.load(s)
        buy = bag['combos'][sg.combo_key(
            epic='GOLD', structure='TREND_DOWN', direction='BUY')]
        sell = bag['combos'][sg.combo_key(
            epic='GOLD', structure='TREND_DOWN', direction='SELL')]
        assert buy['samples'] == 2 and buy['losses'] == 2
        assert buy['mean_pnl'] < 0
        assert sell['mean_pnl'] > 0 and sell['wins'] == 1


def test_observe_close_flips_card_when_live_bleeds():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        cid = sg.card_id(epic='GOLD', structure='TREND_DOWN', prefer='BUY')
        bag = sg.load(s)
        bag['cards'][cid] = {
            'id': cid, 'epic': 'GOLD', 'structure': 'TREND_DOWN',
            'prefer': 'BUY', 'avoid': 'SELL', 'mean_pnl': 0.1, 'samples': 10,
            'status': 'active', 'live_pnl': 0.0, 'live_n': 0,
            'source': 'test',
        }
        bag['active_ids'] = [cid]
        sg.save(s, bag)
        # Three preferred-side losses → flip to SELL.
        for _ in range(3):
            out = sg.observe_close(
                s, structure='TREND_DOWN', direction='BUY',
                broker_pnl=-0.5, epic='GOLD')
        assert out.get('flips')
        card = sg.load(s)['cards'][cid]
        assert card['prefer'] == 'SELL'
        assert card['avoid'] == 'BUY'


def test_soft_nudge_damps_losing_combo_and_boosts_paying():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        for _ in range(5):
            sg.record_combo(
                s, epic='GOLD', structure='TREND_DOWN', direction='BUY',
                broker_pnl=-0.4)
            sg.record_combo(
                s, epic='GOLD', structure='TREND_DOWN', direction='SELL',
                broker_pnl=0.3)
        scores = {'BUY': 0.55, 'SELL': 0.30, 'HOLD': 0.15}
        nudged, meta = sg.soft_nudge_scores(
            scores, structure='TREND_DOWN', epic='GOLD', store=s)
        assert meta['nudged'] is True
        assert nudged['SELL'] > nudged['BUY']
