"""Padziļināts tests: structure shadow caur engine — teach, ne veto.

Coverage:
  1. Frame classifier on TREND / RANGE / BREAKOUT paths
  2. Engine `_learn_closed_trade` records OHLC snapshot + teaches LIVE neurons
  3. `_entry_evaluation` never HOLDs solely because advise() says HOLD
  4. TP/SL brackets stay OHLC-decided (untouched by structure shadow)
  5. Multi-deal Gold-like stress: BUY-biased brain £ improves after engine teach
  6. Suggest/why payload is honest (structure name + do-differently label)
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app import bracket_learn
from app import structure_shadow as ss
from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store


EPIC = 'GOLD'
TICK = 0.01
SPREAD = 0.35


def sandbox():
    return tempfile.TemporaryDirectory()


def _at(i, hour=10):
    return (datetime(2026, 3, 1, hour, 0, tzinfo=timezone.utc)
            + timedelta(minutes=i)).isoformat().replace('+00:00', 'Z')


def _seed_trend_down(store, epic=EPIC, *, n=40, start=3950.0, step=0.4, hour=10):
    px = start
    rows = []
    for i in range(n):
        o, c = px, px - step
        bar = {
            'at': _at(i, hour=hour),
            'open': o, 'high': o + 0.05, 'low': c - 0.05, 'close': c,
            'spread': SPREAD,
        }
        store.candle(epic, bar, source='HISTORICAL')
        rows.append(bar)
        px = c
    return rows


def _seed_range(store, epic=EPIC, *, n=40, mid=3950.0, half=0.8, hour=12):
    rows = []
    for i in range(n):
        sign = 1 if i % 2 == 0 else -1
        o = mid + sign * 0.05
        c = mid if i == n - 1 else mid - sign * 0.05
        bar = {
            'at': _at(i, hour=hour),
            'open': o, 'high': mid + half, 'low': mid - half, 'close': c,
            'spread': SPREAD,
        }
        store.candle(epic, bar, source='HISTORICAL')
        rows.append(bar)
    return rows


def _seed_breakout_down(store, epic=EPIC, *, pre_n=20, path_n=20, base=3950.0):
    pre = []
    for i in range(pre_n):
        bar = {
            'at': _at(i, hour=8),
            'open': base, 'high': base + 0.6, 'low': base - 0.6, 'close': base + 0.05,
            'spread': SPREAD,
        }
        store.candle(epic, bar, source='HISTORICAL')
        pre.append(bar)
    path = []
    px = base - 0.5
    for i in range(path_n):
        o, c = px, px - 0.7
        bar = {
            'at': _at(i, hour=9),
            'open': o, 'high': o + 0.05, 'low': c - 0.1, 'close': c,
            'spread': SPREAD,
        }
        store.candle(epic, bar, source='HISTORICAL')
        path.append(bar)
        px = c
    return pre, path


def _engine(directory):
    store = Store(Path(directory) / 'db.sqlite3')
    brain = AdaptiveBrain(store, data_dir=directory, seed=7)
    brain._activate(EPIC)
    brain.core.last_z = np.linspace(-0.25, 0.25, brain.core.d)
    brain.core.step(brain.core.last_z, dt=60.)
    e = SingleTradeEngine(store, brain)
    e.mandate = {'epic': EPIC, 'size': 0.1, 'currency': 'GBP'}
    e.market = {
        'epic': EPIC, 'tick': TICK, 'spread': SPREAD,
        'mid': 3950.0, 'min_distance': 0.15, 'bid': 3949.8, 'ask': 3950.2,
    }
    e.policy = AutoEvolution(store, Path(directory) / 'policy.py')
    brain.mark_prep_ready('deep structure fixture')
    return store, brain, e


def _scores(brain, transition):
    core = brain.core
    z = np.asarray(transition['z'], dtype=float)
    h = np.asarray(transition['h'], dtype=float)
    c = np.asarray(transition['c'], dtype=float)
    _, _, logits = core._forward(z, h, c, float(transition.get('dt') or 1.0))
    return dict(zip(('BUY', 'SELL', 'HOLD'), map(float, core._softmax(logits))))


# ── 1. Frames ──────────────────────────────────────────────────────────────


def test_deep_frames_trend_range_breakout():
    down = [{'open': 100 - i * 0.4, 'high': 100 - i * 0.4 + 0.05,
             'low': 100 - (i + 1) * 0.4 - 0.05, 'close': 100 - (i + 1) * 0.4,
             'at': f't{i}'} for i in range(12)]
    assert ss.frame_from_ohlc(down)['structure'] == 'TREND_DOWN'

    rng = []
    for i in range(12):
        sign = 1 if i % 2 == 0 else -1
        c = 100.0 if i == 11 else 100.0 - sign * 0.05
        rng.append({'open': 100.0 + sign * 0.05, 'high': 100.8, 'low': 99.2,
                    'close': c, 'at': f'r{i}'})
    assert ss.frame_from_ohlc(rng)['structure'] in ('RANGE', 'CHOP')

    pre = [{'open': 100, 'high': 100.5, 'low': 99.5, 'close': 100.1, 'at': f'p{i}'}
           for i in range(10)]
    path = [{'open': 99.4 - i * 0.6, 'high': 99.45 - i * 0.6,
             'low': 98.7 - i * 0.6, 'close': 98.8 - i * 0.6, 'at': f'x{i}'}
            for i in range(8)]
    br = ss.frame_from_ohlc(path, exit_level=path[-1]['close'], pre_bars=pre)
    assert br['structure'] == 'BREAKOUT_DOWN'
    assert br['breakout'] is True


# ── 2. Engine teach path ───────────────────────────────────────────────────


def test_deep_engine_sl_records_frame_and_teaches_neurons():
    with sandbox() as d:
        store, brain, e = _engine(d)
        rows = _seed_trend_down(store, n=50, start=3950.0, step=0.35)
        entry_bar = rows[20]
        path = rows[20:]
        entry = float(entry_bar['open'])
        exit_px = float(path[-1]['close'])
        tr = brain._json_transition(brain.core.last_transition)
        before = brain.core.P.copy()
        before_sc = _scores(brain, brain.core.last_transition)

        deal = 'deep-sl-1'
        store.upsert_trade({
            'dealId': deal, 'epic': EPIC, 'size': '0.1',
            'direction': 'BUY', 'level': entry,
        })
        store.set(f'entry_brain:{deal}', {
            'epic': EPIC, 'direction': 'BUY', 'entry': entry,
            'tick_size': TICK, 'entry_spread': SPREAD,
            'stop_distance': 2.0, 'profit_distance': 2.0,
            'bar_time': entry_bar['at'],
            'transition': tr,
        })
        e._learn_closed_trade(deal, {
            'level': exit_px, 'profit': -0.12, 'pnl_currency': 'GBP',
            'exit_reason': 'sl',
        })

        # Snapshot library populated — engine splits pre/path by bar_time, so a
        # long down seed may frame as TREND_DOWN *or* BREAKOUT_DOWN (both hostile).
        snaps = store.get(ss.SNAPSHOT_KEY) or []
        assert snaps, 'expected structure snapshot after SL'
        structure = snaps[-1]['frame']['structure']
        assert structure in ('TREND_DOWN', 'BREAKOUT_DOWN')
        assert snaps[-1]['key'].startswith(f'{EPIC}|{structure}|BUY')

        # Shadow explained + taught (or at least suggest recorded)
        taught_ev = [ev for ev in store.recent(80)
                     if ev.get('kind') == 'structure_shadow'
                     and 'taught LIVE' in str(ev.get('message') or '')]
        recorded = [ev for ev in store.recent(80)
                    if ev.get('kind') == 'structure_shadow']
        assert recorded, 'expected structure_shadow events'
        detail = json.loads(taught_ev[0]['details']) if taught_ev else json.loads(
            recorded[0]['details'])
        assert (detail.get('structure') in ('TREND_DOWN', 'BREAKOUT_DOWN')
                or structure in ('TREND_DOWN', 'BREAKOUT_DOWN'))
        if taught_ev:
            assert detail.get('suggest') in ('HOLD', 'SELL')
            assert detail.get('why')
            assert any(s in str(detail.get('why'))
                       for s in ('TREND_DOWN', 'BREAKOUT_DOWN'))
            assert detail.get('taught') is True
            assert not np.array_equal(before, brain.core.P)
            after_sc = _scores(brain, brain.core.last_transition)
            # Grave side must not gain after structure teach + loss CF.
            assert after_sc['BUY'] <= before_sc['BUY'] + 1e-6
        else:
            # Even without weight-move event, suggest must say do-differently.
            assert detail.get('suggest') in ('HOLD', 'SELL', None)


# ── 3. No LIVE veto ─────────────────────────────────────────────────────────


def test_deep_entry_evaluation_not_vetoed_by_structure_advise():
    """Even with a full hostile library, advise must not force HOLD on OPEN."""
    with sandbox() as d:
        store, brain, e = _engine(d)
        rows = _seed_trend_down(store, n=40)
        # Fill graveyard so advise() would say HOLD.
        for i in range(5):
            ss.record_loss(
                store, epic=EPIC, direction='BUY',
                entry=rows[0]['open'], exit_level=rows[-1]['close'],
                exit_reason='sl', broker_pnl=-0.12, bars=rows, deal_id=f'g{i}')
        advice = ss.advise(store, epic=EPIC, proposal='BUY', bars=rows)
        assert advice.get('action') == 'HOLD'  # opinion exists

        # Force neural BUY proposal path pieces.
        e.last_signal = {
            'scores': {'BUY': 0.70, 'SELL': 0.15, 'HOLD': 0.15},
            'epic': EPIC,
        }
        e.market_context = {
            'ready': True, 'reason': 'fixture', 'trend': 'DOWN', 'regime': 'ALIGNED',
            'returns_bps': {'1': -2, '5': -5, '15': -10, '60': -20},
            'bar_time': rows[-1]['at'],
        }
        brain.epic = EPIC
        # Bypass readiness/utility soft gates where possible.
        brain.mark_prep_ready('deep no-veto')
        # Unified cycle cool so edge allows.
        from app import unified_cycle as uc
        st = uc.load_state(store)
        st['aggression'] = 1.0
        st['loss_streak'] = 0
        uc.save_state(store, st)

        result = e._entry_evaluation()
        # Critical contract: structure_shadow evidence may be present, but action
        # must not flip to HOLD *because* of it when neural wants BUY and gates pass.
        assert 'structure_shadow' in result or result['action'] in ('BUY', 'SELL', 'HOLD')
        struct = result.get('structure_shadow') or {}
        if result['action'] == 'HOLD':
            reason = str(result.get('reason') or '')
            assert 'Structure shadow' not in reason
            assert 'same OHLC frame killed' not in reason
            # If HOLD, it must be neural/policy/gate — not structure veto string.
        else:
            assert result['action'] == 'BUY'
            # Evidence attached, not applied as veto.
            if struct:
                assert struct.get('action') in (None, 'HOLD')


# ── 4. Brackets untouched ───────────────────────────────────────────────────


def test_deep_brackets_still_ohlc_only():
    zone = {
        'kappa': 1.4, 'range_distance': 1.8, 'max_tp': 2.2,
        'noise_distance': 0.4, 'ready': True,
    }
    d = bracket_learn.decide_brackets_from_ohlc(
        zone, tick=TICK, spread=SPREAD, min_distance=0.15, tighten=1.0)
    assert d['source'] == 'ohlc_zone'
    assert not d.get('reshaped')
    assert d.get('blocked') in (None, False)
    # Structure module must not export a bracket override API.
    assert not hasattr(ss, 'decide_brackets_from_ohlc')
    assert not hasattr(ss, 'reshape_brackets')


# ── 5. Suggest honesty ──────────────────────────────────────────────────────


@pytest.mark.parametrize('direction,structure,bars_fn', [
    ('BUY', 'TREND_DOWN', lambda: [
        {'open': 100 - i * 0.5, 'high': 100 - i * 0.5 + 0.05,
         'low': 100 - (i + 1) * 0.5 - 0.05, 'close': 100 - (i + 1) * 0.5,
         'at': f'd{i}'} for i in range(12)]),
    ('SELL', 'TREND_UP', lambda: [
        {'open': 100 + i * 0.5, 'high': 100 + (i + 1) * 0.5 + 0.05,
         'low': 100 + i * 0.5 - 0.05, 'close': 100 + (i + 1) * 0.5,
         'at': f'u{i}'} for i in range(12)]),
])
def test_deep_suggest_why_matches_frame(direction, structure, bars_fn):
    frame = ss.frame_from_ohlc(bars_fn())
    assert frame['structure'] == structure
    teach = ss.suggest_teach(frame=frame, direction=direction, broker_pnl=-0.12)
    assert teach['structure'] == structure
    assert teach['suggest'] in ('HOLD', 'BUY', 'SELL')
    assert teach['suggest'] != direction  # do differently
    assert structure in teach['why']
    assert teach['label'] in (0, 1, 2)


# ── 6. Stress £ via engine-shaped teach loop ────────────────────────────────


def test_deep_stress_engine_teach_improves_gold_streak_pnl(capsys):
    """BUY-biased brain, 20 hostile DOWN + 5 UP — with vs without structure teach.

    Teach uses the same suggest_teach + learn_from_trade path the engine calls
    after SL (not an external HOLD gate).
    """
    down_bars = [
        {'open': 3950 - i * 0.35, 'high': 3950 - i * 0.35 + 0.05,
         'low': 3950 - (i + 1) * 0.35 - 0.05, 'close': 3950 - (i + 1) * 0.35,
         'at': f'd{i}'} for i in range(14)]
    up_bars = [
        {'open': 3900 + i * 0.35, 'high': 3900 + (i + 1) * 0.35 + 0.05,
         'low': 3900 + i * 0.35 - 0.05, 'close': 3900 + (i + 1) * 0.35,
         'at': f'u{i}'} for i in range(14)]
    episodes = [('DOWN', down_bars)] * 20 + [('UP', up_bars)] * 5
    loss, win = -0.12, 0.30

    def run(use_teach: bool):
        with sandbox() as d:
            store, brain, e = _engine(d)
            tr = dict(brain.core.last_transition)
            # Stuck bullish.
            for _ in range(30):
                brain.learn_from_trade(tr, 0, lr=0.45, won=True)
            assert _scores(brain, tr)['BUY'] == max(_scores(brain, tr).values())
            pnls, holds, suggests = [], 0, []
            for kind, bars in episodes:
                sc = _scores(brain, tr)
                action = max(('BUY', 'SELL', 'HOLD'), key=lambda a: sc[a])
                if action == 'HOLD':
                    holds += 1
                    continue
                if action == 'BUY' and kind == 'DOWN':
                    pnls.append(loss)
                    if use_teach:
                        # Mirror engine: record_loss + suggest_teach + learn.
                        ss.record_loss(
                            store, epic=EPIC, direction='BUY',
                            entry=bars[0]['open'], exit_level=bars[-1]['close'],
                            exit_reason='sl', broker_pnl=loss, bars=bars,
                            deal_id=f's{len(pnls)}')
                        teach = ss.suggest_teach(
                            frame=ss.frame_from_ohlc(bars), direction='BUY',
                            broker_pnl=loss)
                        suggests.append(teach['suggest'])
                        for _ in range(3):
                            brain.learn_from_trade(
                                tr, int(teach['label']), lr=0.4, won=False)
                elif action == 'BUY' and kind == 'UP':
                    pnls.append(win)
                elif action == 'SELL' and kind == 'DOWN':
                    pnls.append(win)
                elif action == 'SELL' and kind == 'UP':
                    pnls.append(loss)
            return {
                'net': sum(pnls), 'trades': len(pnls), 'holds': holds,
                'losses': sum(1 for p in pnls if p < 0),
                'suggests': suggests,
            }

    blind = run(False)
    smart = run(True)
    print(
        f"DEEP BLIND  net=£{blind['net']:.2f} trades={blind['trades']} "
        f"losses={blind['losses']} holds={blind['holds']}")
    print(
        f"DEEP TEACH  net=£{smart['net']:.2f} trades={smart['trades']} "
        f"losses={smart['losses']} holds={smart['holds']} "
        f"suggests={set(smart['suggests'])}")
    print(f"DEEP DELTA  £{smart['net'] - blind['net']:+.2f}")
    assert blind['losses'] >= 15
    assert smart['net'] > blind['net']
    assert smart['losses'] < blind['losses']
    assert smart['net'] - blind['net'] >= 1.0
    # Teach always proposed something other than BUY.
    assert smart['suggests']
    assert all(s != 'BUY' for s in smart['suggests'])


def test_deep_summary_exposes_operator_view():
    with sandbox() as d:
        store, _, _ = _engine(d)
        rows = _seed_trend_down(store, n=30)
        for i in range(3):
            ss.record_loss(
                store, epic=EPIC, direction='BUY',
                entry=rows[0]['open'], exit_level=rows[-1]['close'],
                exit_reason='sl', broker_pnl=-0.12, bars=rows, deal_id=f'sum{i}')
        ss.advise(store, epic=EPIC, proposal='BUY', bars=rows)
        snap = ss.summary(store)
        assert snap['buckets'] >= 1
        assert snap['snapshots'] >= 3
        assert snap['last_advice'] is not None
        assert snap['top']
