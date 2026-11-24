"""Mistake-audit: loss → diagnose → commit fix → re-test same situation."""
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


def test_promote_on_first_loss():
    assert sma.PROMOTE_N == 1
    assert sma.READY_N == 1


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
    if 'intermarket_fight' in out['mistakes']:
        assert flags.get('respect_intermarket_veto') == 1


def test_first_loss_commits_targeted_fix():
    """Same fingerprint+mistake on first close commits knobs for re-test."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        kwargs = dict(
            pnl=-1.2, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break',
                       'zone': {'lo': 90.0, 'hi': 91.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=95.0, source='live',
            apply_ohlc_penalties=True,
        )
        before = osc.load_params(store)['penalty_range']
        r1 = sma.on_close(store, **kwargs)
        assert r1['promote_gate']['count'] == 1
        assert r1['promote_gate']['need'] == 1
        assert r1['fix'].get('applied') is True
        assert r1['fix'].get('live_commit') is True
        after = osc.load_params(store)['penalty_range']
        assert after != before or r1['fix'].get('changed')
        pending = store.get(sma.PENDING_FIX_KEY) or {}
        assert pending.get('committed') is True


def test_ready_for_live_after_one_proof_win():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        loss_kw = dict(
            pnl=-1.0, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RETEST_UP'},
            ohlc_meta={'action': 'BUY', 'trigger': 'retest',
                       'zone': {'lo': 98.0, 'hi': 99.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=98.5, source='live',
            apply_ohlc_penalties=True,
        )
        out = sma.on_close(store, **loss_kw)
        assert out['fix'].get('applied') is True
        assert out['fix'].get('live_commit') is True

        win_kw = dict(loss_kw)
        win_kw['pnl'] = 1.5
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
                'seen': 1,
                'mistake_counts': {'zone_entry': 1},
                'fix_committed': False,
            }
        })
        gate = sma.promote_gate(store, fp, ['zone_entry'])
        assert gate['ready'] is True
        assert gate['need'] == 1
