"""Flip-after-loss teaches HOLD — no opposite-regret whipsaw loop."""
import tempfile
from pathlib import Path

import pytest

from app.autonomy import AutoEvolution
from app.engine import SingleTradeEngine
from tests.test_learning_integrity import new_brain


def test_second_flip_loss_learns_hold_not_opposite():
    with tempfile.TemporaryDirectory() as d:
        store,brain=new_brain(d)
        brain.tick(100,.05,.01,'GOLD')
        e=SingleTradeEngine(store,brain)
        e.account={'accounts':[{'preferred':True,'currency':'GBP','accountId':'1'}]}
        e.policy=AutoEvolution(store,Path(d)/'policy.py')
        e.market={'epic':'GOLD','tick':.1,'spread':.3}
        # Prior loss was BUY — next loss on SELL is a flip → HOLD target.
        store.set('last_trade_outcome',{'direction':'BUY','pnl_sign':-1,'deal_id':'old'})
        store.upsert_trade({'dealId':'s1','epic':'GOLD','size':'.14','direction':'SELL','level':100})
        store.set('entry_brain:s1',{'epic':'GOLD','direction':'SELL','entry':100,
            'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
        e._learn_closed_trade('s1',{'level':101,'profit':-0.2})
        assert store.get('last_trade_outcome')['mode']=='chop_hold_after_flip_loss'
        # First loss alone still regrets toward opposite (no prior flip).
        store.set('last_trade_outcome',None)
        store.upsert_trade({'dealId':'s2','epic':'GOLD','size':'.14','direction':'BUY','level':100})
        store.set('entry_brain:s2',{'epic':'GOLD','direction':'BUY','entry':100,
            'tick_size':.1,'transition':brain._json_transition(brain.core.last_transition)})
        e._learn_closed_trade('s2',{'level':99,'profit':-0.15})
        assert store.get('last_trade_outcome')['mode']=='regret_loss'
