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
    assert 'hundred_variations' in JS
    assert 'admin.css?v=127' in HTML
    assert 'admin.js?v=133' in HTML


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
        assert '100' in lab['truth']['what_runs'] or 'Not a 100' in lab['truth']['what_runs']
        assert len(lab['paper']['recent']) == 2
        assert lab['paper']['recent'][0]['paper_pnl'] in (0.42, -0.18)
        assert lab['evolution']['attempts'] == 3
        assert lab['self_dev_last']['paper_cash_samples'] == 2
