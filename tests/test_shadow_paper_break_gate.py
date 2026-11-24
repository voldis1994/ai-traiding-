"""Paper OPEN respects zone/structure gate veto when provided (V3 H02/H04)."""
import tempfile
from pathlib import Path

from app import ohlc_entry_zones
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.shadow_paper import ShadowPaperJournal


def test_paper_open_respects_zone_gate_veto():
    """When zone_gate.allowed is False, journal must not OPEN."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.on_bar(
            'BUY', mid=2000., spread=0.2, bar_time='2026-11-23T13:00:00+00:00',
            high=2001., low=1999., tick=0.1, min_distance=0.5,
            stop_distance=4.5, profit_distance=9.0,
            slip_bps=0., fill_delay_bars=0,
            zone_gate={
                'allowed': False, 'trigger': None, 'action': 'BUY',
                'reason': 'wait_entry_zone (no_touch)',
            })
        assert j.position is None
        assert j.pending is None


def test_paper_open_respects_structure_gate_veto():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.on_bar(
            'SELL', mid=2000., spread=0.2, bar_time='t-struct',
            high=2001., low=1999., tick=0.1,
            stop_distance=4.5, profit_distance=9.0,
            slip_bps=0., fill_delay_bars=0,
            structure_gate={'allowed': False, 'reason': 'HOLD_ZONE'})
        assert j.position is None
        assert j.pending is None


def test_paper_open_respects_entry_context_ohlc_allowed_false():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.on_bar(
            'BUY', mid=2000., spread=0.2, bar_time='t-ctx',
            high=2001., low=1999., tick=0.1,
            stop_distance=4.5, profit_distance=9.0,
            slip_bps=0., fill_delay_bars=0,
            entry_context={
                'ohlc_entry': {
                    'allowed': False, 'trigger': None,
                    'reason': 'wait_entry_zone',
                },
            })
        assert j.position is None


def test_paper_open_when_zone_gate_allows():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.on_bar(
            'BUY', mid=2000., spread=0.2, bar_time='t-ok',
            high=2001., low=1999., tick=0.1,
            stop_distance=4.5, profit_distance=9.0,
            slip_bps=0., fill_delay_bars=0,
            zone_gate={
                'allowed': True, 'trigger': 'break', 'action': 'BUY',
                'reason': 'ohlc_break_trigger',
            })
        assert j.position is not None
        assert j.position['direction'] == 'BUY'


def test_paper_open_without_gate_kwargs_unchanged():
    """Omitted gates do not block — additive signature for engine wiring."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.on_bar(
            'BUY', mid=2000., spread=0.2, bar_time='t-nogate',
            high=2001., low=1999., tick=0.1,
            stop_distance=4.5, profit_distance=9.0,
            slip_bps=0., fill_delay_bars=0)
        assert j.position is not None


def test_pending_fill_cancelled_when_zone_gate_vetoes():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.on_bar(
            'BUY', mid=2000., spread=0.2, bar_time='t0',
            high=2001., low=1999., tick=0.1,
            stop_distance=4.5, profit_distance=9.0,
            slip_bps=0., fill_delay_bars=1)
        assert j.pending is not None
        j.on_bar(
            'HOLD', mid=2000.1, spread=0.2, bar_time='t1',
            high=2001., low=1999., tick=0.1,
            slip_bps=0., fill_delay_bars=1,
            zone_gate={'allowed': False, 'reason': 'HOLD_ZONE'})
        assert j.pending is None
        assert j.position is None


def test_paper_opens_on_break_trigger(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': 1}
        e.market = {
            'mid': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
            'bid': 1999.9, 'ask': 2000.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        e._ensure_shadow_brain()
        monkeypatch.setattr(
            e, '_would_be_brackets',
            lambda proposal: {
                'entry': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
                'stop_distance': 4.5, 'profit_distance': 9.0,
                'bracket_source': 'swing_anchor', 'ohlc_kappa': 2.0,
                'epic': 'GOLD',
            })
        monkeypatch.setattr(
            e, '_shadow_scores_now',
            lambda **kw: {'BUY': 0.9, 'SELL': 0.05, 'HOLD': 0.05})
        monkeypatch.setattr(
            e, '_paper_entry_gate',
            lambda action, mtf_analysis=None: {
                'allowed': True, 'trigger': 'break', 'action': action,
                'reason': 'ohlc_break_trigger',
                'zone': {'lo': 1995., 'hi': 1998., 'mid': 1996.5},
                'scenario': {'scenario': 'BREAKOUT_UP', 'scenarios': ['BREAKOUT_UP']},
            })
        from app import self_params
        monkeypatch.setattr(
            self_params, 'load',
            lambda store: {**self_params.DEFAULTS, 'exec_slip_bps': 0.,
                           'exec_fill_delay_bars': 0})
        e._shadow_paper_step(
            mid=2000., spread=0.2, bar_time='2026-11-23T13:01:00+00:00',
            high=2001., low=1999., candles=[])
        pos = e.shadow_paper.position
        assert pos is not None
        assert (pos.get('entry_context') or {}).get('ohlc_entry', {}).get(
            'trigger') == 'break'


def test_paper_close_adapts_break_knobs(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': 1}
        e.market = {
            'mid': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
            'bid': 1999.9, 'ask': 2000.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        shadow = e._ensure_shadow_brain()
        before = ohlc_entry_zones.load_params(store)
        tape_before = int(before.get('tape_need') or 1)
        e.shadow_paper.cash_scale = 1.0
        e.shadow_paper.position = {
            'direction': 'BUY', 'entry': 2000.,
            'stop': 1995., 'profit': 2010.,
            'original_risk': 5.0, 'tick': 0.1, 'min_distance': 0.5,
            'opened_at': 'seed', 'bar_time': 'seed',
            'model_version': 0, 'kappa': 1.0, 'sl_scale': 1.0,
            'shadow_transition': dict(shadow.last_transition)
            if getattr(shadow, 'last_transition', None) else None,
            'trail_extreme': None,
            'entry_context': {
                'ohlc_entry': {
                    'action': 'BUY', 'trigger': 'break',
                    'reason': 'ohlc_break_trigger',
                },
                'scenario': {'scenario': 'BREAKOUT_UP'},
            },
        }
        monkeypatch.setattr(
            e, '_shadow_scores_now',
            lambda **kw: {'BUY': 0.1, 'SELL': 0.1, 'HOLD': 0.8})
        from app import self_params
        monkeypatch.setattr(
            self_params, 'load',
            lambda store: {**self_params.DEFAULTS, 'exec_slip_bps': 0.,
                           'exec_fill_delay_bars': 0})
        closed = e._shadow_paper_step(
            mid=1994., spread=0.2, bar_time='2026-11-23T13:02:00+00:00',
            high=1996., low=1993., candles=[])
        assert closed is not None
        assert closed.get('exit_reason') == 'sl'
        after = ohlc_entry_zones.load_params(store)
        assert int(after.get('tape_need') or 0) >= tape_before
        assert float(after.get('break_mtf_conf') or 0) >= float(
            before.get('break_mtf_conf') or 0)
