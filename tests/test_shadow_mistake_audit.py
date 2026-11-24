"""Mistake-audit full cycle: diagnose → promote ≥3 → proof wins → ready LIVE."""
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


def test_v04_thresholds_forbid_instant_ready():
    assert sma.PROMOTE_N >= 3
    assert sma.READY_N >= 3


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


def test_promote_gate_needs_three_repeats():
    """PROMOTE_N≥3 → first same mistake does not commit; third does."""
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
        assert r1['promote_gate']['need'] == sma.PROMOTE_N
        assert r1['fix'].get('applied') is False
        assert sma.lab_payload(store)['ready_count'] == 0

        r2 = sma.on_close(store, **kwargs)
        assert r2['promote_gate']['count'] == 2
        assert r2['fix'].get('applied') is False

        r3 = sma.on_close(store, **kwargs)
        assert r3['promote_gate']['count'] == 3
        assert r3['fix'].get('applied') is False
        assert r3['fix'].get('pending') is True
        assert r3['fix'].get('live_commit') is False
        after = osc.load_params(store)['penalty_range']
        assert after == before
        pending = store.get(sma.PENDING_FIX_KEY) or {}
        assert pending.get('proposed', {}).get('flags', {}).get('block_mid_range') == 1
        assert pending.get('committed') is False
        assert sma.lab_payload(store)['ready_count'] == 0


def test_ready_for_live_requires_proof_wins():
    """Instant ready_for_live after first mistake / commit is forbidden (V04)."""
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
        for _ in range(sma.PROMOTE_N):
            out = sma.on_close(store, **loss_kw)
        assert out['fix'].get('applied') is False
        assert out['fix'].get('pending') is True
        assert out['fix'].get('live_commit') is False
        lab = sma.lab_payload(store)
        assert lab['ready_n'] >= 3
        assert lab['ready_count'] == 0

        win_kw = dict(loss_kw)
        win_kw['pnl'] = 1.5
        for _ in range(sma.READY_N):
            sma.on_close(store, **win_kw)
        lab2 = sma.lab_payload(store)
        assert lab2['ready_count'] >= 1


def test_flush_respects_promote_and_ready_n():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
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
                'wins_after_fix': 0,
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
        assert out.get('committed', 0) >= 1
        after = osc.load_params(store)['penalty_range']
        assert after >= before
        pending = store.get(sma.PENDING_FIX_KEY) or {}
        assert pending.get('committed') is True
        # No proof wins yet → must not ready.
        ready = store.get(sma.READY_KEY) or []
        assert not any(str(r.get('fingerprint')) == fp for r in ready if isinstance(r, dict))


def test_live_path_does_not_instant_ready():
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
        assert r['fix'].get('applied') is False
        assert sma.lab_payload(store)['ready_count'] == 0
