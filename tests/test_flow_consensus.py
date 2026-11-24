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


def test_apply_to_decision_blocks_fight_with_majority():
    import tempfile
    from pathlib import Path
    from app.engine import Store
    from app import self_params as sp

    cons = fc.evaluate(
        _obs(imbalance=-0.4, micro=-0.4, impulse=-0.35, vol_z=-0.4),
        proposed='BUY', epic='GOLD')
    assert cons['agree'] >= 2
    scores = {'BUY': 0.70, 'SELL': 0.20, 'HOLD': 0.10}
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        store.set(sp.STORE_KEY, {
            **(store.get(sp.STORE_KEY) or {}),
            fc.STORE_BLOCK_KEY: 1,
        })
        dec = fc.apply_to_decision(
            scores, action='BUY', consensus=cons, store=store)
    assert dec['blocked'] is True
    assert dec['action'] == 'HOLD'
    assert dec['effect'] == 'blocked_fight'
    assert 'blocks BUY' in (dec['reason'] or '')
    assert dec['desk'].get('blocked') is True


def test_apply_to_decision_aligned_does_not_block():
    cons = fc.evaluate(_obs(), proposed='BUY', epic='GOLD')
    scores = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
    dec = fc.apply_to_decision(
        scores, action='BUY', consensus=cons, store=None)
    assert dec['blocked'] is False
    assert dec['action'] == 'BUY'
    assert dec['effect'] in ('aligned_boost', 'nudged')
    assert dec['nudged'] is True
    assert dec.get('scores_raw') == scores


def test_apply_to_decision_never_flips_buy_sell():
    """V3 L01: soft nudge must not change BUY↔SELL; scores_raw stay authority."""
    cons = fc.evaluate(
        _obs(imbalance=-0.4, micro=-0.4, impulse=-0.35, vol_z=-0.4),
        proposed='BUY', epic='GOLD')
    assert cons['consensus'] == 'SELL'
    # Disable hard block so nudge could previously flip via argmax.
    class _Store:
        def get(self, k, default=None):
            return default
        def set(self, k, v):
            pass
    # Force block_fights off via soft path: use consensus with agree=1 only.
    cons = dict(cons)
    cons['agree'] = 1
    scores = {'BUY': 0.40, 'SELL': 0.35, 'HOLD': 0.25}
    dec = fc.apply_to_decision(
        scores, action='BUY', consensus=cons, store=None)
    assert dec['effect'] != 'flipped_to_consensus'
    assert dec['action'] in ('BUY', 'HOLD')  # HOLD gate OK; never SELL flip
    if not dec.get('blocked'):
        assert dec['action'] == 'BUY'
    assert dec.get('scores_raw') == scores


def test_apply_to_decision_missing_feed_does_not_freeze():
    scores = {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1}
    dec = fc.apply_to_decision(
        scores, action='BUY', path=None, bar_time=None)
    assert dec['blocked'] is False
    assert dec['action'] == 'BUY'
    assert dec['effect'] == 'feed_unavailable'


def test_observe_close_marks_aligned_win_helpful_and_can_adapt():
    import tempfile
    from pathlib import Path
    from app.engine import Store
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        stamp = fc.open_stamp(
            {'effect': 'aligned_boost', 'nudged': True, 'blocked': False},
            {'consensus': 'BUY', 'accept': True, 'agree': 3, 'confidence': 0.7})
        for _ in range(4):
            out = fc.observe_close(
                store, flow_at_open=stamp, broker_pnl=0.4,
                direction='BUY', epic='GOLD')
            assert out['ok']
            assert out['sample']['helpful'] is True
        snap = fc.learn_snapshot(store)
        assert snap['helpful'] >= 4
        assert snap['nudge'] >= 0.18  # may strengthen after helpful edge


def test_pillar_thresholds_come_from_self_params():
    import tempfile
    from pathlib import Path
    from app.engine import Store
    from app import self_params
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        # Defaults would vote BUY on mild features; raise rails so they go quiet.
        p = self_params.load(store)
        p['flow_pillar_impulse'] = 0.35
        p['flow_pillar_vol_z'] = 0.45
        p['flow_pillar_imbalance'] = 0.40
        p['flow_pillar_impact'] = 0.35
        self_params.save(store, p, reason='test_pillar')
        mild = _obs(vol_z=0.22, impulse=0.20, imbalance=0.22, micro=0.20)
        out = fc.evaluate(mild, proposed='BUY', epic='GOLD', store=store)
        thr = out.get('pillar_thresholds') or {}
        assert thr.get('impulse') == 0.35
        assert thr.get('imbalance') == 0.40
        votes = {x['id']: x['vote'] for x in out['pillars'] if x['ok']}
        assert all(v == 'NEUTRAL' for v in votes.values()), votes
        # Same mild obs with default rails would still allow liquidity/impact votes.
        raw = fc.evaluate(mild, proposed='BUY', epic='GOLD', store=None)
        raw_votes = {x['id']: x['vote'] for x in raw['pillars'] if x['ok']}
        assert raw_votes['liquidity'] == 'BUY'
        assert raw_votes['short_term_impact'] == 'BUY'
        snap = fc.learn_snapshot(store)
        assert snap['pillar_thresholds']['impulse'] == 0.35


def test_block_resolve_marks_saved_loser():
    import tempfile
    from pathlib import Path
    from app.engine import Store
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        fc.observe_block(
            store, proposed='BUY', consensus_side='SELL', agree=3,
            mid=100.0, bar_time='2020-01-01T00:00:00+00:00', epic='GOLD')
        # Price fell → blocking BUY saved a loser.
        out = fc.maybe_resolve_blocks(
            store, candles=[{'close': 99.0}] * 8, mid=99.0, epic='GOLD')
        assert out['resolved'] == 1
        assert out['samples'][0]['saved'] is True
        assert out['samples'][0]['helpful'] is True
