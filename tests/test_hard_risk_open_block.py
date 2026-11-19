"""Hard-risk must NOT gate LIVE OPEN (operator: remove risk entirely)."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import hard_risk, ohlc_entry_zones
from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def _seed_gold_candles(store, n=80):
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    px = 2000.0
    rows = []
    for i in range(n):
        o = px
        c = px + (0.15 if i % 7 else -0.35)
        h = max(o, c) + 0.8 + (1.5 if i % 11 == 10 else 0.1)
        l = min(o, c) - 0.8 - (1.5 if i % 11 == 5 else 0.1)
        row = {
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c,
        }
        store.candle('GOLD', row, source='HISTORICAL')
        rows.append(row)
        px = c
    return rows


def test_preflight_allows_when_limits_unset():
    with tempfile.TemporaryDirectory() as d:
        store,_=new_brain(d)
        pre=hard_risk.preflight(store)
        assert pre['allowed'] is True
        assert pre.get('open_gated') is False


def test_live_open_gates_ignore_unset_hard_risk():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'pol.py')
        brain.trading_readiness=lambda: {'ready':True,'reason':'ok'}
        gates=e.live_open_gates('BUY')
        assert gates['allowed'] is True


def test_snapshot_does_not_blame_hard_risk_when_unset():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        e=SingleTradeEngine(store,brain)
        e.armed=True;e.execution_enabled=True
        e.mandate={'epic':'GOLD','size':'0.02'}
        e.last_closed_step=10
        e.market_context={'ready':True,'reason':''}
        e.latest_fusion_ready=True
        e.last_decision_bar='2026-10-30T08:00:00+00:00'
        e.last_entry_processed_bar=None
        e.last_signal={'action':'BUY','scores':{'BUY':0.55,'SELL':0.3,'HOLD':0.15}}
        brain.trading_readiness=lambda: {'ready':True,'reason':'ok'}
        snap=e.snapshot()
        reason=(snap.get('entry_block_reason') or '').lower()
        assert 'hard risk' not in reason
        assert 'open blocked' not in reason
        assert snap.get('hard_risk',{}).get('open_gated') is False


def test_entry_evaluation_not_held_by_unset_hard_risk():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        rows=_seed_gold_candles(store)
        zones=ohlc_entry_zones.compute_entry_zones(rows, tick=0.1, spread=0.2)
        buy=zones['buy_entry']
        assert buy
        ask=(float(buy['lo'])+float(buy['hi']))/2
        e=SingleTradeEngine(store,brain)
        e.policy=AutoEvolution(store,Path(d)/'pol.py')
        e.mandate={'epic':'GOLD','size':'0.02'}
        e.market={'mid':ask,'ask':ask,'bid':ask-0.1,'tick':0.1,'spread':0.2}
        e.market_context={'ready':True,'reason':'','returns_bps':{},'trend':'UP','regime':'ALIGNED'}
        e.latest_fusion_ready=True
        e.last_signal={'action':'BUY','scores':{'BUY':0.6,'SELL':0.25,'HOLD':0.15},
                       'confidence':0.6}
        brain.trading_readiness=lambda: {
            'ready':True,'reason':'ok','eval_samples':40,
            'minute_direction_accuracy':0.4,'holdout_net_bps_mean':0.1}
        brain.epic='GOLD'
        e._news_risk=lambda when=None: {
            'enabled':True,'entry_allowed':True,'status':'CLEAR','reason':'test'}
        out=e._entry_evaluation()
        assert out['action']=='BUY'
        assert 'hard risk' not in (out.get('reason') or '').lower()
        assert (out.get('ohlc_entry_zone') or {}).get('allowed') is True
