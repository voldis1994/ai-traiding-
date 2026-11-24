"""SHADOW CMD must show concrete knob paths and before→after values."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_scenarios as osc
from app import shadow_mistake_audit as sma
from tests.test_shadow_mistake_audit import _candles


def test_on_close_writes_concrete_before_after_path():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        before = osc.load_params(store)['penalty_zone']
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
        assert out['fix'].get('applied') is True
        muts = out['fix'].get('mutations') or []
        assert muts
        zone = next(m for m in muts if m.get('knob') == 'penalty_zone')
        assert zone['path'] == 'ohlc_scenarios_v1.penalty_zone'
        assert zone['before'] == before
        assert zone['after'] > before
        assert '[WRITTEN]' in zone['line']
        after = osc.load_params(store)['penalty_zone']
        assert after == zone['after']

        lab = sma.lab_payload(store)
        text = '\n'.join(str(x.get('text') or '') for x in (lab.get('learning_log') or []))
        assert 'ohlc_scenarios_v1.penalty_zone' in text
        assert '→' in text
        assert 'MAP' in text
        assert 'WRITE' in text or 'VALUE' in text
        assert 'KEEP' in text or 'UNTOUCHED' in text
        assert 'fix pending / diagnosing' not in text


def test_journal_seed_shows_proposed_path_not_written():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        steps = sma.journal_seed_fix_steps({
            'pnl': -0.32, 'mistakes': ['outcome_hurt'],
            'source': 'live_journal', 'notes': ['journal_24h_seed'],
        }, store=store)
        propose = next(s for s in steps if s['step'] == 'propose')
        assert propose['status'] == 'ok'
        assert any('ohlc_scenarios_v1.penalty_zone' in str(x)
                   for x in (propose.get('map_lines') or propose.get('changed') or []))
        muts = propose.get('mutations') or []
        assert muts
        assert muts[0]['path'] == 'ohlc_scenarios_v1.penalty_zone'
        assert '[PROPOSED]' in muts[0]['line']
        # Not written yet — params stay at default.
        assert osc.load_params(store)['penalty_zone'] == 0.0

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
            store=store,
        )
        text = '\n'.join(str(x.get('text') or '') for x in log)
        assert 'MAP' in text
        assert 'ohlc_scenarios_v1.penalty_zone' in text
        assert '0 →' in text or '0→' in text or '→' in text
        assert 'PROPOSED' in text or 'WAITING' in text


def test_describe_mutations_lists_untouched_peers():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        proposed = sma.propose_fix(['outcome_hurt'])
        pack = sma.describe_fix_mutations(store, proposed, applied=False)
        knobs = {m['knob'] for m in pack['mutations']}
        assert knobs == {'penalty_zone'}
        untouched = {u['knob'] for u in pack['untouched']}
        assert 'penalty_trend' in untouched
        assert 'penalty_range' in untouched
        assert 'penalty_zone' not in untouched


def test_lab_pending_exposes_mutation_lines():
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
        assert pending.get('committed') is True
        assert pending.get('mutations')
        assert pending['mutations'][0]['path'].startswith('ohlc_scenarios_v1.')
        assert pending.get('map_lines')
