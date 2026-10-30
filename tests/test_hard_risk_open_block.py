"""Unset hard-risk after #42 silently blocked every AI OPEN (SL/TP still worked)."""
import tempfile
from pathlib import Path

import pytest

from app import hard_risk
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def test_preflight_blocks_when_limits_unset():
    with tempfile.TemporaryDirectory() as d:
        store,_=new_brain(d)
        pre=hard_risk.preflight(store)
        assert pre['allowed'] is False
        assert 'hard risk' in pre['reason'].lower() or 'Admin' in pre['reason']
        assert 'max_risk_per_trade' in (pre.get('missing_limits') or [])


def test_live_open_gates_surface_unset_hard_risk():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'pol.py')
        brain.trading_readiness=lambda: {'ready':True,'reason':'ok'}
        gates=e.live_open_gates('BUY')
        assert gates['allowed'] is False
        assert 'OPEN blocked' in gates['reason'] or 'hard risk' in gates['reason'].lower()
        hard_risk.save_operator(store,{
            'max_risk_per_trade':10,'daily_loss_limit':50,'equity_drawdown_limit':100},
            reason='test')
        gates2=e.live_open_gates('BUY')
        assert gates2['allowed'] is True


def test_snapshot_names_hard_risk_not_m1_wait():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=True;e.execution_enabled=True
        e.mandate={'epic':'GOLD','size':'0.02'}
        e.last_closed_step=10
        e.market_context={'ready':True,'reason':''}
        e.latest_fusion_ready=True
        e.last_decision_bar='2026-10-30T08:00:00+00:00'
        e.last_entry_processed_bar=e.last_decision_bar
        e.last_policy_decision='BUY'
        e.last_entry_reason='OPEN blocked: operator hard risk limits not configured'
        e.last_signal={'action':'BUY','scores':{'BUY':0.55,'SELL':0.3,'HOLD':0.15}}
        brain.trading_readiness=lambda: {'ready':True,'reason':'ok'}
        snap=e.snapshot()
        reason=snap.get('entry_block_reason') or ''
        assert 'Waiting for next completed M1 candle' not in reason
        assert 'hard risk' in reason.lower() or 'OPEN blocked' in reason or 'Admin' in reason
        assert snap.get('hard_risk',{}).get('configured') is False


def test_entry_evaluation_hold_when_hard_risk_unset():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'pol.py')
        e.mandate={'epic':'GOLD','size':'0.02'}
        e.market_context={'ready':True,'reason':'','returns_bps':{},'trend':'UP','regime':'ALIGNED'}
        e.latest_fusion_ready=True
        e.last_signal={'action':'BUY','scores':{'BUY':0.6,'SELL':0.25,'HOLD':0.15},
                       'confidence':0.6}
        brain.trading_readiness=lambda: {
            'ready':True,'reason':'ok','eval_samples':40,
            'minute_direction_accuracy':0.4,'holdout_net_bps_mean':0.1}
        brain.epic='GOLD'
        out=e._entry_evaluation()
        assert out['action']=='HOLD'
        assert 'OPEN blocked' in out['reason'] or 'hard risk' in out['reason'].lower()
