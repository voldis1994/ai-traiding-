"""Safety / multi-timeframe integration using offline, deterministic broker candles."""
import asyncio
import tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pytest

from app.market_context import market_features, candle_bucket, FEATURE_DIM, FEATURE_SCHEMA
from app.engine import Store,AdaptiveBrain,SingleTradeEngine
from app.autonomy import AutoEvolution
from tests.test_integration import BrokerSimulation

BASE=datetime(2026,10,13,17,0,tzinfo=timezone.utc)


def bars(count=90, start=BASE-timedelta(minutes=90), delta=0.1, price=100):
    out=[]
    for i in range(count):
        c=price+i*delta
        out.append({'at':(start+timedelta(minutes=i)).isoformat(),
            'open':c-.02,'high':c+.06,'low':c-.06,'close':c})
    return out


def test_uses_complete_minutes_not_future_or_active_candle():
    history=bars(91,start=BASE-timedelta(minutes=90))  # latest = BASE
    at=BASE+timedelta(minutes=1)
    first=market_features(history,now_utc=at,spread=.03,epic='GOLD')
    assert first['ready'] and first['trend']=='UP'
    assert first['returns_bps']['15']>0 and first['returns_bps']['60']>0
    assert len(first['features'])==FEATURE_DIM
    sabotaged=history+[{'at':at.isoformat(),'open':3000,'high':9000,'low':1,'close':3000}]
    second=market_features(sabotaged,now_utc=at,spread=.03,epic='GOLD')
    assert second['features']==first['features']
    assert second['bar_time']==first['bar_time']


def test_current_green_candle_cannot_reverse_broad_downtrend():
    history=bars(91,start=BASE-timedelta(minutes=90),delta=-.1)
    history[-1]['open']=history[-1]['close']-.2
    history[-1]['high']=max(history[-1]['open'],history[-1]['close'])+.01
    history[-1]['low']=min(history[-1]['open'],history[-1]['close'])-.01
    result=market_features(history,now_utc=BASE+timedelta(minutes=1),spread=.03)
    assert result['ready'] and result['trend']=='DOWN'
    assert result['returns_bps']['15']<0 and result['returns_bps']['60']<0
    assert result['features'][8]>0  # green latest candle but overall down


def test_short_gap_old_history_and_bad_price_rejected():
    history=bars(91,start=BASE-timedelta(minutes=90))
    assert not market_features(history[:30],now_utc=BASE+timedelta(minutes=1))['ready']
    gapped=[x for x in history if x['at']!=(BASE-timedelta(minutes=2)).isoformat()]
    assert not market_features(gapped,now_utc=BASE+timedelta(minutes=1))['ready']
    assert 'stale' in market_features(history,now_utc=BASE+timedelta(hours=2))['reason'].lower()
    broken=history.copy();broken[-1]=dict(broken[-1],close=-5)
    assert not market_features(broken,now_utc=BASE+timedelta(minutes=1))['ready']
    assert candle_bucket('2026-10-13T17:00:10Z')==BASE.isoformat()


def test_different_price_units_same_return_vector():
    history=bars(91,start=BASE-timedelta(minutes=90),delta=.1,price=100)
    scaled=[{k:(v*100 if k!='at' else v) for k,v in item.items()} for item in history]
    a=market_features(history,now_utc=BASE+timedelta(minutes=1),spread=.03)
    b=market_features(scaled,now_utc=BASE+timedelta(minutes=1),spread=3)
    assert a['features']==pytest.approx(b['features'],abs=1e-9)


def test_mtf_model_migrates_backing_up_legacy_without_deleting_db():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(store,data_dir=d)
        brain.tick(100,.05,.01,'GOLD')
        before=brain._path_for('GOLD')
        assert before.exists() and brain.core.d==3
        brain.tick(100.1,.05,.01,'GOLD',features=[.2]*FEATURE_DIM,bar_time=BASE.isoformat())
        backup=before.with_name(before.stem+'.before_mtf_v3.npz')
        assert backup.exists() and brain.core.d==FEATURE_DIM and brain.feature_schema==FEATURE_SCHEMA
        assert brain.labeled==0
        other=AdaptiveBrain(store,data_dir=d)
        assert other.core.d==FEATURE_DIM and other.feature_schema==FEATURE_SCHEMA
        assert store.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]==2


def test_mtf_retrain_once_per_bar_and_gap_resets_unresolved_labels():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(store,data_dir=d)
        for i in range(12):
            timestamp=(BASE+timedelta(minutes=i)).isoformat()
            assert brain.tick(100+i*.1,.03,.01,'GOLD',features=[.2]*FEATURE_DIM,bar_time=timestamp)
            assert brain.tick(100+i*.1,.03,.01,'GOLD',features=[.2]*FEATURE_DIM,bar_time=timestamp) is None
        assert brain.core.steps==12 and brain.labeled==11  # HORIZON=1
        assert brain.tick(104,.03,.01,'GOLD',features=[.2]*FEATURE_DIM,
                          bar_time=(BASE+timedelta(minutes=30)).isoformat())
        assert brain.labeled==11  # gap clears pending; do not invent a label across the hole
        assert len(brain.pending)==1


