"""Losses teach counterfactual best action — never a hardcoded HOLD-on-minus rule."""
import tempfile
from pathlib import Path

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def test_loss_uses_counterfactual_not_forced_hold():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.market={'epic':'GOLD','tick':.1,'spread':.3}
        # Prior loss was BUY — flip is audit-only; target still follows the path.
        store.set('last_trade_outcome',{'direction':'BUY','pnl_sign':-1,'deal_id':'old'})
        store.upsert_trade({'dealId':'s1','epic':'GOLD','size':'.14','direction':'SELL','level':100})
        store.set('entry_brain:s1',{'epic':'GOLD','direction':'SELL','entry':100,
            'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition),
            'entry_spread':0.3})
        # SELL into a rise → counterfactual BUY beats HOLD/SELL after costs.
        e._learn_closed_trade('s1',{'level':101,'profit':-0.2})
        out=store.get('last_trade_outcome')
        assert out['mode'].startswith('flip_loss_counterfactual_buy')
        store.set('last_trade_outcome',None)
        store.upsert_trade({'dealId':'s2','epic':'GOLD','size':'.14','direction':'BUY','level':100})
        store.set('entry_brain:s2',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition),
            'entry_spread':0.3})
        e._learn_closed_trade('s2',{'level':99,'profit':-0.15})
        assert store.get('last_trade_outcome')['mode']=='loss_counterfactual_sell'
