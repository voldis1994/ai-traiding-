"""Live zone touch / break must OPEN without waiting for M1 candle close."""
import tempfile

from app.engine import SingleTradeEngine
from tests.ohlc_zone_path import demand_zone_path
from tests.test_learning_integrity import new_brain


def test_try_live_ohlc_open_promotes_hold_on_zone_touch():
    """Neural HOLD + live ask inside Demand + MTF BUY → promote BUY."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        brain._activate('GOLD')
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.positions = []
        rows, _ = demand_zone_path(confirm=False)
        from app import ohlc_entry_zones as oez
        z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = z['buy_entry']
        inside = (float(buy['lo']) + float(buy['hi'])) / 2
        e.market = {
            'mid': inside, 'ask': inside, 'bid': inside - 0.1,
            'spread': 0.1, 'tick': 0.1,
        }
        e._decision_candles = lambda *a, **k: rows
        mtf = {
            'ready': True, 'enabled': True, 'mode': 'BUY', 'preferred': 'BUY',
            'confidence': 0.7, 'structure_break': None, 'move_ok': True,
        }
        hold_result = {
            'action': 'HOLD', 'proposal': 'HOLD',
            'scores': {'BUY': 0.25, 'SELL': 0.20, 'HOLD': 0.55},
        }
        out = e._try_live_ohlc_open(
            hold_result,
            base_scores={'BUY': 0.25, 'SELL': 0.20, 'HOLD': 0.55},
            nudged_scores={'BUY': 0.25, 'SELL': 0.20, 'HOLD': 0.55},
            mtf_analysis=mtf)
        assert out is not None
        assert out['action'] == 'BUY'
        assert out['live_ohlc_promote'] is True
        assert out['live_ohlc_trigger'] == 'zone'
        assert 'no_candle_wait' in (out.get('reason') or '')


def test_try_live_ohlc_open_refuses_without_lean():
    """Blind zone touch with flat scores + no MTF must not invent a side."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        brain._activate('GOLD')
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.positions = []
        rows, _ = demand_zone_path(confirm=False)
        from app import ohlc_entry_zones as oez
        z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy = z['buy_entry']
        inside = (float(buy['lo']) + float(buy['hi'])) / 2
        e.market = {
            'mid': inside, 'ask': inside, 'bid': inside - 0.1,
            'spread': 0.1, 'tick': 0.1,
        }
        e._decision_candles = lambda *a, **k: rows
        mtf = {
            'ready': True, 'mode': 'HOLD', 'preferred': 'HOLD',
            'confidence': 0.2, 'structure_break': None,
        }
        out = e._try_live_ohlc_open(
            {'action': 'HOLD'},
            base_scores={'BUY': 0.33, 'SELL': 0.33, 'HOLD': 0.34},
            nudged_scores={'BUY': 0.33, 'SELL': 0.33, 'HOLD': 0.34},
            mtf_analysis=mtf)
        assert out is None


def test_wants_fast_entry_ticks_while_flat_with_signal():
    """Fast ticks stay on while flat so zone touch does not wait for bar close."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': '0.02'}
        e.positions = []
        e.operator_paused = False
        e.last_closed_step = 1
        e.last_signal = {'scores': {'BUY': 0.2, 'SELL': 0.2, 'HOLD': 0.6}}
        assert e._wants_fast_entry_ticks() is True
