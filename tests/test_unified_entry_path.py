"""Unified intent → break|zone trigger path (Admin vocabulary + engine helpers)."""
from app.engine import SingleTradeEngine
from app import ohlc_entry_zones as oez
from tests.test_learning_integrity import new_brain
import tempfile


def test_entry_path_from_evaluation_wait_and_open():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        wait_ev = {
            'action': 'HOLD',
            'proposal': 'BUY',
            'reason': 'wait_buy_entry_zone (price 10 > zona 9)',
            'ohlc_entry_zone': {'allowed': False, 'reason': 'wait_buy_entry_zone'},
        }
        path = e._entry_path_from_evaluation(wait_ev)
        assert path['intent'] == 'BUY'
        assert path['status'] == 'wait'
        assert path['trigger'] is None
        assert 'wait_buy' in (path['wait'] or '')

        open_ev = {
            'action': 'BUY',
            'proposal': 'BUY',
            'ohlc_entry_zone': {
                'allowed': True, 'trigger': 'break',
                'reason': 'ohlc_break_structure',
            },
        }
        path2 = e._entry_path_from_evaluation(open_ev)
        assert path2['intent'] == 'BUY'
        assert path2['trigger'] == 'break'
        assert path2['status'] == 'open'


def test_publish_entry_path_arms_intent_event():
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD'}
        e.market = {'mid': 2000.0, 'tick': 0.1}
        e._publish_entry_path({
            'action': 'HOLD', 'proposal': 'BUY',
            'reason': 'wait_buy_entry_zone',
            'ohlc_entry_zone': {'allowed': False},
        })
        assert e._entry_path['intent'] == 'BUY'
        assert e._intent_side == 'BUY'
        rows = store.db.execute(
            "SELECT kind,message FROM events WHERE kind='entry' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        assert any('Intent armed' in (r[1] or '') for r in rows)


def test_break_gate_still_allows_structure_open():
    from tests.ohlc_zone_path import demand_zone_path
    rows, _ = demand_zone_path(confirm=False)
    # Green M1 tape so break is not blocked as against dump.
    last = rows[-1]
    px = float(last['close'])
    for k in range(3):
        o = px + k * 0.4
        c = o + 0.5
        rows.append({
            'at': last['at'], 'open': o, 'high': c + 0.1,
            'low': o - 0.05, 'close': c,
        })
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z.get('buy_entry')
    far = float(buy['hi']) + 5.0 if buy else float(rows[-1]['close']) + 5.0
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.8, 'structure_break': 'UP', 'move_ok': True,
    }
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=far, ask=far, bid=far - 0.1,
        tick=0.1, spread=0.2, mtf=mtf)
    assert gate['allowed'] is True
    assert gate['trigger'] == 'break'


def test_reentry_default_medium_streak_is_one():
    from app import reentry_confirm
    # Current default on this branch (tape need for medium re-entry).
    assert int(reentry_confirm.DEFAULTS['medium_m1_streak']) >= 1


def test_admin_shows_intent_trigger_wait():
    from pathlib import Path
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert '>Intent<' in js or 'Intent</span>' in js
    assert '>Trigger<' in js or 'Trigger</span>' in js
    assert '>Wait<' in js or 'Wait</span>' in js
    assert 'entry_intent' in js
