"""Max drawdown must include starting capital before the first trade."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app.model_validation import _equity_curve, score_trade_pnls
from app.self_params import DEFAULTS, save


def test_twelve_unit_losses_from_zero_start():
    """12 consecutive −1 losses → max_dd = −12 (peak at starting equity 0)."""
    pnls=[-1.0]*12
    eq,net,dd=_equity_curve(pnls)
    assert net==pytest.approx(-12.0)
    assert dd==pytest.approx(-12.0)
    assert len(eq)==12
    assert float(eq[-1])==pytest.approx(-12.0)


def test_first_loss_not_skipped_in_dd():
    """Without start point, peak would be −1 and first loss would show dd=0."""
    _,_,dd=_equity_curve([-1.0,-1.0])
    assert dd==pytest.approx(-2.0)


def test_score_trade_pnls_max_dd_includes_start():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        save(store,{**DEFAULTS,'model_min_oos':8},reason='test')
        scored=score_trade_pnls([-1.0]*12,store=store,model_version=3)
        assert scored['ok'] is True
        assert scored['max_dd']==pytest.approx(-12.0)
        assert scored['net']==pytest.approx(-12.0)
        assert scored['model_version']==3
