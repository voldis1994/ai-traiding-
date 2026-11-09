"""Live-data repair: structure HOLD teach, gave-then-lost, win frames, exit path."""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.engine import Store
from app import structure_shadow as ss
from app import hold_shadow as hs
from app.bracket_learn import infer_exit_reason_from_path
from app.profit_learning import label_from_profit, path_excursion


def _down_bars(n=12, start=100.0, step=0.5):
    rows = []
    px = start
    for i in range(n):
        o, c = px, px - step
        rows.append({
            'at': f'2026-01-01T00:{i:02d}:00+00:00',
            'open': o, 'high': o + 0.05, 'low': c - 0.05, 'close': c, 'mid': c,
        })
        px = c
    return rows


def _gave_then_lost_buy(n_up=6, n_down=8, start=100.0):
    """BUY path: rises first (MFE), then crashes through entry — classic live grave."""
    rows = []
    px = start
    t = 0
    for _ in range(n_up):
        o, c = px, px + 0.8
        rows.append({
            'at': f'2026-01-01T01:{t:02d}:00+00:00',
            'open': o, 'high': c + 0.05, 'low': o - 0.05, 'close': c, 'mid': c,
        })
        px = c
        t += 1
    for _ in range(n_down):
        o, c = px, px - 1.2
        rows.append({
            'at': f'2026-01-01T01:{t:02d}:00+00:00',
            'open': o, 'high': o + 0.05, 'low': c - 0.05, 'close': c, 'mid': c,
        })
        px = c
        t += 1
    return rows


def test_structure_prefer_hold_overrides_flip():
    """Lethal structure memory → HOLD teach even when opposite CF looks green."""
    bars = _down_bars()
    # Without structure: clear down move → SELL opposite.
    raw = label_from_profit(
        'BUY', -0.40, entry=100., exit_level=94., spread=0.5, bars=bars,
        stop_distance=2.0, profit_distance=2.0, tick=0.1, trail=False)
    assert raw['label'] == 1  # SELL
    # With structure memory prefer-hold: same path → HOLD.
    fixed = label_from_profit(
        'BUY', -0.40, entry=100., exit_level=94., spread=0.5, bars=bars,
        stop_distance=2.0, profit_distance=2.0, tick=0.1, trail=False,
        structure_prefer_hold=True)
    assert fixed['label'] == 2
    assert 'structure_hold' in fixed['mode']


def test_gave_then_lost_prefers_hold_over_weak_flip():
    bars = _gave_then_lost_buy()
    exc = path_excursion(bars, 'BUY', 100.)
    assert exc['mfe'] is not None and exc['mfe'] > 1.0
    # Endpoint/path may favour SELL after the crash; MFE should block weak flips.
    d = label_from_profit(
        'BUY', -0.34, entry=100., exit_level=bars[-1]['close'], spread=0.5,
        bars=bars, stop_distance=5.0, profit_distance=5.0, tick=0.1, trail=False,
        path_mfe=exc['mfe'])
    # Either HOLD via gave-then-hold, or a strong opposite that cleared the MFE floor.
    if d['label'] == 1:
        assert d['simulated']['sell'] > 0.35 * exc['mfe']
    else:
        assert d['label'] == 2
        assert 'gave_then_hold' in d['mode'] or d['mode'].endswith('hold')


def test_record_win_dilutes_hostile_library():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        bars = _down_bars()
        for i in range(3):
            ss.record_loss(
                store, epic='GOLD', direction='BUY',
                entry=100., exit_level=94., exit_reason='sl',
                broker_pnl=-0.2, bars=bars, deal_id=f'l{i}')
        frame = ss.frame_from_ohlc(bars)
        structure = frame['structure']
        pref = ss.library_prefers_hold(
            store, epic='GOLD', structure=structure, direction='BUY')
        assert pref['prefer_hold'] is True
        # Wins on the *same* key dilute hostility (honest rate, not a hard veto).
        for i in range(4):
            ss.record_win(
                store, epic='GOLD', direction='BUY',
                entry=100., exit_level=94., exit_reason='tp',
                broker_pnl=0.3, bars=bars, deal_id=f'w{i}')
        lib = ss.load_library(store)
        key = ss.frame_key(epic='GOLD', structure=structure, direction='BUY')
        bucket = lib['buckets'][key]
        assert bucket['win_count'] == 4
        assert bucket['samples'] == 7
        pref2 = ss.library_prefers_hold(
            store, epic='GOLD', structure=structure, direction='BUY')
        # 3/7 loss rate < 0.67 gate → no longer prefer HOLD.
        assert pref2['prefer_hold'] is False


