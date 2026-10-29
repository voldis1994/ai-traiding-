"""Offline evidence that cross-market and news input reaches TRAINABLE neurons."""
from datetime import datetime, timedelta, timezone
import json
import numpy as np
import pytest

from app.engine import AdaptiveBrain, Store
from app.market_context import FEATURE_DIM
from app.market_fusion import (FUSION_DIM, FUSION_SCHEMA, EXTERNAL_NAMES,
                               peer_features, news_features, fusion_features)
from app.liquidity_feed import LIQUIDITY_NAMES
from tests.test_liquidity_feed import _snapshot as liquidity_snapshot

BASE=datetime(2026,10,14,17,0,tzinfo=timezone.utc)
EXT_N=len(EXTERNAL_NAMES)


def market_state(*, bar=BASE, change=2., stable=True):
    return {'status':'READY','peers':[
        {'ready':True,'stable':stable,'last_completed_bar':bar.isoformat(),
         'correlation':.74,'holdout_correlation':.64,
         'peer_return_1m_bps':change,'peer_return_5m_bps':change*3,
         'peer_return_15m_bps':change*6},
        {'ready':True,'stable':stable,'last_completed_bar':bar.isoformat(),
         'correlation':-.64,'holdout_correlation':-.45,
         'peer_return_1m_bps':-change,'peer_return_5m_bps':-change*3,
         'peer_return_15m_bps':-change*6},
    ]}


def calendar(path, *, currencies=('USD',),when=BASE+timedelta(hours=1)):
    path.write_text(json.dumps({'enabled':True,'updated_at':BASE.isoformat(),
        'max_age_hours':24,'events':[{'at':when.isoformat(),
        'impact':'HIGH','currencies':list(currencies),'epics':[],'name':'CPI'}]}))


def _liq(path, *, bar=BASE, epic='EURUSD'):
    # Align fixture liquidity bars to the fusion bar timeline + epic.
    snap=liquidity_snapshot(bar=bar, epic=epic)
    path.write_text(json.dumps(snap),encoding='utf-8')
    return path


def fused(path, *, bar=BASE, change=2., liq_path=None):
    return fusion_features({'ready':True,'bar_time':bar.isoformat(),
        'features':[.05]*FEATURE_DIM},market_state(bar=bar,change=change),
        'EURUSD',path,['EUR','USD'],now=BASE,liquidity_path=liq_path)


def test_fused_input_has_crossmarket_and_news_with_no_future_candle(tmp_path):
    file=tmp_path/'calendar.json';calendar(file)
    liq=_liq(tmp_path/'liquidity_snapshot.json')
    vector=fused(file,liq_path=liq)
    assert vector and len(vector)==FUSION_DIM and len(EXTERNAL_NAMES)==8+len(LIQUIDITY_NAMES)
    assert vector[FEATURE_DIM:FEATURE_DIM+3]==pytest.approx([.2,.6,1.2])
    # News dims sit before liquidity tail.
    assert vector[FEATURE_DIM+6]==pytest.approx(5/6)
    assert vector[FEATURE_DIM+7]==0
    assert len(vector[FEATURE_DIM+8:])==len(LIQUIDITY_NAMES)
    # Missing peer bar is NOT converted into fake neutral data.
    stale=market_state(bar=BASE-timedelta(minutes=1))
    assert peer_features(stale,BASE.isoformat()) is None
    assert peer_features({'status':'UNAVAILABLE','peers':[]},BASE.isoformat()) is None
    assert fusion_features({'ready':True,'bar_time':BASE.isoformat(),
        'features':[0.]*FEATURE_DIM},stale,'EURUSD',file,['EUR','USD'],now=BASE,
        liquidity_path=liq) is None
    assert news_features('EURUSD',tmp_path/'missing.json',['EUR','USD'],now=BASE) is None


