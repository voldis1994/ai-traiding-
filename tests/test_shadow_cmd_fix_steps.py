"""SHADOW CMD must show concrete knob paths and before→after values."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import ohlc_scenarios as osc
from app import self_params as sp
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


def test_journal_seed_waits_for_full_audit_no_fake_map():
    """Capital £ alone must NOT invent MAP → penalty_zone + KEEP spam."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        steps = sma.journal_seed_fix_steps({
            'pnl': -0.32, 'mistakes': [],
            'source': 'live_journal', 'notes': ['journal_24h_seed'],
        }, store=store)
        propose = next(s for s in steps if s['step'] == 'propose')
        assert propose['status'] == 'skip'
        assert not (propose.get('map_lines') or [])
        assert not (propose.get('mutations') or [])
        commit = next(s for s in steps if s['step'] == 'commit')
        assert commit['status'] == 'wait'
        assert 'on_close' in (commit.get('detail') or '')
        assert osc.load_params(store)['penalty_zone'] == 0.0

        log = sma.build_learning_log(
            recent_audits=[{
                'at': '2026-11-24T12:15:32+00:00',
                'closed_at': '2026-11-24T12:15:32+00:00',
                'pnl': -0.32, 'direction': 'SELL',
                'source': 'live_journal',
                'mistakes': [],
                'notes': ['journal_24h_seed', 'awaiting_full_audit'],
                'scenario': 'LIVE_JOURNAL',
                'deal_id': 'j1',
            }],
            pending_fix={},
            scenarios={},
            limit=4,
            store=store,
        )
        text = '\n'.join(str(x.get('text') or '') for x in log)
        assert 'full audit pending' in text.lower() or 'WAITING' in text or 'WAIT' in text
        assert 'ohlc_scenarios_v1.penalty_zone' not in text
        assert 'outcome_hurt' not in text


def test_outcome_hurt_teaches_full_system_not_zone_only():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        before = sp.load(store)
        proposed = sma.propose_fix(['outcome_hurt'])
        pack = sma.describe_fix_mutations(store, proposed, applied=False)
        knobs = {m['knob'] for m in pack['mutations']}
        assert 'structure_edge_nudge' in knobs
        assert 'situation_memory_nudge' in knobs
        assert 'forecast_bridge_nudge' in knobs
        assert 'mtf_momentum_nudge' in knobs
        assert 'penalty_zone' not in knobs
        # OHLC KEEP spam must not dominate a system-review proposal.
        untouched = {u['knob'] for u in pack['untouched']}
        assert 'penalty_trend' not in untouched
        assert 'penalty_zone' not in untouched

        out = sma.on_close(
            store,
            pnl=-0.55, spread=0.1, candles=_candles(drift=0.5),
            scenario_meta={'scenario': 'UNKNOWN'},
            ohlc_meta={'action': 'BUY', 'trigger': 'zone'},
            news={'status': 'CLEAR', 'entry_allowed': True},
            intermarket={'status': 'WAITING'},
            direction='BUY', entry=100.0, source='live',
            deal_id='sys1', apply_ohlc_penalties=True,
        )
        # Drifted path may still tag structure — either system review or richer class.
        if 'outcome_hurt' in (out.get('mistakes') or []):
            after = sp.load(store)
            assert after['structure_edge_nudge'] > before['structure_edge_nudge']
            assert osc.load_params(store)['penalty_zone'] == 0.0


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
        path0 = pending['mutations'][0]['path']
        assert (path0.startswith('ohlc_scenarios_v1.')
                or path0.startswith('self_params_v1.')
                or path0.startswith('shadow_mistake_audit'))
        assert pending.get('map_lines')


def test_cmd_open_snapshot_skips_capital_only_wait():
    """When OPEN snapshot fed audit, CMD must not tease Capital £ WAIT."""
    log = sma.build_learning_log(
        recent_audits=[{
            'at': '2026-11-24T13:45:12+00:00',
            'closed_at': '2026-11-24T13:45:12+00:00',
            'pnl': -0.40, 'direction': 'BUY',
            'source': 'live_backfill',
            'mistakes': ['zone_fail'],
            'notes': ['open_snapshot_used'],
            'open_snapshot': True,
            'scenario': 'RANGE',
            'deal_id': 'snap-cmd-1',
            'fix_steps': [],
        }],
        pending_fix={},
        scenarios={},
        limit=4,
    )
    text = '\n'.join(str(x.get('text') or '') for x in log)
    assert 'open snapshot' in text.lower() or 'zone_fail' in text
    assert 'Capital £ loss — full audit pending' not in text
    assert 'Capital £ only' not in text