def test_infer_exit_reason_from_ohlc_path():
    # BUY: stop first at 98, then would have hit TP later — classify SL.
    bars = [
        {'high': 100.2, 'low': 99.5, 'close': 99.8, 'mid': 99.8},
        {'high': 99.0, 'low': 97.8, 'close': 98.0, 'mid': 98.0},  # SL
        {'high': 103.0, 'low': 98.0, 'close': 102.5, 'mid': 102.5},
    ]
    assert infer_exit_reason_from_path(
        direction='BUY', entry=100., exit_level=98., bars=bars,
        stop_distance=2.0, profit_distance=3.0, tick=0.1) == 'sl'
    # SELL: profit first.
    bars_tp = [
        {'high': 100.2, 'low': 99.0, 'close': 99.2, 'mid': 99.2},
        {'high': 99.0, 'low': 97.0, 'close': 97.5, 'mid': 97.5},  # TP at -3
    ]
    assert infer_exit_reason_from_path(
        direction='SELL', entry=100., exit_level=97., bars=bars_tp,
        stop_distance=2.0, profit_distance=3.0, tick=0.1) == 'tp'


def test_hold_shadow_teaches_hold_when_both_lost():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        transition = {
            'z': [0.1] * 8, 'h': [0.0] * 4, 'c': [0.0] * 4,
            'dt': 60., 'neuron_ids': list(range(4)),
        }
        hs.enqueue(
            store, proposal='BUY', reason='weak edge',
            bar_time='b0', epic='GOLD', entry=100.0, spread=0.5,
            stop_distance=3.0, profit_distance=3.0, tick=0.1,
            min_distance=0.2, transition=transition, trail=False)
        # Flat path: spread costs both sides → both_lost, teach HOLD.
        flat = [
            {'mid': 100.0, 'high': 100.05, 'low': 99.95, 'spread': 0.5, 'at': f'f{i}'}
            for i in range(10)
        ]
        taught = []

        def learn(label, tr, mode, detail):
            taught.append((label, mode))

        outs = hs.resolve_ready(
            store, candles_after=lambda *_: flat, learn_callback=learn, horizon=8)
        assert outs
        assert outs[0]['taught'] is True
        assert taught and taught[0][0] == hs._ACTION_LABEL['HOLD']
        assert 'hold_both_lost' in taught[0][1]
        assert hs.load_stats(store)['taught_hold'] == 1


def test_hold_shadow_teaches_proposal_when_it_hits_tp():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'db.sqlite3')
        transition = {
            'z': [0.2] * 8, 'h': [0.0] * 4, 'c': [0.0] * 4,
            'dt': 60., 'neuron_ids': list(range(4)),
        }
        hs.enqueue(
            store, proposal='BUY', reason='weak edge',
            bar_time='b0', epic='GOLD', entry=100.0, spread=0.2,
            stop_distance=1.0, profit_distance=1.0, tick=0.1,
            min_distance=0.2, transition=transition)
        # Strong up path — proposal BUY hits TP; opposite SELL does not.
        up = []
        px = 100.0
        for i in range(20):
            o, c = px, px + 0.5
            up.append({
                'mid': c, 'high': c + 0.05, 'low': o - 0.05,
                'spread': 0.2, 'at': f'u{i}'})
            px = c
        taught = []

        def learn(label, tr, mode, detail):
            taught.append((label, mode))

        outs = hs.resolve_ready(
            store, candles_after=lambda *_: up, learn_callback=learn, horizon=20)
        assert outs
        assert outs[0]['proposal_hit_tp'] is True
        assert outs[0]['taught'] is True
        assert taught[0][0] == hs._ACTION_LABEL['BUY']
        assert 'hold_missed_proposal_tp_buy' in taught[0][1]
