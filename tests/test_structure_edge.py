"""Adaptive structure edge — soft nudge, opposite teach, trail self-arm."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import structure_edge as se
from app import structure_shadow as ss
from app.profit_learning import label_from_profit
from app.math_rules import levels_meet_broker_min
from app.protection_ai import propose_broker_trail


def _store(tmp):
    return Store(Path(tmp) / 't.sqlite3')


def _seed_side(store, *, epic='GOLD', structure='TREND_UP', direction='BUY',
               samples=8, total_pnl=4.0, wins=6, losses=2):
    lib = ss.load_library(store)
    key = ss.frame_key(epic=epic, structure=structure, direction=direction)
    buckets = dict(lib.get('buckets') or {})
    buckets[key] = {
        'samples': samples, 'total_pnl': total_pnl,
        'win_count': wins, 'loss_count': losses,
    }
    lib['buckets'] = buckets
    store.set(ss.STORE_KEY, lib)


def test_compare_frame_prefers_positive_side_not_auto_hold():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed_side(s, structure='TREND_UP', direction='BUY', total_pnl=5.0, samples=10)
        _seed_side(s, structure='TREND_UP', direction='SELL', total_pnl=-3.0, samples=10)
        out = se.compare_frame(s, epic='GOLD', structure='TREND_UP')
        assert out['advice'] == 'BUY'
        assert out['confidence'] > 0.4
        assert '−EV' in out['reason'] or '-EV' in out['reason'] or 'BUY' in out['reason']


def test_compare_frame_hold_only_when_both_sides_negative():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed_side(s, structure='BREAKOUT_UP', direction='BUY', total_pnl=-4.0, samples=8)
        _seed_side(s, structure='BREAKOUT_UP', direction='SELL', total_pnl=-2.0, samples=8)
        out = se.compare_frame(s, epic='GOLD', structure='BREAKOUT_UP')
        assert out['advice'] == 'HOLD'


def test_soft_nudge_tilts_scores_without_veto():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        advice = {
            'advice': 'SELL', 'confidence': 0.8,
            'reason': 'test', 'buy': {}, 'sell': {},
        }
        scores = {'BUY': 0.45, 'SELL': 0.40, 'HOLD': 0.15}
        nudged, meta = se.soft_nudge_scores(scores, advice, store=s)
        assert meta['nudged'] is True
        assert nudged['SELL'] > scores['SELL']
        assert nudged['BUY'] < scores['BUY']
        # Still a soft tilt — no action flip forced here.
        assert set(nudged) == {'BUY', 'SELL', 'HOLD'}
        # Critical: renormalize so autonomy._safe_scores does not reject → HOLD.
        assert abs(sum(nudged.values()) - 1.0) < 1e-9
        assert all(0 <= nudged[k] <= 1 for k in nudged)
        assert meta.get('renormalized') is True


def test_toxic_library_louder_nudge_teaches_correct_side():
    """TREND_DOWN×BUY grave + SELL pays → soft nudge must move vote toward SELL."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed_side(s, structure='TREND_DOWN', direction='BUY',
                   total_pnl=-40.0, samples=50, wins=0, losses=50)
        _seed_side(s, structure='TREND_DOWN', direction='SELL',
                   total_pnl=20.0, samples=40, wins=35, losses=5)
        advice = se.compare_frame(s, epic='GOLD', structure='TREND_DOWN')
        assert advice['advice'] == 'SELL'
        assert advice.get('toxic_opposite') == 'BUY'
        assert float(advice.get('nudge_scale') or 1) > 1.5
        # Strong neural BUY (wrong approach) — loud soft teach should flip argmax.
        scores = {'BUY': 0.70, 'SELL': 0.20, 'HOLD': 0.10}
        nudged, meta = se.soft_nudge_scores(scores, advice, store=s)
        assert meta['nudged'] is True
        assert nudged['SELL'] > nudged['BUY']
        # live_trend must NOT cancel protective teach against a toxic grave.
        nudged2, meta2 = se.soft_nudge_scores(
            scores, advice, store=s, live_trend='UP')
        assert meta2['nudged'] is True
        assert nudged2['SELL'] > nudged2['BUY']


def test_grave_only_side_damps_without_forced_hold_advice():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed_side(s, structure='BREAKOUT_DOWN', direction='BUY',
                   total_pnl=-12.0, samples=20, wins=2, losses=18)
        advice = se.compare_frame(s, epic='GOLD', structure='BREAKOUT_DOWN')
        assert advice.get('advice') is None
        assert advice.get('toxic_opposite') == 'BUY'
        scores = {'BUY': 0.55, 'SELL': 0.25, 'HOLD': 0.20}
        nudged, meta = se.soft_nudge_scores(scores, advice, store=s)
        assert meta['nudged'] is True
        assert nudged['BUY'] < scores['BUY']
        assert nudged['HOLD'] > scores['HOLD']


