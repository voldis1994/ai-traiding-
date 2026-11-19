"""Regression for audit logic fixes (#1–#12)."""
import tempfile
from pathlib import Path

import pytest

from app import bracket_learn, day_book, hard_risk, shared_market
from app.engine import AdaptiveBrain, SingleTradeEngine, Store


def test_day_book_refine_close_delta():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        day_book.record_close(store,pnl=-0.19,deal_id='d1',equity=10_000)
        daily=day_book.load_daily(store,equity=10_000)
        assert daily['realized_pnl']==pytest.approx(-0.19)
        assert daily['closes']==1
        day_book.refine_close(store,deal_id='d1',old_pnl=-0.19,new_pnl=-0.21,
                              equity=10_000)
        daily=day_book.load_daily(store,equity=10_000)
        assert daily['realized_pnl']==pytest.approx(-0.21)
        assert daily['closes']==1  # no double close


def test_hard_risk_refine_delta():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        hard_risk.record_realized_pnl(store,-0.19)
        hard_risk.refine_realized_pnl(store,-0.19,-0.21)
        state=store.get(hard_risk.DAILY_KEY)
        assert float(state['realized_pnl'])==pytest.approx(-0.21)


def test_bracket_record_outcome_dedupes_deal():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        bracket_learn.record_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.19,deal_id='x1',
            exit_reason='sl')
        bracket_learn.record_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.21,deal_id='x1',
            exit_reason='sl')
        hist=store.get('bracket_outcome_history') or []
        rows=[r for r in hist if str(r.get('deal_id'))=='x1']
        assert len(rows)==1
        assert rows[0]['pnl']==pytest.approx(-0.21)


def test_structure_bias_soft_conf_below_veto(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'s.db')
        brain=AdaptiveBrain(store,data_dir=d)
        engine=SingleTradeEngine(store,brain)
        engine.mandate={'epic':'GOLD','size':'0.07'}
        engine.market={'mid':3700.0,'tick':0.01,'spread':0.3}
        published=[]
        monkeypatch.setattr(
            shared_market,'publish_side',
            lambda epic,side,**kw: published.append({**kw,'side':side}) or kw)
        monkeypatch.setattr(
            engine,'_decision_candles',
            lambda epic,n=80: [{'high':3701,'low':3699,'close':3700,'mid':3700}]*40)
        from app import ohlc_entry_zones
        monkeypatch.setattr(
            ohlc_entry_zones,'compute_entry_zones',
            lambda *a,**k: {'bias':'DOWN','confidence':0.1,'event':'CHOCH'})
        engine._publish_shared_market_side('GOLD',{'ready':False,'preferred':None})
        assert published
        assert published[0]['side']=='SELL'
        assert published[0]['confidence']<=0.44
