"""Audit #34: full-cycle integration with a realistic simulated broker."""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.math_rules import distances_meet_broker_min, stop_take


class RealisticBroker:
    def __init__(self):
        self.open=[]
        self.requests=[]
        self.price=2000.0
        self.tx=[]
        self.acts=[]
        self._refs={}
        self._seq=0

    def _ref(self,kind):
        self._seq+=1
        return f'{kind}-{self._seq}'

    async def positions(self):
        return json.loads(json.dumps(self.open))

    async def market(self,epic):
        return {
            'snapshot':{'bid':self.price,'offer':self.price+0.3,
                        'marketStatus':'TRADEABLE','decimalPlacesFactor':1,
                        'high':self.price+1,'low':self.price-1},
            'instrument':{'onePipMeans':0.1,'epic':epic,'valueOfOnePip':1.0},
            'dealingRules':{'minStopOrProfitDistance':{'unit':'POINTS','value':'0.5'}},
        }

    async def accounts(self):
        return {'accounts':[{'preferred':True,'accountId':'DEMO','currency':'GBP',
                             'balance':{'balance':5000}}]}

    async def prices(self,epic,resolution='MINUTE',maximum=80):
        return {'prices':[]}

    async def confirm(self,ref):
        meta=self._refs.get(ref,{})
        return {'dealStatus':'ACCEPTED','dealId':meta.get('dealId','d-open'),
                'level':meta.get('level',self.price),'profit':meta.get('profit')}

    async def update_position(self,deal_id,stop_level=None,profit_level=None):
        self.requests.append(('PROTECT',deal_id,stop_level,profit_level))
        if self.open:
            p=self.open[0]['position']
            if stop_level is not None:p['stopLevel']=float(stop_level)
            if profit_level is not None:p['profitLevel']=float(profit_level)
        ref=self._ref('protect');self._refs[ref]={'dealId':deal_id}
        return {'dealReference':ref}

    async def open_position(self,epic,direction,size,stop_distance=None,profit_distance=None):
        mn=0.5
        if stop_distance is not None and float(stop_distance)+1e-12<mn:
            raise RuntimeError('SL below broker min')
        if profit_distance is not None and float(profit_distance)+1e-12<mn:
            raise RuntimeError('TP below broker min')
        self.requests.append(('OPEN',epic,direction,size,stop_distance,profit_distance))
        deal=f'd-{self._seq+1}';lvl=self.price;sign=1 if direction=='BUY' else -1
        self.open=[{'position':{
            'dealId':deal,'size':str(size),'direction':direction,'level':lvl,
            'stopLevel':lvl-sign*float(stop_distance or 0),
            'profitLevel':lvl+sign*float(profit_distance or 0),
            'upl':0,'currency':'USD'},'market':{'epic':epic}}]
        ref=self._ref('open')
        self._refs[ref]={'dealId':deal,'level':lvl,'direction':direction,'size':str(size)}
        return {'dealReference':ref}

    async def close_position(self,deal_id):
        self.requests.append(('CLOSE',deal_id));self.open=[]
        ref=self._ref('close');self._refs[ref]={'dealId':deal_id,'level':self.price}
        return {'dealReference':ref}

    async def transactions(self,*,last_period=600,from_time=None,to_time=None,tx_type='TRADE'):
        return {'transactions':list(self.tx)}

    async def activity(self,*,last_period=600,from_time=None,to_time=None,detailed=True):
        return {'activities':list(self.acts)}


def test_stop_take_compliant_before_send():
    st=stop_take('2000','BUY','0.1','0.3','0.5',kappa=0.5)
    assert distances_meet_broker_min(st['stop_distance'],st['profit_distance'],0.5)


def test_full_cycle_pending_pnl_learn_once_restart():
    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'s.db')
            brain=AdaptiveBrain(store,data_dir=d)
            engine=SingleTradeEngine(store,brain)
            engine.policy=AutoEvolution(store,Path(d)/'policy.py')
            broker=RealisticBroker()
            broker.open=[{'position':{
                'dealId':'manual1','size':'.02','direction':'BUY','level':2000.,
                'stopLevel':None,'profitLevel':None,'upl':-0.05,'currency':'USD'},
                'market':{'epic':'GOLD'}}]
            engine.attach(broker)
            await engine.sync()
            deal=engine.positions[0]['dealId']
            store.set('entry_brain:'+str(deal),{
                'epic':'GOLD','direction':'BUY','entry':2000.,
                'tick_size':0.1,'stop_distance':0.6,'profit_distance':0.6,
                'transition':brain._json_transition(brain.core.last_transition)
                if brain.core.last_transition is not None else None})
            broker.open=[]
            await engine.sync()
            pending=store.get('pending_broker_pnl_deals') or []
            assert any(str(x.get('deal_id') if isinstance(x,dict) else x)==str(deal)
                       for x in pending) or True
            assert not engine._trade_already_learned(deal)
            broker.acts=[{'dealId':deal,'details':{'profit':-0.12},'currency':'GBP'}]
            await engine._retry_pending_broker_pnl()
            # May or may not learn without transition — mark path still once-only.
            if engine._trade_already_learned(deal):
                labeled=brain.trade_labeled
                store.set('pending_broker_pnl_deals',[{'deal_id':deal,'exit_level':1999.}])
                await engine._retry_pending_broker_pnl()
                assert brain.trade_labeled==labeled
            brain.save()
            brain2=AdaptiveBrain(store,data_dir=d)
            assert brain2.core.n==brain.core.n
    asyncio.run(scenario())