def test_soft_nudge_keeps_safe_scores_for_autonomy():
    """Regression: raw tilt sum>1 used to force neural HOLD via _safe_scores."""
    from app.autonomy import AutoEvolution
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        advice = {'advice': 'SELL', 'confidence': 0.8, 'reason': 'test'}
        scores = {'BUY': 0.45, 'SELL': 0.40, 'HOLD': 0.15}
        nudged, meta = se.soft_nudge_scores(scores, advice, store=s)
        assert meta['nudged']
        assert AutoEvolution._safe_scores(nudged)


def test_teach_after_loss_prefers_opposite_when_edge_says_so():
    taught, mode = se.teach_after_loss(
        direction='BUY', cf_best='HOLD', structure_prefer_hold=True,
        frame_advice={'advice': 'SELL', 'buy': {'mean_pnl': -1.0},
                      'sell': {'mean_pnl': 0.8}},
        opp_val=0.5, opp_floor=0.1)
    assert taught == 'SELL'
    assert 'opposite' in mode


def test_teach_after_loss_respects_spread_floor():
    taught, mode = se.teach_after_loss(
        direction='BUY', cf_best='HOLD',
        frame_advice={'advice': 'SELL', 'buy': {'mean_pnl': -1.0},
                      'sell': {'mean_pnl': 0.8}},
        opp_val=0.05, opp_floor=0.1)
    assert taught == 'HOLD'
    assert 'below_floor' in mode


def test_teach_after_loss_no_flip_without_cost_floor():
    """Missing opp_val/opp_floor must not flip on historical mean £ alone."""
    taught, mode = se.teach_after_loss(
        direction='BUY', cf_best='HOLD',
        frame_advice={'advice': 'SELL', 'buy': {'mean_pnl': -1.0},
                      'sell': {'mean_pnl': 2.0}},
        opp_val=None, opp_floor=None)
    assert taught == 'HOLD'
    assert 'no_floor' in mode


def test_teach_after_loss_cf_best_opposite_blocked_without_floor():
    """cf_best='SELL' after BUY loss must not teach SELL when opp_floor unknown."""
    taught, mode = se.teach_after_loss(
        direction='BUY', cf_best='SELL',
        frame_advice={'advice': 'SELL', 'buy': {'mean_pnl': -1.0},
                      'sell': {'mean_pnl': 2.0}},
        opp_val=None, opp_floor=None)
    assert taught == 'HOLD'
    assert taught != 'SELL'
    assert 'no_floor' in mode or 'no_cf_floor' in mode


def test_teach_after_loss_grave_tries_opposite_when_cf_clears():
    taught, mode = se.teach_after_loss(
        direction='BUY', cf_best='HOLD', structure_prefer_hold=True,
        frame_advice={'advice': None},
        opp_val=0.5, opp_floor=0.1)
    assert taught == 'SELL'
    assert 'try_opposite' in mode


def test_label_from_profit_uses_structure_frame_advice():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        _seed_side(s, structure='TREND_UP', direction='SELL', total_pnl=6.0, samples=10)
        _seed_side(s, structure='TREND_UP', direction='BUY', total_pnl=-5.0, samples=10)
        advice = se.compare_frame(s, epic='GOLD', structure='TREND_UP')
        assert advice['advice'] == 'SELL'
        # BUY loss; path must show opposite SELL clearing the spread floor so
        # structure advice may teach SELL (never bypass cost floor).
        out = label_from_profit(
            'BUY', -1.2, entry=100., exit_level=99.0, spread=0.2,
            structure_prefer_hold=True,
            structure_frame_advice=advice,
            bars=[
                {'open': 100., 'high': 100.2, 'low': 99.8, 'close': 100., 'spread': 0.2},
                # Strong adverse for BUY / favorable for SELL CF
                {'open': 100., 'high': 100.1, 'low': 97.5, 'close': 97.8, 'spread': 0.2},
            ],
            stop_distance=1.0, profit_distance=1.5, tick=0.01, min_distance=0.3)
        assert out['label'] == 1  # SELL
        assert 'structure' in str(out.get('structure_teach') or out.get('mode'))


