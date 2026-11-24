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
        lab = sma.lab_payload(store)
        log = lab.get('learning_log') or []
        text = '\n'.join(str(x.get('text') or '') for x in log)
        assert 'CLOSE' in text and 'ERROR' in text
        assert 'FIX' in text and 'COMMITTED' in text
        assert 'OUTCOME' in text


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


def test_loss_commit_does_not_lie_about_ready():
    """First loss commits fix but CMD must WAIT for proof win — not READY DONE."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        out = sma.on_close(
            store,
            pnl=-1.0, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break',
                       'zone': {'lo': 90.0, 'hi': 91.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=95.0, source='live',
            deal_id='loss-ready', apply_ohlc_penalties=True,
        )
        assert out['fix'].get('applied') is True
        ready_steps = [s for s in (out.get('fix_steps') or [])
                       if s.get('step') == 'ready']
        assert ready_steps
        assert ready_steps[0]['status'] == 'wait'
        assert 'stamped' not in (ready_steps[0].get('detail') or '')
        last = store.get(sma.LAST_KEY) or {}
        assert last.get('ready_for_live') is False
        assert last.get('ready_stamped_this_close') is False
        lab = sma.lab_payload(store)
        assert lab.get('ready_count') == 0
        text = '\n'.join(str(x.get('text') or '')
                         for x in (lab.get('learning_log') or []))
        assert 'READY  fingerprint marked ready_for_live' not in text


def test_mid_range_from_live_when_stamp_missing():
    """CMD RANGE from live classify must still teach range knobs."""
    # Flat path → live classify prefers RANGE; stamped empty.
    out = sma.diagnose(
        pnl=-0.5, spread=0.1, candles=_candles(),
        scenario_meta={},
        ohlc_meta={'action': 'BUY', 'trigger': 'break',
                   'zone': {'lo': 90.0, 'hi': 91.0}},
        news={'status': 'CLEAR', 'entry_allowed': True},
        intermarket={'status': 'WAITING'},
        direction='BUY', entry=95.0, source='live_backfill')
    assert out['audited']
    live = out['context'].get('live_scenario') or ''
    stamped = out['context'].get('stamped_scenario') or ''
    assert (live in ('RANGE', 'COMPRESSION')
            or stamped in ('RANGE', 'COMPRESSION'))
    assert 'mid_range_mistake' in out['mistakes']
    assert 'outcome_hurt' not in out['mistakes']
    deltas = (out.get('proposed_fix') or {}).get('deltas') or {}
    assert float(deltas.get('penalty_range') or 0) > 0


def test_fakeout_stamp_is_not_outcome_hurt():
    """Stamped FAKEOUT loss → fakeout_missed (OHLC), never catch-all quartet."""
    out = sma.diagnose(
        pnl=-0.12, spread=0.1, candles=_candles(),
        scenario_meta={'scenario': 'FAKEOUT'},
        ohlc_meta={'action': 'SELL', 'trigger': 'break'},
        news={'status': 'CLEAR', 'entry_allowed': True},
        intermarket={'status': 'WAITING'},
        direction='SELL', entry=99.5, source='live')
    assert 'fakeout_missed' in out['mistakes']
    assert 'outcome_hurt' not in out['mistakes']
    assert 'fakeout_stamp_loss' in (out.get('notes') or [])
    deltas = (out.get('proposed_fix') or {}).get('deltas') or {}
    assert float(deltas.get('penalty_breakout') or 0) > 0
    assert 'structure_edge_nudge' not in deltas


def test_new_fix_commit_resets_stale_wins_after():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        # Seed bag with orphan wins_after_fix so recommit cannot fake READY.
        diag = sma.diagnose(
            pnl=-1.0, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break'},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=95.0, source='live')
        fp = diag['context']['fingerprint']
        store.set(sma.SCENARIOS_KEY, {
            fp: {
                'fingerprint': fp, 'seen': 2, 'fix_committed': False,
                'wins_after_fix': 3, 'ready_for_live': True,
                'mistake_counts': {'mid_range_mistake': 1},
            }
        })
        store.set(sma.READY_KEY, [{'fingerprint': fp, 'ready_at': 'old'}])
        out = sma.on_close(
            store,
            pnl=-1.0, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'BUY', 'trigger': 'break'},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=95.0, source='live',
            deal_id='reset-waf', apply_ohlc_penalties=True,
        )
        assert out['fix'].get('applied') is True
        bag = store.get(sma.SCENARIOS_KEY) or {}
        row = bag.get(fp) or {}
        assert int(row.get('wins_after_fix') or 0) == 0
        assert row.get('ready_for_live') is False
        ready = store.get(sma.READY_KEY) or []
        assert not any(r.get('fingerprint') == fp for r in ready
                       if isinstance(r, dict))


def test_journal_merge_dedupes_by_deal_id():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.set(sma.HIST_KEY, [{
            'at': '2026-11-24T13:00:00+00:00',
            'audited_at': '2026-11-24T13:00:00+00:00',
            'closed_at': '2026-11-24T12:00:00+00:00',
            'pnl': -0.15, 'direction': 'BUY',
            'source': 'live_backfill',
            'mistakes': ['mid_range_mistake'],
            'deal_id': 'same-deal',
            'scenario': 'RANGE',
            'fix_steps': [],
        }])
        # Journal row same deal, different clock → must not duplicate.
        journal = {
            'n': 1, 'wins': 0, 'losses': 1, 'scratch': 0, 'net': -0.15,
            'recent': [{
                'at': '2026-11-24T12:00:00+00:00',
                'source': 'live_journal', 'pnl': -0.15,
                'direction': 'BUY', 'deal_id': 'same-deal',
                'mistakes': ['outcome_hurt'],
                'notes': ['journal_24h_seed'],
                'scenario': 'LIVE_JOURNAL',
            }],
        }
        original = sma._journal_window_stats

        def _fake(store_arg, hours=24):
            return journal

        sma._journal_window_stats = _fake
        try:
            lab = sma.lab_payload(store)
        finally:
            sma._journal_window_stats = original
        recent = lab.get('recent_audits') or []
        same = [r for r in recent
                if str(r.get('deal_id') or '') == 'same-deal']
        assert len(same) == 1
        assert same[0].get('source') == 'live_backfill'
