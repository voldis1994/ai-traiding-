"""V3 R02: hard_risk.preflight / entry_allowed block OPEN when limits unset."""
import tempfile
from pathlib import Path

from app import hard_risk
from app.engine import Store
from tests.test_learning_integrity import new_brain


def _full_limits(**overrides):
    base = {
        'max_risk_per_trade': 10.0,
        'daily_loss_limit': 50.0,
        'equity_drawdown_limit': 100.0,
        'trading_halted': False,
    }
    base.update(overrides)
    return base


def test_preflight_blocks_when_limits_unset():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        pre = hard_risk.preflight(store)
        assert pre['allowed'] is False
        assert pre.get('open_gated') is True
        assert 'missing_limits' in (pre.get('reason') or '') or pre.get('missing_limits')


def test_preflight_allows_when_limits_configured():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        hard_risk.save_operator(store, _full_limits(), reason='op')
        pre = hard_risk.preflight(store)
        assert pre['allowed'] is True
        assert pre.get('missing_limits') == []


def test_snapshot_reports_open_gated_and_missing():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        snap = hard_risk.snapshot(store)
        assert snap.get('open_gated') is True
        assert snap.get('configured') is False
        assert snap.get('missing_limits')
        assert snap.get('preflight', {}).get('allowed') is False


def test_trading_halt_blocks_preflight():
    with tempfile.TemporaryDirectory() as d:
        store, _ = new_brain(d)
        hard_risk.save_operator(store, _full_limits(), reason='op')
        hard_risk.halt(store, 'manual stop')
        pre = hard_risk.preflight(store)
        assert pre['allowed'] is False
        assert 'halt' in (pre.get('reason') or '').lower()