def test_news_currency_specific_and_future_after_event(tmp_path):
    file=tmp_path/'calendar.json';calendar(file)
    assert news_features('GBPJPY',file,['GBP','JPY'],now=BASE)==[0.,0.]
    assert news_features('EURUSD',file,['EUR','USD'],now=BASE+timedelta(hours=1,minutes=1))[1]>.9
    # Unmapped profile with FF weekly protection is a FAIL-CLOSED news input.
    config=json.loads(file.read_text());config.update(source='Forex Factory weekly JSON',
        coverage_start='2026-10-11',coverage_end='2026-10-18')
    file.write_text(json.dumps(config))
    assert news_features('UNKNOWN',file,[],now=BASE) is None


def test_v3_weights_are_preserved_and_external_gradients_learn(tmp_path):
    store=Store(tmp_path/'robot.sqlite3');brain=AdaptiveBrain(store,data_dir=tmp_path,n=10)
    # First accumulate REAL v3 state and delayed outcomes; no fake v4 data.
    for i in range(15):
        brain.tick(100+i*.2,.02,.01,'EURUSD',features=[.1]*FEATURE_DIM,
                   bar_time=(BASE+timedelta(minutes=i)).isoformat())
    assert brain.core.d==FEATURE_DIM and brain.labeled>0
    old_steps=brain.core.steps
    original=brain.core.P.copy();original_g=brain.core.G.copy()
    before=brain._path_for('EURUSD')
    assert before.exists()
    new_time=BASE+timedelta(minutes=15)
    first=brain.tick(103,.02,.01,'EURUSD',features=[.1]*FEATURE_DIM+[.6]*EXT_N,
        bar_time=new_time.isoformat(),external_features=True)
    assert first and brain.feature_schema==FUSION_SCHEMA and brain.core.d==FUSION_DIM
    assert brain.core.steps==old_steps+1
    assert np.array_equal(brain.core.P[:,:FEATURE_DIM],original)
    assert np.array_equal(brain.core.G[:FEATURE_DIM,:],original_g)
    assert before.with_name(before.stem+'.before_fusion_v4.npz').exists()
    assert brain.tick(103,.02,.01,'EURUSD',features=[.1]*FEATURE_DIM,
        bar_time=(new_time+timedelta(minutes=1)).isoformat()) is None
    start_labeled=brain.labeled
    for i in range(1,14):
        stamp=new_time+timedelta(minutes=i)
        brain.tick(103+i*.15,.02,.01,'EURUSD',features=[.1]*FEATURE_DIM+[.4+i*.04]*EXT_N,
            bar_time=stamp.isoformat(),external_features=True)
    assert brain.labeled>start_labeled and brain.core.last_loss is not None
    # Actual supervised gradients train the new external input columns.
    assert np.any(np.abs(brain.core.P[:,FEATURE_DIM:])>1e-12)
    assert brain.core.last_grad>0
    assert np.isfinite(brain.core.P).all()
    resumed=AdaptiveBrain(store,data_dir=tmp_path,n=10)
    assert resumed.core.d==FUSION_DIM and resumed.feature_schema==FUSION_SCHEMA
    assert resumed.core.steps==brain.core.steps
    assert np.array_equal(resumed.core.P,brain.core.P)
    # Another instrument starts with independent weights and normal features.
    resumed.tick(50,.02,.01,'GOLD',features=[0.1]*FEATURE_DIM,
                 bar_time=BASE.isoformat())
    assert resumed.epic=='GOLD' and resumed.core.d==FEATURE_DIM
    resumed.tick(105,.02,.01,'EURUSD',features=[.1]*FEATURE_DIM+[.8]*EXT_N,
                 bar_time=(new_time+timedelta(minutes=14)).isoformat(),external_features=True)
    assert resumed.core.d==FUSION_DIM


def test_invalid_input_never_poison_model(tmp_path):
    store=Store(tmp_path/'store.db');brain=AdaptiveBrain(store,data_dir=tmp_path)
    assert brain.tick(100,.1,.01,'EURUSD',features=[0.]*FEATURE_DIM+[float('nan')]*EXT_N,
                      bar_time=BASE.isoformat(),external_features=True) is None
    assert brain.core.d==3 and brain.core.steps==0


