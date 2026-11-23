"""SHADOW rail lab: payload honesty + assets present."""
import tempfile
from pathlib import Path

from app.engine import Store
from app.runtime_hub import RuntimeHub
from app import strategy_gen


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'app/web/admin/index.html').read_text(encoding='utf-8')
JS = (ROOT / 'app/web/admin/admin.js').read_text(encoding='utf-8')
CSS = (ROOT / 'app/web/admin/admin.css').read_text(encoding='utf-8')


def test_shadow_rail_and_view_exist():
    assert 'data-view="shadow"' in HTML
    assert 'id="shadowBoard"' in HTML
    assert 'id="shadowTruth"' in HTML
    assert 'function renderShadowLab(' in JS
    assert 'shadow-board' in CSS
    assert 'admin.css?v=' in HTML
    assert 'admin.js?v=' in HTML
    assert 'auditWindow24' in JS
    assert 'shadowCmdLog' in JS
    assert 'mistake_audit' in JS
    assert 'SHADOW CMD LOG' in JS
    assert 'learning_log' in JS
    assert 'cmdlog-only' in JS
    assert 'shadow-cmd-meta' in JS
    assert 'shadow-cmd-meta' in CSS
    # SHADOW view is CMD-only — no duplicate side panels.
    assert 'SCENARIO MEMORY' not in JS
    assert 'TARGETED FIX' not in JS
    assert 'MISTAKE AUDIT' not in JS
    assert 'RECENT AUDITS' not in JS
    assert 'RECENT PAPER CLOSES' not in JS
    assert 'PAPER BOOK' not in JS
    assert 'SELF-DEV' not in JS
    assert 'PARAM TRIAL' not in JS
    assert 'POLICY SEARCH' not in JS
    assert 'HOLD SHADOW' not in JS
    assert 'fixState' in JS
    assert 'DIAGNOSING' in JS
    assert 'shadow-cmd' in CSS
    assert 'cmdlog-only' in CSS


def test_shadow_lab_payload_honest_about_100():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        strategy_gen.save(store, {'active_ids': ['GOLD|TREND_UP|BUY']})
        store.set('shadow_paper_journal_v1', [
            {'at': '2026-11-23T10:00:00Z', 'direction': 'BUY', 'entry': 3600,
             'exit': 3605, 'paper_pnl': 0.42, 'exit_reason': 'tp'},
            {'at': '2026-11-23T10:10:00Z', 'direction': 'SELL', 'entry': 3610,
             'exit': 3612, 'paper_pnl': -0.18, 'exit_reason': 'sl'},
        ])
        store.set('evolution_attempts', 3)
        store.set('evolution_last_reason', 'Waiting for labels')
        store.set('shadow_self_dev_last', {
            'decision': {'promote': False, 'reason': 'not better yet'},
            'shadow_neurons': 12, 'live_samples': 4, 'paper_cash_samples': 2,
            'method': 'independent_shadow_paper_cash',
        })
        store.set('shadow_mistake_audit_v1:last', {
            'mistakes': ['zone_fail'], 'scenario': 'TREND_UP',
            'source': 'shadow', 'pnl': -0.5, 'promote_count': 1, 'promote_need': 3,
            'news_status': 'CLEAR', 'intermarket_status': 'WAITING',
            'zone_side': 'demand', 'notes': ['price_closed_through_demand'],
        })
        hub = RuntimeHub.__new__(RuntimeHub)

        class _RT:
            pass

        rt = _RT()
        rt.store = store
        out = hub._desk_system_payload(rt, snap={'self_development': {'shadow_active': True}})
        lab = out['shadow_lab']
        assert lab['ok'] is True
        assert lab['truth']['hundred_variations'] is False
        assert lab['truth']['policy_candidates_approx'] == 30
        assert lab['truth'].get('mistake_audit') is True
        assert 'mistake-audit' in lab['truth']['what_runs'] or 'scenario memory' in lab['truth']['what_runs']
        assert len(lab['paper']['recent']) == 2
        assert lab['paper']['recent'][0]['paper_pnl'] in (0.42, -0.18)
        assert lab['evolution']['attempts'] == 3
        assert lab['self_dev_last']['paper_cash_samples'] == 2
        assert lab['mistake_audit']['ok'] is True
        assert lab['mistake_audit']['last']['mistakes'] == ['zone_fail']
        assert isinstance(lab['mistake_audit'].get('learning_log'), list)
