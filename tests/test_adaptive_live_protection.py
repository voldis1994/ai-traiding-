"""Evidence-led broker SL/TP updates (mock calls only; no real Capital.com orders)."""
import asyncio
from datetime import datetime,timedelta,timezone
import tempfile
from pathlib import Path

from app.protection_ai import select_policy,propose_update
from app.engine import Store,SingleTradeEngine
from tests.test_integration import BrokerSimulation


def scenario_bars(n=181):
    x=100.;t=datetime(2026,10,14,tzinfo=timezone.utc);bars=[]
    moves=[.0,.6,.7,-.1,-.5,-.6,-.4,.3]
    for i in range(n):
        o=x;x+=moves[i%8]
        bars.append({'at':(t+timedelta(minutes=i)).isoformat(),
                     'open':o,'high':max(o,x)+.02,'low':min(o,x)-.02,'close':x})
    return bars


def test_walk_forward_candidate_beats_baseline_on_completed_history():
    learned=select_policy(scenario_bars(),'BUY',.8,1.6,.1)
    assert learned['ready'] and learned['samples']>=15
    assert learned['holdout_score_r']>learned['baseline_holdout_r']
    assert 0<learned['retention']<1
    assert learned['tp_multiplier']>=1


def test_policy_rejects_insufficient_history_and_invalid_input():
    assert not select_policy(scenario_bars(50),'BUY',.8,1.6,.1)['ready']
    assert not select_policy(scenario_bars(),'BUY',0,1.6,.1)['ready']
    assert not select_policy(scenario_bars(),'SELL',.8,1.6,-.1)['ready']


def _market(bid=103.0,ask=103.1):
    return {'epic':'GOLD','bid':bid,'ask':ask,'spread':ask-bid,
            'tick':.01,'min_distance':.3,'status':'TRADEABLE'}


def _position(direction='BUY',stop=99.2,profit=101.6):
    return {'dealId':'auto1','epic':'GOLD','direction':direction,'level':100.,
            'stopLevel':stop,'profitLevel':profit,'size':'.25'}


def _meta(trend=True):
    return {'owner':'AUTO','deal_id':'auto1','original_risk':.8,
            'original_profit':1.6,'trend_aligned':trend}

POLICY={'ready':True,'retention':.5,'activation_r':1.2,'tp_multiplier':1.5}


def test_stop_moves_only_toward_profit_on_long_and_short():
    buy=propose_update(_position(),_market(101.35,101.45),_meta(False),POLICY,'2026-10-14T09:00:00+00:00')
    assert buy and 99.2<buy['stop_level']<=101.35-.3
    assert buy['profit_level'] is None
    sellpos=_position('SELL',stop=100.8,profit=98.4)
    sellmarket=_market(98.55,98.65)
    sell=propose_update(sellpos,sellmarket,_meta(False),POLICY,'2026-10-14T09:00:00+00:00')
    assert sell and 98.65+.3<=sell['stop_level']<100.8
    assert sell['profit_level'] is None


def test_stop_never_widens_if_existing_better_and_no_manual_trade():
    assert propose_update(_position(stop=101.1),_market(101.35,101.45),_meta(False),POLICY,'b') is None
    assert propose_update(_position(),_market(101.35,101.45),dict(_meta(),owner='MANUAL'),POLICY,'b') is None
    assert propose_update(_position(),_market(101.35,101.45),dict(_meta(),deal_id='other'),POLICY,'b') is None
    assert propose_update(_position(),dict(_market(101.35,101.45),status='CLOSED'),_meta(),POLICY,'b') is None


def test_tp_extension_only_with_aligned_trend_and_broker_distance():
    current=_market(bid=101.1,ask=101.2)
    p=_position(stop=99.2,profit=101.6)
    result=propose_update(p,current,_meta(True),dict(POLICY,activation_r=.8), 'b')
    assert result and result['profit_level'] is not None
    assert result['profit_level']>101.6
    other=propose_update(p,current,_meta(False),dict(POLICY,activation_r=.8),'b')
    assert other and other['profit_level'] is None


class Brain:
    class Core:
        steps=20
    core=Core()


class UpdatingBroker(BrokerSimulation):
    def __init__(self):
        super().__init__()
        self.open=[{'position':{'dealId':'auto1','size':'.25','direction':'BUY','level':100.,
                    'stopLevel':99.2,'profitLevel':101.6,'upl':0},'market':{'epic':'GOLD'}}]
        self.return_old=False
        self.fail=False
        self.reject=False
    async def update_position(self,deal_id,stop_level=None,profit_level=None):
        self.requests.append(('PROTECT',deal_id,stop_level,profit_level))
        if self.fail:raise TimeoutError('Uncertain broker response')
        if not self.return_old and not self.reject:
            if stop_level is not None:self.open[0]['position']['stopLevel']=float(stop_level)
            if profit_level is not None:self.open[0]['position']['profitLevel']=float(profit_level)
        return {'dealReference':'change1'}
    async def confirm(self,ref):return {'dealStatus':'REJECTED' if self.reject else 'ACCEPTED','dealId':'auto1'}