def test_trail_arm_on_mfe_fraction():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        # Seed 1.0R + 0.45×TP — micro moves wait; real progress arms.
        weak = se.trail_arm_decision(
            s, epic='GOLD', structure='TREND_UP', direction='BUY',
            mfe=0.5, original_risk=0.8, original_profit=2.0, be_locked=False)
        assert weak['arm'] is False
        # 0.75 covers neither full 1R (0.8) nor 0.45×TP (0.9).
        mid = se.trail_arm_decision(
            s, epic='GOLD', structure='TREND_UP', direction='BUY',
            mfe=0.75, original_risk=0.8, original_profit=2.0, be_locked=False)
        assert mid['arm'] is False
        arm = se.trail_arm_decision(
            s, epic='GOLD', structure='TREND_UP', direction='BUY',
            mfe=0.95, original_risk=0.8, original_profit=2.0, be_locked=False)
        assert arm['arm'] is True
        assert 'mfe' in arm['reason']


def test_trail_arm_waits_without_evidence():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        arm = se.trail_arm_decision(
            s, epic='GOLD', structure='CHOP', direction='BUY',
            mfe=0.1, original_risk=0.8, original_profit=2.0, be_locked=False)
        assert arm['arm'] is False


def test_trail_stop_only_min_distance_when_tp_near_market():
    """TP inside minStop must not block a valid trail SL PUT."""
    # BUY: bid=101.35 ask=101.45 min=0.3; TP=101.6 is only 0.15 above ask → fails full check
    assert not levels_meet_broker_min(
        'BUY', 100.55, 101.6, bid=101.35, ask=101.45, min_distance=0.3)
    # Stop-only check (profit_level=None) still passes — trail may proceed.
    assert levels_meet_broker_min(
        'BUY', 100.55, None, bid=101.35, ask=101.45, min_distance=0.3)


def test_propose_early_arm_locks_be_plus_cushion():
    """First arm locks BE+ cushion — micro bounce must not scratch at exact entry."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        pos = {'dealId': 'a1', 'epic': 'GOLD', 'direction': 'BUY', 'level': 100.,
               'stopLevel': 99.2, 'profitLevel': 102.0, 'size': '.25'}
        # Need max(1.0×0.8, 0.45×2.0)=0.9 tip before first early-arm lock.
        market = {'epic': 'GOLD', 'bid': 100.95, 'ask': 101.05, 'spread': 0.1,
                  'tick': 0.01, 'min_distance': 0.3, 'status': 'TRADEABLE'}
        meta = {'owner': 'AUTO', 'deal_id': 'a1', 'original_risk': 0.8,
                'original_profit': 2.0}
        # Without arm: tip_fav < open_gap → no full-R trail yet.
        assert propose_broker_trail(pos, market, meta, extreme=100.7, store=s) is None
        # Micro tip still waits (below 1R / TP share).
        arm = {'arm': True, 'reason': 'mfe test', 'mfe': 0.5}
        assert propose_broker_trail(
            pos, market, meta, extreme=100.5, store=s, arm_decision=arm) is None
        arm = {'arm': True, 'reason': 'mfe test', 'mfe': 0.95}
        out = propose_broker_trail(
            pos, market, meta, extreme=100.95, store=s, arm_decision=arm)
        assert out is not None
        assert out['stop_level'] >= 100.0 - 1e-9  # never loss-side
        assert out['stop_level'] > 100.0 + 1e-9   # BE+ cushion, not exact BE
        assert out['profit_level'] == 102.0
        assert out.get('early_arm') is True


def test_propose_early_arm_skips_when_be_violates_min_distance():
    """If BE+ stop would sit inside broker min of market, skip — do not glue loss-side."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        pos = {'dealId': 'a1', 'epic': 'GOLD', 'direction': 'BUY', 'level': 100.,
               'stopLevel': 99.2, 'profitLevel': 102.0, 'size': '.25'}
        # tip enough for 1R+TP-share but bid barely above entry; minStop large
        market = {'epic': 'GOLD', 'bid': 100.2, 'ask': 100.3, 'spread': 0.1,
                  'tick': 0.01, 'min_distance': 0.5, 'status': 'TRADEABLE'}
        meta = {'owner': 'AUTO', 'deal_id': 'a1', 'original_risk': 0.8,
                'original_profit': 2.0}
        arm = {'arm': True, 'reason': 'mfe', 'mfe': 0.95}
        out = propose_broker_trail(
            pos, market, meta, extreme=100.95, store=s, arm_decision=arm)
        # Either None (skipped) or stop >= entry — never below entry.
        if out is not None:
            assert out['stop_level'] >= 100.0 - 1e-9
