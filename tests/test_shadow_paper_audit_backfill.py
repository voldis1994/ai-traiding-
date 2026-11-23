"""Empty paper journal must restore closes from mistake-audit hist."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app.shadow_paper import ShadowPaperJournal
from app.runtime_hub import RuntimeHub
from app import strategy_gen


def test_backfill_restores_price_closes_from_audit_hist():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.set('shadow_mistake_audit_v1:hist', [
            {'at': '2026-11-23T14:36:00Z', 'source': 'shadow',
             'pnl': -12.94, 'unit': 'price', 'direction': 'BUY',
             'scenario': 'BREAKOUT_UP', 'mistakes': ['outcome_hurt']},
            {'at': '2026-11-23T14:22:00Z', 'source': 'shadow',
             'pnl': 10.0, 'unit': 'price', 'direction': 'BUY',
             'scenario': 'CHOCH_UP'},
            {'at': '2026-11-23T14:16:00Z', 'source': 'live',
             'pnl': 0.70, 'direction': 'BUY'},  # LIVE must not enter paper book
        ])
        j = ShadowPaperJournal(store)
        assert j.history == []
        n = j.backfill_from_mistake_audits()
        assert n == 2
        s = j.summary()
        assert s['display_trades'] == 2
        assert s['unit'] == 'price'
        assert s['display_net'] == pytest.approx(-2.94)
        assert s['wins'] == 1
        assert s['losses'] == 1
        assert s['audit_backfill'] is True
        # Idempotent — non-empty journal is left alone.
        assert j.backfill_from_mistake_audits() == 0


def test_desk_payload_backfills_empty_paper_from_audits():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        strategy_gen.save(store, {'active_ids': ['GOLD|TREND_UP|BUY']})
        store.set('shadow_mistake_audit_v1:hist', [
            {'at': '2026-11-23T14:36:00Z', 'source': 'shadow',
             'pnl': -12.94, 'unit': 'price', 'mistakes': ['outcome_hurt'],
             'scenario': 'BREAKOUT_UP'},
        ])
        store.set('shadow_mistake_audit_v1:last', {
            'mistakes': ['outcome_hurt'], 'pnl': -12.94, 'unit': 'price',
            'source': 'shadow', 'scenario': 'BREAKOUT_UP',
        })
        hub = RuntimeHub.__new__(RuntimeHub)

        class _RT:
            pass

        rt = _RT()
        rt.store = store
        out = hub._desk_system_payload(
            rt, snap={'self_development': {'shadow_active': True}})
        paper = out['shadow']
        assert paper['display_trades'] >= 1
        assert paper['display_net'] == pytest.approx(-12.94)
        assert out['shadow_lab']['paper']['display_trades'] >= 1
        assert len(out['shadow_lab']['paper']['recent']) >= 1
