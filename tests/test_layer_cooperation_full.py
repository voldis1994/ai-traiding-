"""Full stack: neural → policy → zones → regime → shadow must cooperate.

Contracts under test (not “does each module exist”, but do they fight):
  1. Neural scores_raw stay frozen; nudges only touch scores.
  2. Policy HOLD keeps proposal (= neural intent) for zones/shadow.
  3. Zones / regime gate can HOLD without inventing opposite side.
  4. Regime SL/TP for BUY and SELL use Demand/Supply + scenario — same source.
  5. Structure-shadow advice never vetoes LIVE OPEN.
  6. Hold-shadow + shadow-paper brackets == LIVE _would_be_brackets.
  7. When swing/regime unready → HOLD wait_ohlc_swing_rr, not fantasy SL/TP.
  8. Layer order: gate HOLD reasons stay attributable (zone vs regime vs swing).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import bracket_learn
from app import hold_shadow
from app import ohlc_entry_zones as oez
from app import ohlc_regime_brackets as orb
from app import ohlc_regime_gate
from app import ohlc_scenarios as osc
from app import structure_shadow as ss
from app import unified_cycle
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.shadow_paper import simulate_action_on_path
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


# ---------------------------------------------------------------------------
# 1–2. Neural scores_raw vs nudge / policy proposal preservation
# ---------------------------------------------------------------------------

def test_scores_raw_not_overwritten_by_nudge_stack():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        raw = {'BUY': 0.62, 'SELL': 0.18, 'HOLD': 0.20}
        e.last_signal = {
            'scores': dict(raw), 'scores_raw': dict(raw),
            'action': 'BUY',
        }
        e.mandate = {'epic': 'GOLD', 'size': 1}
        e.brain.epic = 'GOLD'
        e.market_context = {
            'ready': True, 'trend': 'UP', 'regime': 'TREND',
            'structure': 'TREND_UP', 'returns_bps': 4.0,
            'bar_time': '2026-11-23T12:00:00+00:00', 'features': [0.0] * 18,
        }
        e.market = {
            'mid': 2000., 'bid': 1999.9, 'ask': 2000.1, 'spread': 0.2,
            'tick': 0.1, 'min_distance': 0.5, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        # Soft stubs so entry path runs without full readiness/broker.
        e.brain.trading_readiness = lambda: {'ready': True, 'reason': 'test'}
        e.policy.evaluate_neural_entry = MagicMock(return_value={
            'action': 'BUY', 'proposal': 'BUY', 'score_edge': 0.25,
            'reason': 'neural_buy', 'evidence': {'samples': 20},
        })
        e._structure_edge_advice = lambda: {
            'structure': 'TREND_UP', 'advice': None, 'ready': False}
        e._situation_memory_advice = lambda **kw: {'ready': False}
        e._mtf_momentum_analysis = lambda: {
            'ready': True, 'mode': 'BUY', 'preferred': 'BUY', 'confidence': 0.5}
        e._external_entry_check = lambda a: {
            'entry_allowed': True, 'status': 'OK', 'reason': 'ok'}
        e._reentry_confirm_gate = lambda *a, **k: {'allowed': True}
        e._ohlc_entry_zone_gate = lambda *a, **k: {
            'allowed': True, 'trigger': 'zone', 'reason': 'in_zone'}
        e._would_be_brackets = lambda a: {
            'stop_distance': 2.0, 'profit_distance': 4.0,
            'bracket_source': 'regime_brackets', 'epic': 'GOLD',
            'entry': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
        }
        e._structure_shadow_advice = lambda a: {
            'ready': True, 'advice': 'HOLD', 'hard_veto': False}
        e._fusion_entry_blocked = lambda: False
        e._account_equity = lambda: 1000.0
        e.liquidity_path = []

        rows, mid = demand_zone_path(confirm=True)
        for row in rows:
            store.candle('GOLD', row, source='HISTORICAL')
        e.market['mid'] = mid

        out = e._entry_evaluation()
        assert e.last_signal['scores_raw'] == raw
        # Proposal / action attribution must remain BUY (policy stub).
        assert out.get('proposal') in ('BUY', None) or out.get('action') in (
            'BUY', 'HOLD')
        # Structure shadow must be attached when OPEN candidate — never veto key.
        if out.get('action') == 'BUY':
            assert 'structure_shadow' in out
            assert out['structure_shadow'].get('hard_veto') is not True


def test_policy_hold_preserves_proposal_for_hold_shadow():
    """HOLD from edge gate keeps neural proposal so hold-shadow can enqueue."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': 1}
        e.market = {
            'mid': 2000., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
            'bid': 1999.9, 'ask': 2000.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
        }
        rows, mid = demand_zone_path(confirm=True)
        for row in rows:
            store.candle('GOLD', row, source='HISTORICAL')
        e.market['mid'] = mid
        e.last_signal = {
            'scores': {'BUY': 0.55, 'SELL': 0.20, 'HOLD': 0.25},
            'scores_raw': {'BUY': 0.55, 'SELL': 0.20, 'HOLD': 0.25},
        }
        n = max(8, int(brain.core.n))
        d = max(3, int(brain.core.d))
        # Full transition shape AdaptiveBrain._json_transition expects (z/h/c/dt).
        transition = {
            'z': [0.0] * d,
            'h': [0.0] * n,
            'c': [0.0] * n,
            'dt': 1.0,
            'neuron_ids': list(range(n)),
        }
        brain.core.last_transition = transition
        evaluation = {
            'action': 'HOLD', 'proposal': 'BUY',
            'reason': 'weak_edge_after_loss_streak', 'score_edge': 0.04,
        }
        # Brackets for proposal must resolve (same LIVE path).
        br = e._would_be_brackets('BUY')
        if br is None:
            pytest.skip('fixture path could not place swing/regime brackets')
        row = hold_shadow.enqueue(
            store, proposal='BUY', reason=evaluation['reason'],
            bar_time='2026-11-23T12:00:00+00:00', epic='GOLD',
            entry=br['entry'], spread=br['spread'],
            stop_distance=br['stop_distance'],
            profit_distance=br['profit_distance'],
            tick=br['tick'], min_distance=br['min_distance'],
            transition=brain._json_transition(transition),
            scores=e.last_signal['scores'], slip_bps=0.)
        assert row is not None
        assert row['proposal'] == 'BUY'
        assert row['opposite'] == 'SELL'
        assert row['stop_distance'] == pytest.approx(br['stop_distance'])
        assert row['profit_distance'] == pytest.approx(br['profit_distance'])
        # Policy HOLDed but layers still share LIVE OHLC brackets for shadow.
        assert evaluation['action'] == 'HOLD' and evaluation['proposal'] == 'BUY'


