"""Audit #20–21: operator hard risk limits; neural cannot raise them."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app import hard_risk
from app.math_rules import cash_risk, tick_value_from_instrument


def _full_limits(**overrides):
    base={'max_risk_per_trade':10,'daily_loss_limit':50,
          'equity_drawdown_limit':100,'trading_halted':False}
    base.update(overrides)
    return base


def test_neural_cannot_raise_or_clear_limits():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,_full_limits(),reason='op')
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
        hard_risk.save_operator(store,_full_limits(max_risk_per_trade=1.0),reason='op')
        gate=hard_risk.entry_allowed(store,proposed_risk=2.5)
        assert gate['allowed'] is False
        assert 'exceeds' in gate['reason']


def test_daily_loss_halts_trading():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,_full_limits(daily_loss_limit=5.0),reason='op')
        hard_risk.record_realized_pnl(store,-3.0)
        hard_risk.record_realized_pnl(store,-2.5)
        limits=hard_risk.load(store)
        assert limits['trading_halted'] is True
        gate=hard_risk.entry_allowed(store,proposed_risk=0.1)
        assert gate['allowed'] is False


def test_equity_drawdown_halt():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.save_operator(store,_full_limits(equity_drawdown_limit=20.0),reason='op')
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
        hard_risk.save_operator(store,_full_limits(max_risk_per_trade=5.0),reason='op')
        gate=hard_risk.entry_allowed(store,proposed_risk=None)
        assert gate['allowed'] is False
        assert 'could not be computed' in gate['reason']


def test_unset_limits_block_without_invented_floors():
    """Critical #2 revised: no 5/25/100 surrogates — block until operator configures."""
    assert hard_risk.DEFAULTS['max_risk_per_trade'] is None
    assert hard_risk.DEFAULTS['daily_loss_limit'] is None
    assert hard_risk.DEFAULTS['equity_drawdown_limit'] is None
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        limits=hard_risk.load(store)
        assert limits['max_risk_per_trade'] is None
        gate=hard_risk.entry_allowed(store,proposed_risk=0.5)
        assert gate['allowed'] is False
        assert 'not configured' in gate['reason']
        # Partial operator save still blocks (all three required).
        hard_risk.save_operator(store,{'max_risk_per_trade':10.0},reason='partial')
        gate2=hard_risk.entry_allowed(store,proposed_risk=0.5)
        assert gate2['allowed'] is False
        assert 'daily_loss_limit' in (gate2.get('missing_limits') or [])
        # Full operator config uses *those* numbers — not 5/25/100.
        hard_risk.save_operator(store,_full_limits(
            max_risk_per_trade=12.5,daily_loss_limit=40,equity_drawdown_limit=200),
            reason='op')
        loaded=hard_risk.load(store)
        assert loaded['max_risk_per_trade']==pytest.approx(12.5)
        assert loaded['daily_loss_limit']==pytest.approx(40)
        assert loaded['equity_drawdown_limit']==pytest.approx(200)
        assert hard_risk.entry_allowed(store,proposed_risk=1.0)['allowed'] is True
        assert hard_risk.entry_allowed(store,proposed_risk=20.0)['allowed'] is False


def test_neural_cannot_invent_limits_when_unset():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        before=hard_risk.load(store)
        safe=hard_risk.neural_may_not_raise(before,{
            'max_risk_per_trade':5,'daily_loss_limit':25,'equity_drawdown_limit':100})
        assert safe['max_risk_per_trade'] is None
        assert safe['daily_loss_limit'] is None
        assert safe['equity_drawdown_limit'] is None
