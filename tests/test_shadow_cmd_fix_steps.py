"""SHADOW CMD must show each FIX cycle step — what changed / what skipped."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_scenarios as osc
from app import shadow_mistake_audit as sma
from tests.test_shadow_mistake_audit import _candles


def test_on_close_stores_fix_steps_and_cmd_shows_them():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        out = sma.on_close(
            store,
            pnl=-0.32, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'RANGE'},
            ohlc_meta={'action': 'SELL', 'trigger': 'break',
                       'zone': {'lo': 90.0, 'hi': 91.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='SELL', entry=95.0, source='live',
            deal_id='step1', apply_ohlc_penalties=True,
        )
        steps = out.get('fix_steps') or []
        names = [s.get('step') for s in steps]
        assert names == [
            'diagnose', 'propose', 'memory', 'promote', 'commit', 'ready']
        assert any(s.get('step') == 'commit' and s.get('status') == 'ok'
                   for s in steps)
        lab = sma.lab_payload(store)
        text = '\n'.join(str(x.get('text') or '') for x in (lab.get('learning_log') or []))
        assert 'STEP' in text
        assert 'COMMIT' in text or 'COMMITTED' in text
        assert 'fix pending / diagnosing' not in text


def test_journal_seed_cmd_explains_skip_not_opaque_wait():
    steps = sma.journal_seed_fix_steps({
        'pnl': -0.32, 'mistakes': ['outcome_hurt'],
        'source': 'live_journal', 'notes': ['journal_24h_seed'],
    })
    by = {s['step']: s for s in steps}
    assert by['diagnose']['status'] == 'ok'
    assert by['propose']['status'] == 'ok'
    assert 'penalty_zone' in by['propose']['detail']
    assert by['memory']['status'] == 'skip'
    assert by['commit']['status'] == 'wait'
    assert by['ready']['status'] == 'skip'

    log = sma.build_learning_log(
        recent_audits=[{
            'at': '2026-11-24T12:15:32+00:00',
            'closed_at': '2026-11-24T12:15:32+00:00',
            'pnl': -0.32, 'direction': 'SELL',
            'source': 'live_journal',
            'mistakes': ['outcome_hurt'],
            'notes': ['journal_24h_seed'],
            'scenario': 'LIVE_JOURNAL',
            'deal_id': 'j1',
        }],
        pending_fix={},
        scenarios={},
        limit=4,
    )
    text = '\n'.join(str(x.get('text') or '') for x in log)
    assert 'CLOSE' in text and 'ERROR' in text
    assert 'STEP' in text
    assert 'journal' in text.lower() or 'SKIP' in text
    assert 'fix pending / diagnosing' not in text
    assert 'WAITING backfill' in text or 'WAIT' in text


def test_lab_pending_fix_includes_steps_from_last():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        sma.on_close(
            store,
            pnl=-1.0, spread=0.1, candles=_candles(),
            scenario_meta={'scenario': 'TREND_UP'},
            ohlc_meta={'action': 'BUY', 'trigger': 'zone',
                       'zone': {'lo': 98.0, 'hi': 99.0}},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=98.5, source='capital',
            deal_id='cap1', apply_ohlc_penalties=True,
        )
        lab = sma.lab_payload(store)
        pending = lab.get('pending_fix') or {}
        assert isinstance(pending.get('steps'), list)
        assert pending['steps']
        assert pending.get('committed') is True