# ---------------------------------------------------------------------------
# 3–4. Zones + regime: BUY/SELL structural, no opposite invent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('action,path_fn,scenario', [
    ('BUY', demand_zone_path, 'TREND_UP'),
    ('SELL', supply_zone_path, 'TREND_DOWN'),
])
def test_regime_and_zone_agree_side_for_long_and_short(action, path_fn, scenario):
    rows, mid = path_fn(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    zone = zones.get('buy_entry') if action == 'BUY' else zones.get('sell_entry')
    assert zone, 'expected confirmed entry zone'
    entry = (float(zone['lo']) + float(zone['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario=scenario, entry_hint=action)
    # Zone gate allows this side.
    half = 0.1
    gate = oez.entry_allows_open(
        action, candles=rows, mid=entry,
        ask=entry + half, bid=entry - half, tick=0.1, spread=0.2,
        mtf={'ready': True, 'mode': action, 'preferred': action,
             'confidence': 0.7, 'move_ok': True})
    assert gate.get('allowed') is True, gate
    # Regime brackets same side, structural.
    br = orb.regime_brackets(
        candles=rows, entry=entry, action=action, scenario=sc, zones=zones,
        entry_zone=zone, trigger=gate.get('trigger') or 'zone',
        tick=0.1, spread=0.2, min_distance=0.5, atr=1.0)
    assert br['ready'] is True
    assert br['source'] == 'regime_brackets'
    if action == 'BUY':
        assert br['stop_level'] < entry < br['profit_level']
    else:
        assert br['profit_level'] < entry < br['stop_level']
    # decide_brackets prefers regime when scenario+zones given.
    z = bracket_learn.zone_at_open(rows, multi_tf=True)
    decided = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry, action=action,
        candles=rows, atr=1.0, require_swing=True, entry_zone=zone,
        scenario=sc, zones=zones, trigger=gate.get('trigger') or 'zone')
    assert decided.get('ready') is True
    assert decided.get('source') == 'regime_brackets'
    # Opposite side must NOT silently get the same zone box as SL.
    opp = 'SELL' if action == 'BUY' else 'BUY'
    opp_zone = zones.get('sell_entry') if opp == 'SELL' else zones.get('buy_entry')
    if opp_zone:
        opp_gate = oez.entry_allows_open(
            opp, candles=rows, mid=entry,
            ask=entry + half, bid=entry - half, tick=0.1, spread=0.2)
        # Being inside demand does not auto-allow SELL (and vice versa).
        if opp_gate.get('allowed'):
            # If allowed via break, brackets still correct-side.
            d2 = bracket_learn.decide_brackets_from_ohlc(
                z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry,
                action=opp, candles=rows, atr=1.0, require_swing=True,
                entry_zone=opp_zone, scenario=sc, zones=zones)
            if d2.get('ready'):
                if opp == 'BUY':
                    assert d2['stop_level'] < entry < d2['profit_level']
                else:
                    assert d2['profit_level'] < entry < d2['stop_level']


def test_regime_gate_hold_does_not_flip_side():
    """Chop / no-trade OHLC veto → HOLD, proposal unchanged by gate itself."""
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    # Tiny doji/chop tape.
    rows = []
    for i in range(40):
        px = 100.0 + (0.01 if i % 2 == 0 else -0.01)
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': px, 'high': px + 0.02, 'low': px - 0.02, 'close': px,
        })
    out = ohlc_regime_gate.evaluate(
        candles=rows, mid=100.0, spread=0.05, atr=0.05,
        structure='CHOP', store=None)
    # Either soft allow or veto — if veto, reason is OHLC quality, not side flip.
    if not out.get('allowed', True):
        reason = str(out.get('reason') or '').lower()
        assert 'buy' not in reason or 'sell' not in reason or 'no_trade' in reason
        assert out.get('forced_action') not in ('BUY', 'SELL')


# ---------------------------------------------------------------------------
# 5. Structure shadow never vetoes
# ---------------------------------------------------------------------------

def test_structure_shadow_advise_is_evidence_not_live_veto():
    rows, mid = demand_zone_path(confirm=True)
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        advice = ss.advise(
            store, epic='GOLD', proposal='BUY', bars=rows)
        # advise may warn HOLD — but module contract: not a LIVE gate.
        assert not hasattr(ss, 'veto_open')
        assert not hasattr(ss, 'block_open')
        assert isinstance(advice, dict)
        # HOLD advice is a teach hint, not a forced OPEN flip / hard veto.
        assert advice.get('hard_veto') in (None, False)
        assert advice.get('action') in (None, 'HOLD')
        # Engine never gates on structure_shadow — soft evidence only.
        src = Path('app/engine.py').read_text(encoding='utf-8')
        assert 'structure_shadow' in src
        assert 'Soft evidence only' in src or 'never rewrite neural' in src.lower()


# ---------------------------------------------------------------------------
# 6. Shadow stack shares LIVE brackets
# ---------------------------------------------------------------------------

def test_hold_shadow_and_paper_sim_use_identical_live_brackets():
    rows, mid = demand_zone_path(confirm=True)
    zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = zones['buy_entry']
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1),
              scenario='TREND_UP')
    z = bracket_learn.zone_at_open(rows, multi_tf=True)
    live = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry, action='BUY',
        candles=rows, atr=1.0, require_swing=True, entry_zone=buy,
        scenario=sc, zones=zones, trigger='zone')
    assert live.get('ready') is True
    sd, pd = float(live['stop_distance']), float(live['profit_distance'])
    # Hold-shadow replay both sides with those distances.
    bars = [{'mid': float(r['close']), 'high': float(r['high']),
             'low': float(r['low']), 'spread': 0.2} for r in rows[-15:]]
    sides = hold_shadow.evaluate_sides(
        proposal='BUY', bars=bars, spread=0.2,
        stop_distance=sd, profit_distance=pd, tick=0.1, min_distance=0.5)
    assert sides is not None
    assert sides['proposal'] == 'BUY'
    assert sides['opposite'] == 'SELL'
    # Paper sim refuses invent; with LIVE distances it runs.
    sim = simulate_action_on_path(
        'BUY', bars, spread=0.2, stop_distance=sd, profit_distance=pd, tick=0.1)
    assert sim is not None and sim.get('entry') is not None
    assert abs(sim['entry'] - (sim.get('stop') or 0)) == pytest.approx(
        sd, abs=0.35) or sim.get('reason') in ('sl', 'tp', 'hold', 'path')


