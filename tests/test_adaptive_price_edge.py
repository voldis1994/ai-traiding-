"""Entry evidence: signal profitability proxy, not a static hit-rate requirement."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from app.autonomy import AutoEvolution
from app.engine import Store


def price_replay(action='SELL', *, wins=2, blocks=5, gain=4., loss=1., spread_bps=2.):
    """E.g. 2 profitable outcomes in 5; winners can outweigh losers."""
    rows=[]
    price=1000.
    origin=datetime(2026,1,5,tzinfo=timezone.utc)
    score={'BUY':.18,'SELL':.62,'HOLD':.20} if action=='SELL' else {
        'BUY':.62,'SELL':.18,'HOLD':.20}
    for block in range(blocks):
        positive=(block%5)<wins
        net=(gain if positive else -loss)
        if action=='SELL':net=-net
        label=(1 if action=='SELL' else 0) if positive else (0 if action=='SELL' else 1)
        for j in range(8):
            rows.append({'scores':score.copy(),'label':label,'price':price,
                         'spread_bps':spread_bps,
                         'time':(origin+timedelta(minutes=block*8+j)).isoformat()})
        price+=net
    for j in range(8):
        rows.append({'scores':{'BUY':.1,'SELL':.1,'HOLD':.8},'label':None,
                     'price':price,'spread_bps':spread_bps,
                     'time':(origin+timedelta(minutes=blocks*8+j)).isoformat()})
    return rows


def _policy(folder):
    return AutoEvolution(Store(folder/'db.sqlite3'),folder/'policy.py')


def test_forty_percent_wins_can_pass_when_after_spread_edge_positive():
    with tempfile.TemporaryDirectory() as d:
        p=_policy(Path(d))
        rows=price_replay('SELL',wins=2,blocks=100,gain=4.,loss=1.)
        verdict=p.evaluate_entry({'BUY':.18,'SELL':.62,'HOLD':.20},rows,
                                  accuracy=.4,samples=650,spread_bps=2.)
        assert verdict['action']=='SELL',verdict
        assert verdict['evidence']['precision']<.60
        assert verdict['evidence']['validation_lower_net_bps']>0
        assert verdict['evidence']['train_lower_net_bps']>0


def test_high_hit_rate_still_hold_if_avg_price_move_negative_after_spread():
    with tempfile.TemporaryDirectory() as d:
        p=_policy(Path(d))
        rows=price_replay('BUY',wins=4,blocks=80,gain=1.,loss=8.)
        verdict=p.evaluate_entry({'BUY':.62,'SELL':.18,'HOLD':.20},rows,
                                  accuracy=.8,samples=650,spread_bps=2.)
        assert verdict['action']=='HOLD',verdict
        assert verdict['evidence']['precision']>.60
        assert verdict['evidence']['validation_lower_net_bps']<0


def test_gaps_and_missing_prices_fail_closed_even_if_labels_good():
    with tempfile.TemporaryDirectory() as d:
        p=_policy(Path(d))
        rows=price_replay('BUY',wins=5,blocks=28)
        # Missing dates or every ninth bar: NOT eight consecutive minutes.
        for i,record in enumerate(rows):
            if i%8==3:record['time']=None
        verdict=p.evaluate_entry({'BUY':.62,'SELL':.18,'HOLD':.20},rows,
                                  accuracy=.9,samples=230,spread_bps=2.)
        assert verdict['action']=='HOLD'
        assert verdict['evidence']['samples']==0


def test_a_future_candle_does_not_count_as_a_matured_label():
    with tempfile.TemporaryDirectory() as d:
        p=_policy(Path(d))
        rows=price_replay('SELL',wins=5,blocks=30)
        for row in rows:row['label']=None
        verdict=p.evaluate_entry({'BUY':.18,'SELL':.62,'HOLD':.20},rows,
                                  accuracy=.95,samples=0,spread_bps=2.)
        assert verdict['action']=='HOLD'
        assert 'Waiting for delayed labels' in verdict['reason']
