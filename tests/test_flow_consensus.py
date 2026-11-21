"""M1 multi-source Order Flow + Liquidity + Impact consensus."""
from app import flow_consensus as fc


def _obs(*, vol_z=0.5, confirm=0.2, impulse=0.4, imbalance=0.35, micro=0.4,
         coverage=1.0, book=True, volume=True):
    availability = {
        'primary_volume': volume,
        'confirm_volume': True,
        'volume_impulse': True,
        'dom_book': book,
    }
    book_meta = {
        'available': book,
        'imbalance': imbalance,
        'micro_bps': micro,
        'source': 'coinbase',
        'instrument': 'PAXG-USD',
        'relation': 'substitute',
        'label': 'PAXG-USD Coinbase L2 (substitute for GOLD — not Capital book)',
    } if book else {'available': False, 'reason': 'dom_book unavailable'}
    return {
        'ok': True,
        'features': [vol_z, confirm, impulse, imbalance, micro, coverage],
        'availability': availability,
        'series_used': [{
            'source': 'yahoo_finance', 'instrument': 'GC=F',
            'relation': 'substitute', 'exchange': 'CME',
        }],
        'book': book_meta,
        'coverage': coverage,
        'target_bar': '2026-11-21T12:00:00+00:00',
    }


def test_buy_consensus_from_three_pillars():
    out = fc.evaluate(_obs(), proposed='BUY', epic='GOLD',
                      snapshot={'routes': {
                          'yahoo': ['GC=F', 'GLD'],
                          'coinbase': 'PAXG-USD',
                          'okx': 'PAXG-USDT',
                          'mapped': True,
                          'label': 'METAL_GOLD',
                      }, 'sources': ['yahoo:GC=F', 'coinbase:PAXG-USD']})
    assert out['ok']
    assert out['tempo'] == 'M1'
    assert out['consensus'] == 'BUY'
    assert out['advice'] == 'BUY'
    assert out['accept'] is True
    assert out['confidence'] >= 0.35
    votes = {p['id']: p['vote'] for p in out['pillars'] if p['ok']}
    assert votes['order_flow'] == 'BUY'
    assert votes['liquidity'] == 'BUY'
    assert votes['short_term_impact'] == 'BUY'
    assert out['providers']
    assert all(p.get('status') == 'active' for p in out['providers'])
    assert not any('dxfeed' in str(p.get('id') or '').lower() for p in out['providers'])
    assert not any('cme_comex' in str(p.get('id') or '').lower() for p in out['providers'])


def test_soft_nudge_damps_fight_against_consensus():
    cons = fc.evaluate(_obs(imbalance=-0.4, micro=-0.4, impulse=-0.35, vol_z=-0.4),
                       proposed='BUY', epic='GOLD')
    assert cons['consensus'] == 'SELL'
    assert cons['accept'] is False
    scores = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
    nudged, meta = fc.soft_nudge_scores(scores, cons)
    assert meta['nudged'] is True
    assert nudged['SELL'] > nudged['BUY']
    assert abs(sum(nudged.values()) - 1.0) < 1e-9


def test_missing_observation_is_soft_no_op():
    scores = {'BUY': 0.4, 'SELL': 0.4, 'HOLD': 0.2}
    nudged, meta = fc.soft_nudge_scores(scores, {'ok': False, 'reason': 'stale'})
    assert meta['nudged'] is False
    assert nudged == scores


def test_adaptive_system_includes_flow_stage():
    from app import adaptive_system as ads
    scores = {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2}
    nudged, meta = ads.nudge_open(
        None, scores, epic='GOLD', structure='TREND_UP', trend='UP')
    assert 'flow_consensus' in meta['stages']
    assert abs(sum(nudged.values()) - 1.0) < 1e-9