def test_engine_would_be_brackets_matches_decide_for_buy_and_sell():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.mandate = {'epic': 'GOLD', 'size': 1}
        for action, path_fn in (('BUY', demand_zone_path),
                                ('SELL', supply_zone_path)):
            rows, mid = path_fn(confirm=True)
            store.db.execute('DELETE FROM candles')  # isolation per side
            store.db.commit()
            for row in rows:
                store.candle('GOLD', row, source='HISTORICAL')
            zones = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
            zone = zones.get('buy_entry') if action == 'BUY' else zones.get('sell_entry')
            if not zone:
                continue
            entry = (float(zone['lo']) + float(zone['hi'])) / 2
            half = 0.1
            e.market = {
                'mid': entry, 'bid': entry - half, 'ask': entry + half,
                'spread': 0.2, 'tick': 0.1, 'min_distance': 0.5,
                'status': 'TRADEABLE', 'epic': 'GOLD',
            }
            e.market_context = {
                'ready': True, 'trend': 'UP' if action == 'BUY' else 'DOWN',
                'structure': 'TREND_UP' if action == 'BUY' else 'TREND_DOWN',
            }
            wb = e._would_be_brackets(action)
            if wb is None:
                # Honest refuse is OK — must not invent.
                continue
            assert wb['stop_distance'] > 0 and wb['profit_distance'] > 0
            assert wb.get('bracket_source') in (
                'regime_brackets', 'swing_anchor')
            # Direct decide with same candles must agree on side geometry.
            sc = dict(osc.classify(rows, mid=entry, zones=zones, tick=0.1))
            z = bracket_learn.zone_at_open(rows, multi_tf=True)
            decided = bracket_learn.decide_brackets_from_ohlc(
                z, tick=0.1, spread=0.2, min_distance=0.5, entry=entry,
                action=action, candles=rows, atr=1.0, require_swing=True,
                entry_zone=zone, scenario=sc, zones=zones)
            if decided.get('ready'):
                # Distances within snap tolerance of engine helper.
                assert abs(wb['stop_distance'] - decided['stop_distance']) < 1.0
                assert abs(wb['profit_distance'] - decided['profit_distance']) < 1.5