def engine_fixture(tmp):
    store=Store(Path(tmp)/'test.sqlite3')
    eng=SingleTradeEngine(store,Brain());broker=UpdatingBroker()
    eng.broker=broker;eng.armed=True;eng.execution_enabled=True
    eng.mandate={'epic':'GOLD','size':'.25','initial_deal_id':'manual1'}
    eng.market=_market(101.35,101.45)
    eng.market_context={'ready':True,'bar_time':'2026-10-14T09:00:00+00:00',
                        'returns_bps':{'5':4.,'15':5.}}
    store.set('adaptive_protection:auto1',dict(_meta(),epic='GOLD'))
    # Code normally reads broker candles; inject a known, valid path.
    for c in scenario_bars():store.candle('GOLD',c,source='HISTORICAL')
    return eng,broker,store


def test_live_update_protects_with_confirmation_and_no_duplicate_bar():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d)
            await e.sync()
            await e.adapt_protection(e.positions[0])
            # historical policy selects winner; broker levels should be updated
            assert len(b.requests)==1
            assert b.open[0]['position']['stopLevel']>99.2
            assert not s.unresolved()
            assert s.get('adaptive_runtime:auto1')['bar_time']=='2026-10-14T09:00:00+00:00'
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
    asyncio.run(scenario())


def test_stale_broker_levels_are_not_false_confirmed():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d);b.return_old=True
            await e.sync()
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
            assert not e.execution_enabled and not e.armed
            assert len(s.unresolved())==1
            assert e.protection_brief['status'].startswith('Uncertain')
    asyncio.run(scenario())


def test_broker_rejection_does_not_remove_old_stop_or_retry():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d);b.reject=True
            await e.sync()
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
            assert not s.unresolved()
            assert e.armed and e.execution_enabled
            assert b.open[0]['position']['stopLevel']==99.2
            await e.adapt_protection(e.positions[0]);assert len(b.requests)==1
    asyncio.run(scenario())


def test_legacy_manual_order_is_not_modified():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d);s.set('adaptive_protection:auto1',None)
            await e.sync();await e.adapt_protection(e.positions[0])
            assert not b.requests
    asyncio.run(scenario())


def test_protection_reconciliation_uses_actual_requested_levels(tmp_path):
    s=Store(tmp_path/'t.sqlite3')
    e=SingleTradeEngine(s,Brain())
    p=_position()
    assert e._protection_matches(p,{'stop':99.2,'tick':.01})
    assert not e._protection_matches(p,{'stop':100.,'tick':.01})
    assert not e._protection_matches(p,{'profit':102.,'tick':.01})


def test_crossed_tp_or_poor_broker_rules_do_not_trigger_put():
    p=_position()
    assert propose_update(p,_market(102.,102.1),_meta(),POLICY,'b') is None
    assert propose_update(p,dict(_market(101.35,101.45),min_distance=None),_meta(),POLICY,'b') is None


def test_broker_rejection_backoff_survives_next_candle():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d);b.reject=True
            await e.sync();await e.adapt_protection(e.positions[0])
            e.market_context['bar_time']='2026-10-14T09:01:00+00:00'
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
            e.market_context['bar_time']='2026-10-14T09:20:00+00:00'
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==2
    asyncio.run(scenario())


class CorrectOpeningBroker(BrokerSimulation):
    async def open_position(self,epic,direction,size,stop_distance=None,profit_distance=None):
        self.requests.append(('OPEN',epic,direction,size,stop_distance,profit_distance))
        sign=1 if direction=='BUY' else -1
        entry=self.price
        self.open=[{'position':{'dealId':'b2','size':str(size),'direction':direction,'level':entry,
              'stopLevel':entry-sign*float(stop_distance),
              'profitLevel':entry+sign*float(profit_distance)},'market':{'epic':epic}}]
        return {'dealReference':'auto-open-2'}


def test_confirmed_sell_open_registers_persistent_adaptive_rights():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            from app.engine import AdaptiveBrain
            s=Store(Path(d)/'state.sqlite3');brain=AdaptiveBrain(s,data_dir=d)
            e=SingleTradeEngine(s,brain);b=CorrectOpeningBroker()
            e.attach(b);await e.sync()
            b.open=[];await e.sync()
            e.market={'epic':'SIM','mid':100.,'bid':100.,'ask':100.02,'spread':.02,
                      'tick':.01,'min_distance':.15,'status':'TRADEABLE'}
            e.market_context={'ready':True}
            await e.submit('SELL')
            meta=s.get('adaptive_protection:b2')
            assert meta and meta['owner']=='AUTO' and meta['epic']=='SIM'
            assert meta['original_risk']>0 and meta['original_profit']>0
            assert len([x for x in b.requests if x[0]=='OPEN'])==1
            assert not s.unresolved()
    asyncio.run(scenario())
