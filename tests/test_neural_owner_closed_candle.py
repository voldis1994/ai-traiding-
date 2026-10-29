"""User-requested behavior: AI alone chooses direction per confirmed M1 bar.

All tests are OFFLINE broker simulations. No LIVE Capital.com operations.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import numpy as np

from app.autonomy import AutoEvolution
from app.engine import Store, AdaptiveBrain, SingleTradeEngine
from app.market_context import FEATURE_DIM, FEATURE_SCHEMA
from tests.test_integration import BrokerSimulation


def sandbox():
    return tempfile.TemporaryDirectory()


def test_ai_no_entry_confidence_threshold_and_hold_still_its_own_choice():
    with sandbox() as d:
        p=AutoEvolution(Store(Path(d)/'db.sqlite3'), Path(d)/'active_policy.py')
        # 38%: previously forbidden by 60% and by 8.1pp margin
        score={'BUY':.32, 'SELL':.38, 'HOLD':.30}
        assert p.neural_decision(score)=='SELL'
        # LIVE entry = neural argmax — zone/SHIFT are evidence only, not vetoes.
        bare=p.evaluate_neural_entry(score)
        assert bare['action']=='SELL' and bare['proposal']=='SELL'
        down={'5':-2,'15':-3,'60':-4}
        result=p.evaluate_neural_entry(score,returns_bps=down,trend='DOWN',regime='ALIGNED')
        assert result['action']=='SELL'
        assert result['threshold'] is None
        assert result['evidence'] is not None
        assert p.neural_decision({'BUY':.34,'SELL':.33,'HOLD':.33})=='BUY'
        assert p.neural_decision({'BUY':.25,'SELL':.35,'HOLD':.40})=='HOLD'
        blocked=p.evaluate_neural_entry({'BUY':.25,'SELL':.35,'HOLD':.40},
            returns_bps={'5':-1,'15':-2,'60':-3},trend='DOWN')
        assert blocked['action']=='HOLD' and blocked['proposal']=='SELL'
        assert p.evaluate_neural_entry({'BUY':.9,'SELL':.05,'HOLD':.05})['action']=='BUY'
        up={'5':2,'15':3,'60':4}
        tied=p.evaluate_neural_entry({'BUY':.34,'SELL':.33,'HOLD':.33},
            returns_bps=up,trend='UP',regime='ALIGNED')
        assert tied['action']=='BUY' and tied['proposal']=='BUY'
        entered=p.evaluate_neural_entry({'BUY':.45,'SELL':.28,'HOLD':.27},
            returns_bps=up,trend='UP',regime='ALIGNED')
        assert entered['action']=='BUY'
        # Against trend / SHIFT — still argmax (no hardcode veto).
        assert p.evaluate_neural_entry({'BUY':.8,'SELL':.1,'HOLD':.1},
            returns_bps={'5':-2,'15':-3,'60':-4},trend='DOWN')['action']=='BUY'
        assert p.evaluate_neural_entry({'BUY':.8,'SELL':.1,'HOLD':.1},
            returns_bps={'5':2,'15':3,'60':-4},regime='SHIFT')['action']=='BUY'
        # Raw vote still reports CLOSE intent; live exit needs zone + streak.
        assert p.neural_decision(score,current_direction='BUY')=='CLOSE'
        assert p.neural_decision(score,current_direction='SELL')=='HOLD'
        assert p.neural_decision({'BUY':float('nan'),'SELL':.6,'HOLD':.4})=='HOLD'
        # One opposite M1 without DOWN zone / streak must NOT close a long.
        assert AutoEvolution.evaluate_neural_exit(
            score,'BUY',returns_bps={'5':-2,'15':-3},opposite_streak=1)=='HOLD'
        assert AutoEvolution.evaluate_neural_exit(
            score,'BUY',returns_bps={'5':-2,'15':-3},opposite_streak=2)=='CLOSE'
        # Zone disagrees (still green 5m/15m) → hold even with streak.
        assert AutoEvolution.evaluate_neural_exit(
            score,'BUY',returns_bps={'5':1,'15':2},opposite_streak=5)=='HOLD'


def test_second_manual_deal_changes_lot_and_recovers_after_closed():
    async def scenario():
        with sandbox() as d:
            store=Store(Path(d)/'db.sqlite3')
            brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain)
            e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation();e.attach(broker)
            await e.sync()
            assert e.mandate['size']=='.25'
            broker.open=[];await e.sync()
            assert e.last_closed_step>=0
            broker.open=[{'position':{'dealId':'m2','size':'.5','direction':'SELL','level':100,
                    'stopLevel':99.5,'profitLevel':98,'upl':0},'market':{'epic':'SIM'}}]
            await e.sync()
            assert e.mandate['size']=='.5'
            assert e.mandate['last_manual_deal_id']=='m2'
            assert e.last_closed_step==-1
            await e.sync()
            assert e.mandate['last_manual_deal_id']=='m2'
            broker.open=[];await e.sync()
            assert e.last_closed_step>=0
            e.market={'mid':100.01,'spread':.02,'bid':100.,'ask':100.02,'tick':.01,
                      'epic':'SIM','status':'TRADEABLE','min_distance':.15}
            e.market_context={'ready':True}
            e._external_entry_check=lambda _: {'entry_allowed':True,'status':'TEST'}
            brain.mark_prep_ready('test fixture')
            await e.submit('BUY')
            orders=[r for r in broker.requests if r[0]=='OPEN']
            assert len(orders)==1 and orders[0][3]==Decimal('.5')
            assert e.mandate['last_manual_deal_id']=='m2'  # robot cannot masquerade as a new manual order
            assert store.get('robot_open:b2') is True
            assert e.last_closed_step>=0
            assert e.positions[0]['dealId']=='b2'
            restarted=SingleTradeEngine(store,brain)
            restarted.attach(broker)
            await restarted.sync()
            # Restart clears SQLite mandate, then re-adopts the LIVE open deal
            # for THIS session (size/epic from broker — not the old cache).
            assert restarted.mandate is not None
            assert str(restarted.mandate['size']) in ('.5','0.5')
            assert restarted.mandate['initial_deal_id']=='b2'
            assert restarted.last_closed_step==-1
            assert restarted.positions and restarted.positions[0]['dealId']=='b2'
    asyncio.run(scenario())


def test_new_manual_market_adopted_without_switch_button():
    async def scenario():
        with sandbox() as d:
            store=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain);broker=BrokerSimulation()
            e.policy=AutoEvolution(store,Path(d)/'policy.py');e.attach(broker)
            await e.sync();broker.open=[];await e.sync()
            brain.core.steps=1500 # Old GOLD model may have learned for many days.
            broker.open=[{'position':{'dealId':'new_market_m1','size':'1.5','direction':'BUY',
                'level':200,'stopLevel':190,'profitLevel':220},'market':{'epic':'NEW.EPIC'}}]
            await e.sync()
            assert e.mandate['epic']=='NEW.EPIC'
            assert e.brain.epic=='NEW.EPIC'
            assert e.brain.core.steps<1500
            assert e.mandate['size']=='1.5'
            assert e.last_closed_step==-1
            assert e.market_context['ready'] is False
            assert e.last_signal is None
            assert not e._paused_for_mismatch
    asyncio.run(scenario())


def test_signals_and_exits_only_once_per_completed_m1_bar(monkeypatch):
    from app import engine as module
    clock=[datetime(2026,10,14,16,30,tzinfo=timezone.utc)]
    def context(rows,*,spread=0.,epic=None,**_kw):
        return {'ready':True,'reason':'synthetic completed M1','epic':epic,
                'bar_time':clock[0].isoformat(),'close':100.0,
                'features':[.01]*FEATURE_DIM,'trend':'UP','regime':'ALIGNED',
                'returns_bps':{'1':1,'5':2,'15':3,'60':4},'feature_schema':FEATURE_SCHEMA}
    monkeypatch.setattr(module,'market_features',context)
    async def scenario():
        with sandbox() as d:
            store=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain);broker=BrokerSimulation()
            e.policy=AutoEvolution(store,Path(d)/'policy.py');e.attach(broker)
            await e.poll()  # manual position adopted and protected
            broker.open=[];await e.sync()  # manually closed
            e._external_entry_check=lambda _: {'entry_allowed':True,'status':'TEST'}
            brain.mark_prep_ready('test fixture')
            brain.core.b=np.array([8.,-3.,-3.]) # BUY dominant
            # Same confirmed M1 bar already learned while manual was open.
            for _ in range(6): await e.poll()
            assert not [r for r in broker.requests if r[0]=='OPEN']
            assert e.brain.core.steps==1  # one model step, not six tick steps
            clock[0]+=timedelta(minutes=1)
            await e.poll()
            assert len([r for r in broker.requests if r[0]=='OPEN'])==1
            assert e.mandate['initial_deal_id']=='m1'
            bar=e.last_decision_bar
            for _ in range(5): await e.poll()
            assert e.last_decision_bar==bar
            assert len([r for r in broker.requests if r[0]=='OPEN'])==1
            assert e.brain.core.steps==2
            count=store.db.execute("SELECT COUNT(*) FROM events WHERE kind='entry'").fetchone()[0]
            assert count==1
            assert e.snapshot()['neural_decision_bar']==bar
    asyncio.run(scenario())


def test_position_exit_never_market_deletes_neural_intent(monkeypatch):
    """Zone+streak may score CLOSE intent, but live exit is SL/TP/trail only."""
    from app import engine as module
    clock=[datetime(2026,10,14,17,10,tzinfo=timezone.utc)]
    def context(rows,*,spread=0.,epic=None,**_kw):
        return {'ready':True,'reason':'synthetic completed M1','epic':epic,
                'bar_time':clock[0].isoformat(),'close':100.,'features':[.02]*FEATURE_DIM,
                'trend':'DOWN','regime':'ALIGNED',
                'returns_bps':{'1':-1,'5':-4,'15':-5,'60':-6},
                'feature_schema':FEATURE_SCHEMA}
    monkeypatch.setattr(module,'market_features',context)
    async def scenario():
        with sandbox() as d:
            store=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(store,data_dir=d)
            e=SingleTradeEngine(store,brain);e.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation();broker.open[0]['position'].update(stopLevel=99.,profitLevel=101.)
            e.attach(broker)
            e._schedule_calendar_in_background=lambda:None
            exits=[]
            async def fake_exit(deal_id):exits.append(deal_id)
            e.exit_position=fake_exit
            await e.poll()
            exits.clear()
            brain.core.b=np.array([-4.,8.,-4.])  # SELL opposes manual BUY
            for _ in range(5):
                clock[0]+=timedelta(minutes=1)
                await e.poll()
            assert exits==[]  # never market-delete; broker SL/TP must close
            snap=e.snapshot()
            assert snap.get('neural_market_exit') is False
            events=store.recent(50)
            assert any(ev.get('kind')=='exit' and 'SL/TP' in str(ev.get('message',''))
                       for ev in events)
    asyncio.run(scenario())