def test_engine_poll_reaches_neurons_from_capital_reference_feed(tmp_path,monkeypatch):
    """Integration: a broker reference series and calendar reach the neural net."""
    import asyncio
    from app import engine as engine_mod
    from app import intermarket as intermarket_mod
    from app.autonomy import AutoEvolution
    from app.engine import SingleTradeEngine
    from app.market_context import market_features
    from tests.test_integration import BrokerSimulation

    clock=[BASE+timedelta(minutes=1)]
    monkeypatch.setattr(engine_mod,'now',lambda:clock[0].isoformat())
    monkeypatch.setattr(engine_mod,'market_features',
        lambda rows,**kw:market_features(rows,now_utc=clock[0],**kw))
    old_refresh=intermarket_mod.IntermarketMonitor.refresh
    async def fixed_refresh(self,*args,**kwargs):
        return await old_refresh(self,*args,**dict(kwargs,current=clock[0],force=True))
    monkeypatch.setattr(intermarket_mod.IntermarketMonitor,'refresh',fixed_refresh)

    def history():
        start=clock[0]-timedelta(minutes=90)
        return [(start+timedelta(minutes=i),100.+i*.05+.03*(i%3)) for i in range(90)]

    class Broker(BrokerSimulation):
        async def market(self,epic):
            obj=await super().market(epic)
            obj['instrument'].update(name='Gold',currency='USD')
            return obj
        async def search_markets(self,term):
            return {'markets':[{'epic':'REF'+term.replace(' ',''),'instrumentName':term}]}
        async def prices(self,epic,resolution='MINUTE',maximum=80):
            items=[]
            for stamp,value in history():
                price={'bid':value-.01,'ask':value+.01}
                row={'snapshotTimeUTC':stamp.isoformat(),'closePrice':price}
                if epic=='SIM':
                    row.update(openPrice=price,highPrice={'bid':value+.04,'ask':value+.06},
                               lowPrice={'bid':value-.06,'ask':value-.04})
                items.append(row)
            return {'prices':items}
    calendar(tmp_path/'economic_calendar.json',when=BASE+timedelta(hours=3))
    # Liquidity bars must cover the live clock minute; epic matches broker SIM mandate.
    _liq(tmp_path/'liquidity_snapshot.json', bar=clock[0]-timedelta(minutes=1), epic='SIM')

    async def run():
        store=Store(tmp_path/'state.db')
        brain=AdaptiveBrain(store,data_dir=tmp_path)
        engine=SingleTradeEngine(store,brain)
        engine.policy=AutoEvolution(store,tmp_path/'policy.py')
        broker=Broker();engine.attach(broker)
        engine.last_calendar_refresh=1.
        engine.last_liquidity_refresh=1.
        async def no_remote_calendar():return None
        async def no_remote_liquidity():return None
        engine._refresh_calendar_if_due=no_remote_calendar
        engine._refresh_liquidity_if_due=no_remote_liquidity
        await engine.sync()
        broker.open=[]
        await engine.sync()
        epic=(engine.mandate or {}).get('epic') or 'SIM'
        # Keep liquidity snapshot aligned to the completed M1 bar under test.
        bar_guess=clock[0].replace(second=0,microsecond=0)-timedelta(minutes=1)
        _liq(engine.liquidity_path, bar=bar_guess, epic=epic)
        await engine.poll()
        # If fusion still not ready (bar_time mismatch), refresh to exact context bar.
        if not engine.latest_fusion_ready and engine.market_context.get('bar_time'):
            bt=datetime.fromisoformat(engine.market_context['bar_time'].replace('Z','+00:00'))
            _liq(engine.liquidity_path, bar=bt, epic=epic)
            # Force a second poll after liquidity aligned.
            clock[0]=clock[0]+timedelta(minutes=1)
            _liq(engine.liquidity_path, bar=clock[0]-timedelta(minutes=1), epic=epic)
            await engine.poll()
        state=engine.snapshot()
        assert brain.core.d==FUSION_DIM and brain.feature_schema==FUSION_SCHEMA
        assert state['neural']['external_features_learned'] is True
        assert state['neural']['input_dimension']==FUSION_DIM
        assert state['intermarket_analysis']['status']=='READY'
        assert brain.core.steps>=1
        assert all(entry['last_completed_bar']==state['market_context']['bar_time']
                   for entry in state['intermarket_analysis']['peers'] if entry.get('ready'))
        # With confirmed external neural inputs and a completed M1 bar, the
        # trained network may choose BUY/SELL without waiting for 60%/8.1pp.
        assert len([r for r in broker.requests if r[0]=='OPEN'])<=1
    asyncio.run(run())


