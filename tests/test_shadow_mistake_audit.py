"""Mistake-audit + scenario memory + promote gate."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import shadow_mistake_audit as sma
from app import ohlc_scenarios as osc


def _candles(n=40, *, start=100.0, drift=-0.4):
    rows = []
    px = start
    for i in range(n):
        o = px
        c = px + drift
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        rows.append({
            'at': f'2026-11-23T10:{i:02d}:00Z',
            'open': o, 'high': h, 'low': l, 'close': c,
        })
        px = c
    return rows


def test_diagnose_zone_fail_and_propose_targeted_not_pnl():
    candles = _candles()
    zone = {'lo': 98.0, 'hi': 99.0}
    out = sma.diagnose(
        pnl=-1.5, spread=0.1, candles=candles,
        scenario_meta={'scenario': 'TREND_UP'},
        ohlc_meta={'action': 'BUY', 'zone': zone, 'trigger': 'touch'},
        news={'status': 'CLEAR', 'entry_allowed': True},
        intermarket={'status': 'WAITING'},
        direction='BUY', entry=98.5,
    )
    assert out['audited'] is True
    assert 'zone_fail' in out['mistakes']
    assert out['proposed_fix'] is not None
    assert 'penalty_zone' in out['proposed_fix']['deltas']
    assert out['proposed_fix']['why'] == 'targeted_to_mistake_class_not_pnl'


def test_news_and_intermarket_mistakes():
    out = sma.diagnose(
        pnl=-2.0, spread=0.05, candles=_candles(),
        scenario_meta={'scenario': 'RANGE'},
        ohlc_meta={'action': 'BUY', 'trigger': 'break'},
        news={'status': 'NEWS_WINDOW', 'entry_allowed': False},
        intermarket={
            'status': 'READY',
            'peers': [
                {'ready': True, 'stable': True, 'direction': -1},
                {'ready': True, 'stable': True, 'direction': -1},
            ],
        },
        direction='BUY', entry=100.0,
    )
    assert 'news_risk_ignored' in out['mistakes']
    assert 'intermarket_fight' in out['mistakes']
    flags = out['proposed_fix']['flags']
    assert flags.get('respect_news_window') == 1
    assert flags.get('respect_intermarket_veto') == 1


def test_promote_gate_commits_only_after_repeats():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        kwargs = dict(
            pnl=-1.2, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break',
                       'zone': {'lo': 90.0, 'hi': 91.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=95.0, source='shadow',
            apply_ohlc_penalties=True,
        )
        r1 = sma.on_close(store, **kwargs)
        assert r1['fix'].get('applied') is False
        assert r1['fix'].get('pending') is True
        assert r1['promote_gate']['count'] == 1
        r2 = sma.on_close(store, **kwargs)
        assert r2['fix'].get('applied') is False
        assert r2['promote_gate']['count'] == 2
        before = osc.load_params(store)['penalty_range']
        r3 = sma.on_close(store, **kwargs)
        assert r3['promote_gate']['ready'] is True or r3['fix'].get('applied') is True
        assert r3['fix'].get('applied') is True
        after = osc.load_params(store)['penalty_range']
        assert after > before
        assert osc.load_params(store)['block_mid_range'] == 1
        # Fourth hit must not double-commit.
        r4 = sma.on_close(store, **kwargs)
        assert r4['fix'].get('applied') is False
        lab = sma.lab_payload(store)
        assert lab['ok'] is True
        assert lab['scenarios'] >= 1
        assert lab['last']['mistakes']
        assert 'mid_range_mistake' in lab['last']['mistakes'] or lab['pending_fix']


def test_ready_for_live_after_proof_wins():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        loss_kw = dict(
            pnl=-1.0, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RETEST_UP'},
            ohlc_meta={'action': 'BUY', 'trigger': 'retest',
                       'zone': {'lo': 98.0, 'hi': 99.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=98.5, source='shadow',
        )
        for _ in range(sma.PROMOTE_N):
            sma.on_close(store, **loss_kw)
        # Wins after fix → ready
        win_kw = dict(loss_kw)
        win_kw['pnl'] = 1.5
        # Use candles that do not trip zone_fail on win path
        win_kw['candles'] = _candles(drift=0.3)
        a = sma.on_close(store, **win_kw)
        b = sma.on_close(store, **win_kw)
        lab = sma.lab_payload(store)
        assert lab['ready_count'] >= 1 or (b.get('ready_for_live') or a.get('ready_for_live'))
        assert any(r.get('ready_for_live') for r in lab['top_scenarios']) or lab['ready_count'] >= 1


def test_live_path_skips_double_penalty_commit():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        kwargs = dict(
            pnl=-1.2, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break'},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=100.0, source='live',
            apply_ohlc_penalties=False,
        )
        for _ in range(sma.PROMOTE_N):
            r = sma.on_close(store, **kwargs)
        assert r['fix'].get('applied') is False
        assert r['memory']['saved'] is True