def test_live_engine_waits_for_ohlc_but_still_protects_manual_position(monkeypatch):
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(store,data_dir=d)
            engine=SingleTradeEngine(store,brain);engine.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=BrokerSimulation();engine.attach(broker)
            await engine.poll()
            assert not engine.market_context['ready']
            assert brain.core.steps==0 and engine.last_signal is None
            assert any(req[0]=='PROTECT' for req in broker.requests)
            broker.open=[];await engine.sync()
            await engine.submit('BUY')
            assert not any(req[0]=='OPEN' for req in broker.requests)
    asyncio.run(scenario())


def test_market_context_for_real_broker_candles_and_minute_learning(monkeypatch):
    from app import engine as engine_mod
    clock=[BASE+timedelta(minutes=1)]
    monkeypatch.setattr(engine_mod,'now',lambda:clock[0].isoformat())
    original=engine_mod.market_features
    monkeypatch.setattr(engine_mod,'market_features',lambda rows,**kwargs:original(rows,now_utc=clock[0],**kwargs))
    class HistoricalBroker(BrokerSimulation):
        async def prices(self,epic,resolution='MINUTE',maximum=80):
            history=bars(90,start=clock[0]-timedelta(minutes=90))
            return {'prices':[{'snapshotTimeUTC':p['at'],
                'openPrice':{'bid':p['open']-.01,'ask':p['open']+.01},
                'highPrice':{'bid':p['high']-.01,'ask':p['high']+.01},
                'lowPrice':{'bid':p['low']-.01,'ask':p['low']+.01},
                'closePrice':{'bid':p['close']-.01,'ask':p['close']+.01}} for p in history]}
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'db.sqlite3');brain=AdaptiveBrain(s,data_dir=d)
            e=SingleTradeEngine(s,brain);e.policy=AutoEvolution(s,Path(d)/'policy.py')
            broker=HistoricalBroker();e.attach(broker)
            await e.poll()
            assert e.market_context['ready'] and brain.core.steps==1
            assert e.last_signal and brain.core.d==FEATURE_DIM
            await e.poll()
            assert brain.core.steps==1  # same completed candle cannot generate repeated learning
            for step in range(1,10):
                clock[0]=BASE+timedelta(minutes=1+step)
                e.last_candle_tick=-20  # request new minute history in deterministic test
                await e.poll()
            assert brain.core.steps==10
            assert brain.labeled==9  # HORIZON=1: ten M1 steps → nine next-bar labels
            assert e.snapshot()['market_context']['trend']=='UP'
    asyncio.run(scenario())


def test_evolution_not_retried_repeatedly_on_same_label_count():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3');policy=AutoEvolution(store,Path(d)/'policy.py')
        policy.last_change=0
        policy.evolve(.4,20,[])
        previous=policy.attempts
        policy.last_change=0  # even if time passes, same labeled sample count
        policy.evolve(.4,20,[])
        assert policy.attempts==previous
        policy.evolve(.4,40,[])
        assert policy.attempts==previous+1


def test_chart_quote_candles_are_not_model_training_history():
    with tempfile.TemporaryDirectory() as d:
        store=Store(Path(d)/'db.sqlite3')
        for item in bars(91,start=BASE-timedelta(minutes=90)):
            store.candle('GOLD',item)  # quote source, no broker OHLC confirmation
        assert len(store.candles('GOLD'))==91
        assert store.candles('GOLD',historical_only=True)==[]
        result=market_features(store.candles('GOLD',historical_only=True),
            now_utc=BASE+timedelta(minutes=1),spread=.03,epic='GOLD')
        assert not result['ready']
        for item in bars(91,start=BASE-timedelta(minutes=90)):
            store.candle('GOLD',item,source='HISTORICAL')
        assert len(store.candles('GOLD',historical_only=True))==91


def test_legacy_candles_table_migrates_without_trusting_old_bars():
    import sqlite3
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'old.sqlite3'
        db=sqlite3.connect(str(path));db.execute('CREATE TABLE candles (epic TEXT,at TEXT,open REAL,high REAL,low REAL,close REAL,PRIMARY KEY(epic,at))')
        db.execute("INSERT INTO candles VALUES('GOLD','2026-10-13T17:00:00+00:00',100,101,99,100)")
        db.commit();db.close()
        store=Store(path)
        assert len(store.candles('GOLD'))==1
        assert not store.candles('GOLD',historical_only=True)
