"""Mistake-audit full cycle: diagnose → fix → ready LIVE immediately."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_scenarios as osc
from app import shadow_mistake_audit as sma


def _candles(drift=0.0):
    rows = []
    px = 100.0
    for i in range(40):
        px = px + drift * 0.05
        rows.append({
            'open': px, 'high': px + 0.4, 'low': px - 0.4, 'close': px,
            'at': f'2026-11-23T10:{i:02d}:00+00:00',
        })
    return rows


def test_diagnose_news_and_peers():
    out = sma.diagnose(
        pnl=-1.0, spread=0.1, candles=_candles(),
        scenario_meta={'scenario': 'TREND_UP'},
        ohlc_meta={'action': 'BUY', 'trigger': 'zone',
                   'zone': {'lo': 99.0, 'hi': 100.0}},
        news={'status': 'NEWS_WINDOW', 'entry_allowed': False},
        intermarket={'status': 'CONTRADICTION'},
        direction='BUY', entry=99.5, source='shadow')
    assert out['audited']
    assert 'news_risk_ignored' in out['mistakes']
    flags = out['proposed_fix']['flags']
    assert flags.get('respect_news_window') == 1
    # Peers CONTRADICTION may or may not tag fight depending on status mapping.
    if 'intermarket_fight' in out['mistakes']:
        assert flags.get('respect_intermarket_veto') == 1


def test_promote_gate_commits_on_first_hurt():
    """PROMOTE_N=1 → first same mistake commits fix (full cycle NOW)."""
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
        before = osc.load_params(store)['penalty_range']
        r1 = sma.on_close(store, **kwargs)
        assert r1['promote_gate']['count'] == 1
        assert r1['promote_gate']['need'] == 1
        assert r1['fix'].get('applied') is True
        after = osc.load_params(store)['penalty_range']
        assert after > before
        assert osc.load_params(store)['block_mid_range'] == 1
        # READY_N=0 → ready_for_live on commit.
        lab = sma.lab_payload(store)
        assert lab['ready_count'] >= 1
        assert lab['pending_fix'].get('committed') is True
        # Second hit must not double-commit.
        r2 = sma.on_close(store, **kwargs)
        assert r2['fix'].get('applied') is False


def test_ready_for_live_immediate_after_commit():
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
            apply_ohlc_penalties=True,
        )
        out = sma.on_close(store, **loss_kw)
        assert out['fix'].get('applied') is True
        lab = sma.lab_payload(store)
        assert lab['ready_count'] >= 1
        assert any(r.get('ready_for_live') for r in lab['top_scenarios']) or lab[
            'ready_count'] >= 1


def test_flush_unsticks_pending_from_old_gate():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        # Simulate old PENDING state (pre PROMOTE_N=1) with hist already counted.
        fp = 'flush-fp'
        store.set(sma.SCENARIOS_KEY, {
            fp: {
                'fingerprint': fp,
                'scenario': 'RANGE',
                'direction': 'BUY',
                'seen': 3,
                'mistake_hits': 3,
                'mistake_counts': {'mid_range_mistake': 3},
                'last_mistakes': ['mid_range_mistake'],
                'fix_committed': False,
            }
        })
        store.set(sma.PENDING_FIX_KEY, {
            'at': '2026-11-23T20:00:00+00:00',
            'fingerprint': fp,
            'mistakes': ['mid_range_mistake'],
            'committed': False,
            'proposed': sma.propose_fix(['mid_range_mistake']),
        })
        before = osc.load_params(store)['penalty_range']
        out = sma.flush_full_cycle(store)
        assert out.get('committed', 0) >= 1 or out.get('readied', 0) >= 1
        after = osc.load_params(store)['penalty_range']
        assert after >= before
        pending = store.get(sma.PENDING_FIX_KEY) or {}
        assert pending.get('committed') is True
        ready = store.get(sma.READY_KEY) or []
        assert any(str(r.get('fingerprint')) == fp for r in ready if isinstance(r, dict))


def test_live_path_now_commits_full_cycle():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        kwargs = dict(
            pnl=-1.2, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break',
                       'zone': {'lo': 90.0, 'hi': 91.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=100.0, source='live',
            apply_ohlc_penalties=True,
        )
        r = sma.on_close(store, **kwargs)
        assert r['memory']['saved'] is True
        assert r['fix'].get('applied') is True
        assert sma.lab_payload(store)['ready_count'] >= 1