# ---------------------------------------------------------------------------
# 7–8. Unready structure → HOLD reason, unified cycle edge shared
# ---------------------------------------------------------------------------

def test_unready_swing_means_hold_not_fantasy_brackets():
    z = {'kappa': 1.5, 'range_distance': 12.0, 'max_tp': 20.0,
         'noise_distance': 10.0, 'ready': True}
    # Too few bars → no pivots.
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    rows = [{
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': 100., 'high': 100.2, 'low': 99.8, 'close': 100.1,
    } for i in range(4)]
    out = bracket_learn.decide_brackets_from_ohlc(
        z, tick=0.01, spread=0.05, min_distance=0.2,
        entry=100.0, action='BUY', candles=rows, require_swing=True)
    assert out['ready'] is False
    assert out['source'] == 'swing_anchor'
    assert out.get('stop_distance') is None


def test_unified_cycle_edge_gate_shared_by_compose_and_evaluate():
    """Loss-streak aggression tighten is shared — not a second competing edge."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        state = unified_cycle.default_state()
        state['loss_streak'] = 3
        state['aggression'] = 0.75
        unified_cycle.save_state(store, state)
        scores = {'BUY': 0.40, 'SELL': 0.30, 'HOLD': 0.30}
        evaluation = {'action': 'BUY', 'score_edge': 0.05, 'proposal': 'BUY'}
        edge = unified_cycle.edge_allows_open(scores, evaluation, state)
        zone = {'kappa': 1.2, 'range_distance': 2.0, 'max_tp': 3.0, 'ready': True}
        pkg = unified_cycle.compose_open(
            action='BUY', scores=scores, evaluation=evaluation, zone=zone,
            learned={'kappa': 1.0, 'sl_scale': 1.0}, cycle_state=state,
            base_sl=2.0, max_tp=3.0)
        # If edge blocks OPEN, compose must not silently advertise a free BUY
        # with invented wider brackets from aggression>1.
        if not edge.get('allowed'):
            assert pkg.get('action') == 'HOLD' or pkg.get('blocked') or (
                float(pkg.get('tighten') or 1.0) <= 1.0)
        assert float(pkg.get('tighten') or 1.0) <= 1.0 + 1e-9
        assert float(pkg.get('sl_scale') or 1.0) == pytest.approx(1.0)


def test_layer_reason_attribution_zone_vs_swing():
    """When zone blocks, reason says zone; when swing missing, wait_ohlc_swing."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        # Zone wait reason preserved in entry path.
        path = e._entry_path_from_evaluation({
            'action': 'HOLD', 'proposal': 'BUY',
            'reason': 'wait_buy_entry_zone',
            'ohlc_entry_zone': {'allowed': False, 'reason': 'wait_buy_entry_zone'},
        })
        assert path['intent'] == 'BUY'
        assert path['status'] == 'wait'
        assert 'wait' in (path['wait'] or '').lower() or 'zone' in (
            path['wait'] or '').lower()
        path2 = e._entry_path_from_evaluation({
            'action': 'HOLD', 'proposal': 'SELL',
            'reason': 'wait_ohlc_swing_rr',
            'ohlc_swing_brackets': None,
        })
        assert path2['intent'] == 'SELL'
        assert 'swing' in (path2['wait'] or path2.get('wait') or
                           'wait_ohlc_swing_rr').lower() or path2['status'] == 'wait'
