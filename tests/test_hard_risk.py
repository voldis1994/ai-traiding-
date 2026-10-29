"""Audit #20–21: operator hard risk limits; neural cannot raise them."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app import hard_risk
from app.math_rules import cash_risk, tick_value_from_instrument


def test_neural_cannot_raise_or_clear_limits():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,{
            'max_risk_per_trade':10,'daily_loss_limit':50,
            'equity_drawdown_limit':100,'trading_halted':False},reason='op')
        before=hard_risk.load(store)
        safe=hard_risk.neural_may_not_raise(before,{
            'max_risk_per_trade':100,'daily_loss_limit':None,
            'equity_drawdown_limit':50,'trading_halted':False})
        assert safe['max_risk_per_trade']==pytest.approx(10)
        assert safe['daily_loss_limit']==pytest.approx(50)
        assert safe['equity_drawdown_limit']==pytest.approx(50)  # tighter OK


def test_entry_blocked_when_fixed_size_risk_exceeds_max():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,{'max_risk_per_trade':1.0},reason='op')
        gate=hard_risk.entry_allowed(store,proposed_risk=2.5)
        assert gate['allowed'] is False
        assert 'exceeds' in gate['reason']


def test_daily_loss_halts_trading():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,{'daily_loss_limit':5.0},reason='op')
        hard_risk.record_realized_pnl(store,-3.0)
        hard_risk.record_realized_pnl(store,-2.5)
        limits=hard_risk.load(store)
        assert limits['trading_halted'] is True
        gate=hard_risk.entry_allowed(store,proposed_risk=0.1)
        assert gate['allowed'] is False


def test_equity_drawdown_halt():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,{'equity_drawdown_limit':20.0},reason='op')
        hard_risk.update_peak_equity(store,1000.0)
        hard_risk.check_drawdown_halt(store,970.0)  # dd=30
        assert hard_risk.load(store)['trading_halted'] is True


def test_neural_cannot_clear_halt():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.halt(store,'test')
        before=hard_risk.load(store)
        safe=hard_risk.neural_may_not_raise(before,{'trading_halted':False})
        assert safe['trading_halted'] is True
        hard_risk.clear_halt(store,operator=True)
        assert hard_risk.load(store)['trading_halted'] is False


def test_cash_risk_size_times_sl_ticks_times_tick_value():
    assert cash_risk(2,20,0.5)==pytest.approx(20.0)
    tv=tick_value_from_instrument({'valueOfOnePip':1.0,'onePipMeans':0.01},0.01)
    assert float(tv)==pytest.approx(1.0)
    # Scale when order tick differs from pip means.
    tv2=tick_value_from_instrument({'valueOfOnePip':1.0,'onePipMeans':0.01},0.001)
    assert float(tv2)==pytest.approx(0.1)


def test_missing_tick_value_blocks_when_max_risk_set():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,{'max_risk_per_trade':5.0},reason='op')
        gate=hard_risk.entry_allowed(store,proposed_risk=None)
        assert gate['allowed'] is False
        assert 'could not be computed' in gate['reason']


def test_defaults_never_none_and_always_enforce_max_risk():
    assert hard_risk.DEFAULTS['max_risk_per_trade'] is not None
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        # Fresh store → mandatory defaults; small proposed risk allowed.
        gate=hard_risk.entry_allowed(store,proposed_risk=0.5)
        assert gate['allowed'] is True
        gate2=hard_risk.entry_allowed(store,proposed_risk=999.0)
        assert gate2['allowed'] is False