def test_background_reference_fetch_is_nonblocking_and_cancellable(tmp_path):
    import asyncio
    from app.engine import SingleTradeEngine
    from app.intermarket import IntermarketMonitor
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    origin=now-timedelta(minutes=61)
    main=[{'at':(origin+timedelta(minutes=i)).isoformat(),
           'close':100+.01*i+.03*(i%3)} for i in range(61)]
    class SlowBroker:
        def __init__(self):self.gate=asyncio.Event();self.calls=0
        async def search_markets(self,term):
            self.calls+=1
            await self.gate.wait()
            return {'markets':[{'epic':'PEER'+term.replace(' ',''),
                                'instrumentName':term}]}
        async def prices(self,epic,resolution='MINUTE',maximum=90):
            return {'prices':[{'snapshotTimeUTC':bar['at'],
                'closePrice':{'bid':bar['close']-.001,'ask':bar['close']+.001}}
                for bar in main]}
    async def run():
        store=Store(tmp_path/'state.db')
        eng=SingleTradeEngine(store,AdaptiveBrain(store,data_dir=tmp_path))
        slow=SlowBroker()
        eng.broker=slow
        eng.mandate={'epic':'GOLD'}
        eng.market_context={'ready':True,'bar_time':main[-1]['at']}
        eng._refresh_peers_without_delaying_protection({'name':'Gold'},main)
        task=eng._peer_refresh_task
        assert task is not None and not task.done()  # never awaited by position manager
        await asyncio.sleep(0)
        eng._refresh_peers_without_delaying_protection({'name':'Gold'},main)
        assert eng._peer_refresh_task is task  # no duplicate remote fetch
        slow.gate.set()
        await task
        assert eng.intermarket.state['status']=='READY'
        eng.cancel_peer_refresh()
        assert eng._peer_refresh_task is None
    asyncio.run(run())


def test_stale_peer_not_second_independent_veto_after_fused_neural_readiness(tmp_path,monkeypatch):
    from app import engine as engine_mod
    from app.engine import SingleTradeEngine
    # Freeze the engine clock to the fixture calendar epoch so news freshness
    # is evaluated against the same timeline as the synthetic market bar.
    monkeypatch.setattr(engine_mod,'now',lambda:BASE.isoformat())
    st=Store(tmp_path/'db.sqlite3')
    eng=SingleTradeEngine(st,AdaptiveBrain(st,data_dir=tmp_path))
    eng.mandate={'epic':'GOLD'}
    eng.market_context={'ready':True,'bar_time':BASE.isoformat()}
    eng.last_calendar_refresh=1.
    eng.instrument_profile={'currencies':['USD']}
    calendar(tmp_path/'economic_calendar.json',when=BASE+timedelta(hours=3))
    eng.intermarket.state={'status':'READY','bar_time':(BASE-timedelta(minutes=1)).isoformat(),
                           'peers':[]}
    result=eng._external_entry_check('BUY')
    assert result['status']=='AI_INTERMARKET_FUSION'
    assert result['entry_allowed'] is True
