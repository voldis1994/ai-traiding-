"""Audit #15 path counterfactual + #16 unified trade cost accounting."""
import math

import pytest

from app.profit_learning import (best_action_from_counterfactual, hypothetical_pnl,
                                 label_from_profit)
from app.shadow_paper import simulate_action_on_path, _fill_price
from app.trade_costs import (fill_price, fill_to_fill_pnl, half_spread,
                             net_forward_bps, net_mid_move, round_trip_spread_bps,
                             round_trip_spread_cost)
from app.feature_validation import _trade_net
from app.autonomy import AutoEvolution


def test_round_trip_spread_never_double_counts():
    # Full width 0.4 in + 0.6 out → one RT = 0.5, not 1.0.
    assert round_trip_spread_cost(0.4,0.6)==pytest.approx(0.5)
    assert round_trip_spread_cost(0.4)==pytest.approx(0.4)
    assert round_trip_spread_bps(10.,20.)==pytest.approx(15.)
    # fill-to-fill embeds half+half once.
    mid_pnl=net_mid_move('BUY',100.,101.,spread_entry=0.4,spread_exit=0.6)
    fill_pnl=fill_to_fill_pnl('BUY',100.,101.,spread_entry=0.4,spread_exit=0.6)
    assert mid_pnl==pytest.approx(1.0-0.5)
    assert fill_pnl==pytest.approx(mid_pnl)


def test_shadow_and_shared_fill_price_match():
    assert fill_price('BUY',100.,0.4,slip_bps=0)==pytest.approx(100.2)
    assert fill_price('SELL',100.,0.4,slip_bps=0)==pytest.approx(99.8)
    assert _fill_price('BUY',100.,0.4)==pytest.approx(fill_price('BUY',100.,0.4))
    assert half_spread(0.4)==pytest.approx(0.2)


def test_hypothetical_pnl_uses_shared_one_rt_cost():
    # Was: move - |spread|; still one RT, not two.
    assert hypothetical_pnl('BUY',100,101,spread=0.2)==pytest.approx(0.8)
    assert hypothetical_pnl('BUY',100,101,spread=0.2,spread_exit=0.4)==pytest.approx(0.7)


def test_feature_validation_trade_net_not_double_spread():
    closes=[100.,100.,100.,100.,101.]
    spreads=[0.2,0.2,0.2,0.2,0.4]
    # horizon=4 → entry i=0 exit j=4
    net=_trade_net(closes,spreads,0,1,horizon=4)
    # Gross +1, RT cost 0.5*(0.2+0.4)=0.3 → 0.7 (NOT 1-0.2-0.4=0.4)
    assert net==pytest.approx(0.7)


def test_autonomy_forward_net_bps_uses_shared_rt():
    rows=[]
    t0=1_700_000_000.
    for i in range(9):
        rows.append({
            'label':0,'price':100.+i*0.1,
            'spread_bps':10. if i==0 else 20.,
            'time':__import__('datetime').datetime.fromtimestamp(
                       t0+60*i, __import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'),
        })
    net=AutoEvolution._forward_net_bps(rows,0,'BUY')
    # signed = 0.8/100*10000 = 80 bps; cost = 0.5*(10+20)=15 → 65
    assert net==pytest.approx(net_forward_bps('BUY',100.,100.8,
                                              spread_bps_entry=10.,spread_bps_exit=20.))
    assert net==pytest.approx(65.)


def test_path_counterfactual_differs_from_endpoint_delta():
    """BUY looks bad on end mid, but TP would have hit earlier on the path."""
    spread=0.2
    # Rise to TP zone then crash — endpoint favors SELL; path BUY takes TP.
    bars=[
        {'mid':100.,'high':100.1,'low':99.9,'spread':spread},
        {'mid':101.5,'high':102.2,'low':101.0,'spread':spread},  # TP for BUY (dist 2)
        {'mid':101.8,'high':102.0,'low':101.4,'spread':spread},
        {'mid':98.0,'high':99.0,'low':97.5,'spread':spread},     # crash
    ]
    endpoint=best_action_from_counterfactual(100.,98.,spread=spread)
    path=best_action_from_counterfactual(
        100.,98.,spread=spread,bars=bars,stop_distance=1.0,profit_distance=2.0,
        tick=0.1,trail=False)
    assert endpoint['method']=='endpoint'
    assert path['method']=='path'
    # Endpoint: BUY mid move -2 - 0.2 = -2.2; SELL +2 - 0.2 = +1.8 → SELL
    assert endpoint['best']=='SELL'
    # Path BUY should take TP near +2 (fill-embedded), beating SELL/HOLD.
    assert path['best']=='BUY'
    assert path['buy']>path['sell']
    assert path['buy']>0


def test_path_sim_hold_is_zero_and_sl_beats_tp_same_bar():
    hold=simulate_action_on_path('HOLD',[{'mid':100.,'high':100.,'low':100.}])
    assert hold['pnl']==0.
    # BUY: both SL and TP touchable — SL wins.
    bars=[{'mid':100.,'high':103.,'low':98.5,'spread':0.2}]
    sim=simulate_action_on_path('BUY',bars,spread=0.2,stop_distance=1.0,
                                profit_distance=2.0,tick=0.1,trail=False)
    assert sim['reason']=='sl'
    assert sim['pnl']<0


def test_loss_label_uses_path_opposite_not_endpoint():
    # Losing BUY; path says SELL would have won via SL/TP rules.
    bars=[
        {'mid':100.,'high':100.2,'low':99.8,'spread':0.2},
        {'mid':99.0,'high':99.2,'low':98.0,'spread':0.2},  # BUY SL; SELL TP-ish
        {'mid':98.5,'high':98.8,'low':98.0,'spread':0.2},
    ]
    d=label_from_profit('BUY',-1.5,entry=100.,exit_level=98.5,spread=0.2,
                        bars=bars,stop_distance=1.5,profit_distance=2.0,tick=0.1,
                        trail=False)
    assert d['simulated']['method']=='path'
    assert d['mode'].startswith('loss_counterfactual_')
    # Opposite SELL should beat HOLD on this down path.
    assert d['label']==1  # SELL


def test_endpoint_fallback_preserves_prior_loss_labels():
    # No bars → same behaviour as pre-#15 endpoint CF.
    d=label_from_profit('BUY',-1.0,entry=100,exit_level=99,spread=0.1)
    assert d['label']==1 and d['mode']=='loss_counterfactual_sell'
    assert d['simulated']['method']=='endpoint'
    inside=label_from_profit('BUY',-0.2,entry=100,exit_level=99.95,spread=0.3)
    assert inside['label']==2 and inside['mode']=='loss_counterfactual_hold'
