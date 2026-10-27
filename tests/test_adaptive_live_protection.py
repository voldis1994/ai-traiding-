"""Broker-native trail SL updates (mock calls only; no real Capital.com orders)."""
import asyncio
from datetime import datetime,timedelta,timezone
import tempfile
from pathlib import Path
import time

from app.protection_ai import select_policy,propose_broker_trail,propose_update
from app.math_rules import stop_take,ohlc_range_distance
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


def _meta(risk=.8):
    return {'owner':'AUTO','deal_id':'auto1','original_risk':risk,
            'original_profit':1.6,'opened':'2026-10-14T08:00:00+00:00'}


def test_trail_uses_opening_risk_not_live_min_glue():
    """tip±min glued SL to market → £0 Gold scratches; gap = original_risk."""
    buy=propose_broker_trail(_position(),_market(101.35,101.45),_meta(.8))
    assert buy and buy['stop_level']==100.55  # tip − opening risk
    assert buy['trail_gap']==.8
    assert buy['profit_level'] is None
    sellpos=_position('SELL',stop=100.8,profit=98.4)
    sell=propose_broker_trail(sellpos,_market(98.55,98.65),_meta(.8))
    assert sell and sell['stop_level']==99.45  # tip + opening risk
    assert sell['trail_gap']==.8


def test_trail_waits_until_favorable_covers_opening_gap():
    # Only +0.4 favorable with gap 0.8 → no ratchet (would glue near entry).
    assert propose_broker_trail(_position(),_market(100.4,100.5),_meta(.8)) is None


def test_stop_never_widens_if_existing_better_and_no_manual_trade():
    assert propose_broker_trail(_position(stop=101.1),_market(101.35,101.45),_meta(.8)) is None
    assert propose_broker_trail(_position(),_market(101.35,101.45),dict(_meta(),owner='MANUAL')) is None
    assert propose_broker_trail(_position(),_market(101.35,101.45),dict(_meta(),deal_id='other')) is None
    assert propose_broker_trail(_position(),dict(_market(101.35,101.45),status='CLOSED'),_meta()) is None


def test_broker_trail_does_not_invent_tp_extension():
    current=_market(bid=101.1,ask=101.2)
    p=_position(stop=99.2,profit=101.6)
    result=propose_broker_trail(p,current,_meta(.8))
    assert result and result['profit_level'] is None
    assert result['stop_level']==101.1-0.8


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
    store.set('adaptive_protection:auto1',dict(_meta(.8),epic='GOLD'))
    return eng,broker,store


def test_live_update_protects_with_confirmation_and_throttle():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d)
            await e.sync()
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
            assert b.open[0]['position']['stopLevel']>99.2
            assert not s.unresolved()
            assert s.get('adaptive_runtime:auto1')['status']=='CONFIRMED'
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
    assert propose_broker_trail(p,_market(102.,102.1),_meta()) is None
    assert propose_broker_trail(p,dict(_market(101.35,101.45),min_distance=None),_meta()) is None


def test_broker_rejection_backoff_then_retries():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            e,b,s=engine_fixture(d);b.reject=True
            await e.sync();await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
            await e.adapt_protection(e.positions[0])
            assert len(b.requests)==1
            rt=s.get('adaptive_runtime:auto1')
            rt['mono']=time.monotonic()-20
            s.set('adaptive_runtime:auto1',rt)
            b.reject=False
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


def test_tick_prefers_decimal_places_over_one_pip_means():
    from app.engine import SingleTradeEngine
    market={
        'snapshot':{'decimalPlacesFactor':1,'bid':4004,'offer':4004.3},
        'instrument':{'onePipMeans':0.025},
        'dealingRules':{'minStopOrProfitDistance':{'unit':'POINTS','value':2}},
    }
    assert SingleTradeEngine._tick(market)==0.1
    assert SingleTradeEngine._min_distance(market,4004.15)==2.0


def test_sell_4015_trails_by_opening_risk_not_min_glue():
    """SELL 4015, extreme 4004, opening risk 5 → stop 4009 (not tip+min=4006 glued)."""
    market={'epic':'IDX','bid':4003.7,'ask':4004.0,'spread':0.3,'tick':0.1,
            'min_distance':2.0,'status':'TRADEABLE'}
    position={'dealId':'d1','epic':'IDX','direction':'SELL','level':4015.0,
              'stopLevel':4020.0,'profitLevel':3990.0,'size':'.5'}
    meta={'owner':'AUTO','deal_id':'d1','original_risk':5.0,'original_profit':25.0}
    prop=propose_broker_trail(position,market,meta,extreme=4004.0)
    assert prop and prop['stop_level']==4009.0
    assert prop['trail_gap']==5.0
    assert prop['stop_level']!=4006.075


def test_ohlc_range_widens_open_bracket_past_min_only():
    bars=[{'high':100.8,'low':100.1},{'high':100.9,'low':100.2},{'high':100.7,'low':100.0}]
    rng=ohlc_range_distance(bars,n=3)
    assert float(rng)==0.7
    # Without range: min+buffer only; with range: uses broker M1 median range.
    tight=stop_take(100,'BUY',0.01,0.05,0.3,kappa=2,sl_scale=1,range_distance=0)
    wide=stop_take(100,'BUY',0.01,0.05,0.3,kappa=2,sl_scale=1,range_distance=float(rng))
    assert wide['stop_distance']>tight['stop_distance']
    assert wide['stop_ticks']>=70


def test_scratch_close_sets_entry_cooldown_after_streak():
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'t.sqlite3')
        e=SingleTradeEngine(s,Brain())
        e._note_scratch_close(0.0,'d1')
        assert s.get('entry_scratch_cooldown') in (None,0)
        e._note_scratch_close(-0.02,'d2')
        assert s.get('entry_scratch_cooldown')==3
        e._note_scratch_close(-1.5,'d3')  # real loss resets streak
        assert s.get('scratch_streak')==0
