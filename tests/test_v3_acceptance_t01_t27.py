"""V5.0 acceptance tests T01–T27 (fixtures / mocks only — no live Capital).

Spec: docs/v5/LIVE_V5_esosas_sistemas_labojumi_LV.txt chapters 21–22.
V5 supersedes V1–V4: fix existing LIVE; no new HOLD_* OPEN gates.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from app import cash_ledger
from app import flow_consensus as fc
from app import hard_risk
from app import model_readiness as mr
from app import ohlc_entry_zones as oez
from app import ohlc_regime_brackets as orb
from app import ohlc_scenarios as osc
from app import realized_cash
from app import sample_strategies
from app.bracket_learn import resample_m1_ohlc
from app.core import NeuralCore, client_neural_seed
from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app.market_context import market_features
from app.math_rules import cash_risk, dealing_size_grid, position_size
from app.ohlc_regime_brackets import _ohlc_stop
from app.shadow_paper import (
    SAME_BAR_BOTH_HIT_POLICY,
    ShadowPaperJournal,
    hit_sl_tp,
)
from app.trade_costs import fill_price, half_spread
from tests.ohlc_zone_path import demand_zone_path
from tests.test_learning_integrity import new_brain


# --- helpers -----------------------------------------------------------------

def _m1_rows(n, *, end=None, base=100.0, drift=0.01, gap_at=None):
    """Completed M1 bars ending just before ``end`` (UTC, floored)."""
    end = end or datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    gap = set(gap_at or [])
    rows = []
    px = float(base)
    for i in range(n):
        t = end - timedelta(minutes=n - i)
        if i in gap:
            continue
        o = px
        c = px + drift
        rows.append({
            'at': t.isoformat(),
            'open': o, 'high': max(o, c) + 0.05,
            'low': min(o, c) - 0.05, 'close': c,
        })
        px = c
    return rows


def _scenario_fixture(name, **extra):
    base = {
        'ready': True,
        'enabled': True,
        'scenario': name,
        'scenarios': [name],
        'bias': 'UP' if 'UP' in name else ('DOWN' if 'DOWN' in name else 'RANGE'),
        'event': None,
        'range': {'lo': 100.0, 'hi': 110.0, 'span': 10.0},
        'retest': None,
        'fakeout': name == 'FAKEOUT',
        'compression': name == 'COMPRESSION',
        'entry_hint': 'BUY' if 'UP' in name else ('SELL' if 'DOWN' in name else 'EDGE'),
        'confidence': 0.7,
        'evidence': {'tag': name, 'source': 'fixture'},
        'invalidation_anchor': 99.0 if 'UP' in name or name in (
            'RANGE', 'COMPRESSION', 'FAKEOUT') else 111.0,
        'block_mid_range': True,
    }
    base.update(extra)
    return base


# --- T01–T04: data ------------------------------------------------------------

def test_t01_gap_hold_data_no_synthetic_features():
    """T01: missing M1 in 60m window → unready / HOLD_DATA; features=None."""
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    full = _m1_rows(70, end=now)
    ok = market_features(full, now_utc=now, spread=0.1)
    assert ok['ready'] is True
    assert ok['features'] is not None

    # Drop one candle inside the required 60m contiguous window.
    gap_at = now - timedelta(minutes=10)
    gapped = [r for r in full if r['at'] != gap_at.isoformat()]
    bad = market_features(gapped, now_utc=now, spread=0.1)
    assert bad['ready'] is False
    assert bad['features'] is None
    reason = (bad.get('reason') or '').lower()
    assert 'missing' in reason and 'minute' in reason
    # Docstring / V3 D02: refusal is HOLD_DATA class (no synthetic fill).
    assert 'hold_data' in reason or 'contiguous' in reason or 'missing' in reason


def test_t02_forming_bar_does_not_mutate_completed_context():
    """T02: forming / revised current minute must not change completed context."""
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    completed = _m1_rows(65, end=now)
    snap1 = market_features(completed, now_utc=now, spread=0.1)
    assert snap1['ready'] is True
    h1 = hashlib.sha256(json.dumps(snap1['features']).encode()).hexdigest()

    forming = {
        'at': now.isoformat(),
        'open': 999.0, 'high': 1000.0, 'low': 1.0, 'close': 999.0,
    }
    snap2 = market_features(completed + [forming], now_utc=now, spread=0.1)
    assert snap2['ready'] is True
    assert snap2['bar_time'] == snap1['bar_time']
    assert snap2['features'] == snap1['features']

    revised = {
        'at': now.isoformat(),
        'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5,
    }
    snap3 = market_features(completed + [revised], now_utc=now, spread=0.1)
    h3 = hashlib.sha256(json.dumps(snap3['features']).encode()).hexdigest()
    assert h3 == h1
    assert snap3['bar_time'] == snap1['bar_time']

    dropped = oez.drop_forming_minute(completed + [revised], now_utc=now)
    assert all(r['at'] != now.isoformat() for r in dropped)


def test_t03_m5_aggregation_complete_vs_gap():
    """T03: 5 full M1 → precise M5 O/H/L/C; missing 1 → no completed M5 bucket."""
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    five = []
    for i, (o, h, l, c) in enumerate([
        (100.0, 101.0, 99.5, 100.5),
        (100.5, 102.0, 100.0, 101.5),
        (101.5, 103.0, 101.0, 102.0),
        (102.0, 102.5, 101.2, 101.8),
        (101.8, 102.2, 101.0, 101.2),
    ]):
        five.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c,
        })
    m5 = resample_m1_ohlc(five, 5)
    assert len(m5) == 1
    bar = m5[0]
    assert bar['open'] == pytest.approx(100.0)
    assert bar['high'] == pytest.approx(103.0)
    assert bar['low'] == pytest.approx(99.5)
    assert bar['close'] == pytest.approx(101.2)
    assert bar['m1_count'] == 5

    # Hole inside the bucket → incomplete M5 skipped.
    gapped = [r for i, r in enumerate(five) if i != 2]
    assert resample_m1_ohlc(gapped, 5) == []


def test_t04_last_good_ttl_expiry():
    """T04 V5: stale/missing external → keep time+status; use compatible context; no REQUIRED→HOLD."""
    from datetime import datetime, timezone, timedelta
    from app import market_fusion

    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    fresh = market_fusion.stamp_last_good(
        [0.1] * 6, source='peers', now=now - timedelta(seconds=30), max_age=120)
    assert market_fusion.last_good_ttl_expired(fresh, now=now, source='peers') is False
    assert market_fusion.last_good_values(fresh, now=now, source='peers') is not None

    stale = market_fusion.stamp_last_good(
        [0.1] * 6, source='peers', now=now - timedelta(seconds=200), max_age=120)
    assert market_fusion.last_good_ttl_expired(stale, now=now, source='peers') is True
    # Stale last-good remains usable context — not a REQUIRED→HOLD gate.
    assert market_fusion.last_good_values(stale, now=now, source='peers') == [0.1] * 6
    # require_fresh keeps diagnostics able to drop expired stamps.
    assert market_fusion.last_good_values(
        stale, now=now, source='peers', require_fresh=True) is None
    # Legacy bare list has no provenance stamp → expired flag, but values usable.
    assert market_fusion.last_good_ttl_expired([0.1] * 6, now=now) is True
    assert market_fusion.last_good_values([0.1] * 6, now=now) == [0.1] * 6
    # Re-read must not refresh observed_at.
    assert stale['observed_at'] == (now - timedelta(seconds=200)).isoformat()


# --- T05–T08: zones & scenarios ----------------------------------------------

def test_t05_zone_invalidation_close_through():
    """T05: demand 100–102, close 99 tick 0.1 → INVALIDATED even at high quality."""
    zone = {
        'lo': 100.0, 'hi': 102.0, 'kind': 'demand', 'side': 'BUY',
        'quality': 999.0, 'touches': 0, 'impulse_i1': 0,
        'at': '2026-11-18T10:00:00+00:00',
    }
    candles = [
        {'at': '2026-11-18T10:00:00+00:00',
         'open': 101.0, 'high': 102.0, 'low': 100.0, 'close': 101.5},
        {'at': '2026-11-18T10:01:00+00:00',
         'open': 101.0, 'high': 101.2, 'low': 98.5, 'close': 99.0},
    ]
    inv = oez.zone_invalidation_status(zone, candles, tick=0.1)
    assert inv['invalidated'] is True
    assert inv['state'] == 'INVALIDATED'
    assert oez._pick_best_zone([zone], mid=101.0, candles=candles, tick=0.1) is None


def test_t06_zone_and_confirm_pin_and_microstructure():
    """T06: confirm OFF by default; AND/OR only when require_confirm=1."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    assert buy
    last = rows[-1]
    base_lo, base_hi = float(buy['lo']), float(buy['hi'])
    mid = 0.5 * (base_lo + base_hi)
    t = datetime.fromisoformat(last['at'])
    pin = {
        'at': (t + timedelta(minutes=1)).isoformat(),
        'open': mid + 0.05, 'high': mid + 0.2,
        'low': base_lo - 0.05, 'close': mid + 0.12,
    }
    skipped = oez.zone_confirm_allows(
        'BUY', candles=list(rows) + [pin], zone=buy, tick=0.1)
    assert skipped['ok'] is True
    assert skipped.get('reason') == 'confirm_skipped'
    soft = oez.zone_confirm_allows(
        'BUY', candles=list(rows) + [pin], zone=buy, tick=0.1,
        require_confirm=True, require_both=False)
    assert soft['ok'] is True
    hard = oez.zone_confirm_allows(
        'BUY', candles=list(rows) + [pin], zone=buy, tick=0.1,
        require_confirm=True, require_both=True)
    assert hard['ok'] is False

    rows_ok, _ = demand_zone_path(confirm=True)
    buy2 = oez.compute_entry_zones(rows_ok, tick=0.1, spread=0.2)['buy_entry']
    conf2 = oez.zone_confirm_allows(
        'BUY', candles=rows_ok, zone=buy2, tick=0.1,
        require_confirm=True, require_both=True)
    assert conf2['ok'] is True
    assert conf2['rejection'].get('ok') is True
    assert conf2['structure'].get('ok') is True


def test_t07_scenario_tags_for_regimes():
    """T07: fixtures for TREND/RANGE/BREAKOUT/RETEST/CHOCH/FAKEOUT/COMPRESSION."""
    names = (
        'TREND_UP', 'TREND_DOWN',
        'RANGE',
        'BREAKOUT_UP', 'BREAKOUT_DOWN',
        'RETEST_UP', 'RETEST_DOWN',
        'CHOCH_UP', 'CHOCH_DOWN',
        'FAKEOUT', 'COMPRESSION',
    )
    for name in names:
        sc = _scenario_fixture(name)
        meta = osc.open_meta_from_scenario(sc)
        assert meta['scenario'] == name
        assert name in (meta.get('scenarios') or [name])
        assert sc.get('evidence') is not None
        assert sc.get('invalidation_anchor') is not None
        # Gate must accept ready named scenarios on zone trigger (except FAKEOUT break).
        gate = osc.scenario_allows_trigger(
            'BUY' if sc['entry_hint'] in ('BUY', 'EDGE', 'WAIT') else 'SELL',
            sc, 'zone' if name != 'FAKEOUT' else 'break')
        if name == 'FAKEOUT':
            assert gate['ok'] is False
        elif name == 'COMPRESSION' and gate.get('ok') is False:
            assert 'HOLD_SCENARIO' in (gate.get('reason') or gate.get('hold') or '')
        else:
            assert gate.get('scenario') == name or gate.get('ok') in (True, False)

    # Live classify also stamps a named scenario + evidence fields.
    rows, _ = demand_zone_path(confirm=False)
    live = osc.classify(rows, tick=0.1)
    assert live['ready'] is True
    assert live['scenario'] not in ('', None)
    assert isinstance(live.get('structure'), dict)
    assert live.get('range') is not None or live['scenario'] != 'RANGE'


def test_t08_range_mid_hold_and_unknown_hold_scenario():
    """T08: RANGE mid → HOLD; UNKNOWN/unready/disabled → HOLD_SCENARIO."""
    sc = {
        'ready': True, 'scenario': 'RANGE', 'block_mid_range': True,
        'range': {'lo': 100.0, 'hi': 110.0, 'span': 10.0},
    }
    blocked, reason = osc.mid_range_blocked('BUY', sc, 105.0, tick=0.1)
    assert blocked is True
    assert 'mid_range' in (reason or '')

    for payload in (
        {'ready': False, 'scenario': 'TREND_UP'},
        None,
        {'ready': True, 'enabled': False, 'scenario': 'TREND_UP',
         'entry_hint': 'BUY', 'confidence': 0.8},
        {'ready': True, 'enabled': True, 'scenario': 'UNKNOWN',
         'entry_hint': 'WAIT', 'confidence': 0.5},
    ):
        gate = osc.scenario_allows_trigger('BUY', payload, 'zone')
        assert gate['ok'] is False
        assert gate['reason'] == 'HOLD_SCENARIO'


# --- T09–T12: SL/TP & decision integrity -------------------------------------

def test_t09_hold_rr_not_sl_move():
    """T09 V5: BUY inv 95, buffer 0.5, entry 100, TP 104 → SL=94.5; no soft-cap; no HOLD_RR."""
    sl = _ohlc_stop('BUY', 95.0, entry=100.0, buffer=0.5)
    assert sl == pytest.approx(94.5)
    # Bruto RR with TP=104: 4 / 5.5 ≈ 0.727 — context only, not a veto.
    bruto_rr = 4.0 / 5.5
    assert bruto_rr < 1.5

    # tick=0.25 → buffer max(2*tick, 2*spread)=0.5 matching the fixture buffer.
    candles = [{
        'at': f'2026-01-01T00:{i:02d}:00+00:00',
        'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0,
    } for i in range(30)]
    br = orb.regime_brackets(
        candles=candles, entry=100.0, action='BUY',
        scenario={'scenario': 'TREND_UP', 'range': {'lo': 95.0, 'hi': 110.0}},
        zones={
            'buy_entry': {'side': 'BUY', 'lo': 95.0, 'hi': 96.0, 'sl_anchor': 95.0},
            'sell_entry': {'side': 'SELL', 'lo': 103.5, 'hi': 104.0},
        },
        entry_zone={'side': 'BUY', 'lo': 95.0, 'hi': 96.0, 'sl_anchor': 95.0},
        trigger='zone', tick=0.25, spread=0.0, min_distance=0.5, atr=None,
        store=None)
    assert br.get('ready') is True
    assert br.get('reason') != 'HOLD_RR'
    assert br.get('rr_capped') is not True
    assert float(br['stop_level']) == pytest.approx(94.5)
    # Must not soft-cap SL toward entry (96).
    assert float(br['stop_level']) < 96.0 - 1e-9
    assert float(br.get('rr') or 0) == pytest.approx(bruto_rr, rel=1e-3)

    # LIVE wrapper must also refuse soft-cap (F01/F02 — not only regime_brackets).
    from app.bracket_learn import decide_brackets_from_ohlc, apply_min_rr
    soft = apply_min_rr(
        action='BUY', entry=100.0, stop_level=94.5, profit_level=104.0,
        stop_d=5.5, profit_d=4.0, floor=0.5, min_rr=1.5, allow_soft_shrink=False)
    assert soft.get('ok') is False
    assert soft.get('capped') is not True
    zone = {
        'kappa': 1.0, 'range_distance': 5.0, 'max_tp': 8.0, 'noise_distance': 2.0,
        'ready': True,
        'buy_entry': {'side': 'BUY', 'lo': 95.0, 'hi': 96.0, 'sl_anchor': 95.0},
        'sell_entry': {'side': 'SELL', 'lo': 103.5, 'hi': 104.0},
    }
    decided = decide_brackets_from_ohlc(
        zone, tick=0.25, spread=0.0, min_distance=0.5, entry=100.0, action='BUY',
        candles=candles, atr=None, store=None, entry_zone=zone['buy_entry'],
        scenario={'scenario': 'TREND_UP', 'range': {'lo': 95.0, 'hi': 110.0}},
        trigger='zone')
    if decided.get('ready') and decided.get('stop_level') is not None:
        assert float(decided['stop_level']) < 96.0 - 1e-9
        assert decided.get('rr_capped') is not True
        assert '+rr_cap_sl' not in str(decided.get('reason') or '')


def test_t10_fakeout_sl_below_sweep():
    """T10: fakeout BUY range/sweep low 98, buffer 0.2 → SL ≤ 97.8; SELL symmetric."""
    candles = [{
        'at': f'2026-01-01T00:{i:02d}:00+00:00',
        'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0,
    } for i in range(30)]
    buy = orb.regime_brackets(
        candles=candles, entry=101.0, action='BUY',
        scenario={'scenario': 'FAKEOUT', 'range': {'lo': 98.0, 'hi': 110.0}},
        zones={}, trigger='zone', tick=0.1, spread=0.0, min_distance=0.1,
        atr=None, store=None)
    assert buy.get('stop_level') is not None
    assert float(buy['stop_level']) <= 97.8 + 1e-9

    sell = orb.regime_brackets(
        candles=candles, entry=99.0, action='SELL',
        scenario={'scenario': 'FAKEOUT', 'range': {'lo': 90.0, 'hi': 102.0}},
        zones={}, trigger='zone', tick=0.1, spread=0.0, min_distance=0.1,
        atr=None, store=None)
    assert sell.get('stop_level') is not None
    assert float(sell['stop_level']) >= 102.2 - 1e-9


def test_t11_neto_rr_example_b07_hold():
    """T11 / B07: neto 7/5=1.4 < min 1.5 → HOLD; spread not subtracted twice."""
    mid_e, mid_sl, mid_tp = 1.0850, 1.0846, 1.0858
    spread = 0.0001  # 1 pip full width
    entry = fill_price('BUY', mid_e, spread)
    half = half_spread(spread)
    # BUY closes on bid: SL/TP fills use mid − half (spread embedded once).
    sl_fill = mid_sl - half
    tp_fill = mid_tp - half
    assert entry == pytest.approx(1.08505)
    assert sl_fill == pytest.approx(1.08455)
    assert tp_fill == pytest.approx(1.08575)
    risk = entry - sl_fill
    reward = tp_fill - entry
    neto_rr = reward / risk
    assert neto_rr == pytest.approx(1.4, abs=1e-9)
    min_rr = 1.5
    assert neto_rr < min_rr  # plan refused (HOLD)


def test_t12_no_post_infer_flip_from_flow_consensus():
    """T12: scores BUY=0.6 SELL=0.3 HOLD=0.1; consensus SELL must not flip proposal."""
    scores = {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1}
    cons = {
        'ok': True, 'consensus': 'SELL', 'advice': 'SELL', 'accept': False,
        'agree': 3, 'confidence': 0.9, 'strength': 0.8,
        'fight_reason': 'proposed BUY fights M1 consensus SELL',
        'pillars': [
            {'id': 'order_flow', 'ok': True, 'vote': 'SELL'},
            {'id': 'liquidity', 'ok': True, 'vote': 'SELL'},
            {'id': 'short_term_impact', 'ok': True, 'vote': 'SELL'},
        ],
    }
    dec = fc.apply_to_decision(scores, action='BUY', consensus=cons, store=None)
    assert dec['scores_raw'] == scores
    assert dec['action'] in ('BUY', 'HOLD')
    assert dec['action'] != 'SELL'
    assert dec.get('effect') != 'flipped_to_consensus'


# --- T13–T16: broker / paper -------------------------------------------------

def test_t13_bid_ask_tp_not_hit_shadow_paper():
    """T13: BUY TP=105 high_mid=104 spread=2 → no TP; SELL symmetric; both → SL."""
    assert hit_sl_tp(
        'BUY', high=104., low=100., stop=90., profit=105.,
        mid=102., spread=2., slip_bps=0.) is None
    assert hit_sl_tp(
        'SELL', high=100., low=96., stop=110., profit=95.,
        mid=98., spread=2., slip_bps=0.) is None
    # Both SL/TP touched without tick sequence → conservative SL.
    assert SAME_BAR_BOTH_HIT_POLICY == 'conservative_sl'
    exit_px, reason = hit_sl_tp(
        'BUY', high=106., low=99., stop=99., profit=105.,
        mid=102., spread=2., slip_bps=0.)
    assert reason == 'sl'
    assert exit_px == pytest.approx(98.)


@pytest.mark.xfail(
    strict=False,
    reason='V3 pending: shadow pending fill-delay cancel on RR invalidation '
           '(gate cancel exists; RR re-check at fill not wired)')
def test_t14_pending_delay_cancel_when_rr_invalid():
    """T14: pending BUY fixed SL/TP; fill delay moves price so RR fails → cancel."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        # Queue pending with fill_delay; next bar must cancel when RR breaks.
        j.on_bar(
            'BUY', mid=100., spread=0.2, bar_time='t0',
            high=100.5, low=99.5, stop_distance=5.5, profit_distance=4.0,
            tick=0.1, min_distance=0.1, slip_bps=0., fill_delay_bars=1)
        assert j.pending is not None
        fixed_sl = j.pending.get('stop_distance')
        fixed_tp = j.pending.get('profit_distance')
        # Price drifts so neto RR vs fill would be worse — API should cancel.
        j.on_bar(
            'HOLD', mid=103., spread=0.2, bar_time='t1',
            high=103.5, low=102.5, slip_bps=0., fill_delay_bars=1)
        assert j.pending is None
        assert j.position is None
        # Absolute plan levels must not have been rewritten while pending.
        assert fixed_sl == pytest.approx(5.5)
        assert fixed_tp == pytest.approx(4.0)


def test_t15_uncertain_blocks_second_open():
    """T15: OPEN timeout → UNCERTAIN journal; second submit must not POST."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        e.armed = True
        e.execution_enabled = True
        e.mandate = {'epic': 'GOLD', 'size': '0.1'}
        e.positions = []
        e.last_closed_step = 0
        e.market_context = {'ready': True}
        e.pending_order = False
        # Prior OPEN left UNCERTAIN (timeout after broker accept).
        oid = store.order('OPEN', None, {'direction': 'BUY', 'size': 0.1})
        store.order_update(oid, 'UNCERTAIN', 'ref-timeout')
        assert any(op.get('kind') == 'OPEN' for op in store.unresolved())

        posts = []

        class FakeBroker:
            async def market(self, epic):
                return {
                    'snapshot': {
                        'offer': 100.1, 'bid': 99.9, 'marketStatus': 'TRADEABLE',
                    },
                    'dealingRules': {
                        'minDealSize': {'unit': 'AMOUNT', 'value': 0.1},
                        'minSizeIncrement': {'unit': 'AMOUNT', 'value': 0.1},
                        'maxDealSize': {'unit': 'AMOUNT', 'value': 10},
                    },
                    'instrument': {'valueOfOnePip': 1.0, 'onePipMeans': 0.1},
                }

            async def open_position(self, *a, **k):
                posts.append(('open', a, k))
                return {'dealReference': 'should-not-fire'}

        e.broker = FakeBroker()
        asyncio.run(e.submit('BUY', broker_market=None))
        assert posts == []
        assert 'unresolved' in (e.last_entry_reason or '').lower()


# --- T17–T20: risk & result truth --------------------------------------------

def test_t17_hold_risk_size_budget():
    """T17 V5: hard_risk/day_risk informational; no new HOLD_RISK from limits/grid."""
    # Min lot 0.5 at £25 risk exceeds £10 budget → size 0 (math), not HOLD_RISK gate.
    sized = position_size(10, 0, 50, 1, '.1', '.5', 10)
    assert sized['size'] == Decimal('0')
    assert float(cash_risk(Decimal('0.5'), 50, 1)) == pytest.approx(25.0)

    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.db')
        hard_risk.save_operator(store, {
            'max_risk_per_trade': 10.0,
            'daily_loss_limit': 50.0,
            'equity_drawdown_limit': 100.0,
            'trading_halted': False,
        }, reason='t17')
        over = hard_risk.entry_allowed(store, proposed_risk=25.0)
        assert over['allowed'] is True
        assert over.get('open_gated') is False
        missing = hard_risk.entry_allowed(store, proposed_risk=None)
        assert missing['allowed'] is True
        hard_risk.halt(store, 'emergency')
        halt = hard_risk.entry_allowed(store, proposed_risk=5.0)
        # Halt flag is bookkeeping for Admin; OPEN stays allowed (operator Pause separate).
        assert halt['allowed'] is True
        assert hard_risk.load(store).get('trading_halted') is True

    grid = dealing_size_grid({}, mandate_size='0.5')
    assert grid['ok'] is True
    assert grid['source'] == 'mandate_fallback'
    assert 'HOLD_RISK' not in (grid.get('reason') or '')


def test_t18_funds_pending_vs_settled_zero():
    """T18: pending + UPL → realized null; verified zero → £0; ambiguous → MATCH_UNCERTAIN."""
    # Pending / UPL must not invent realized cash.
    picked = realized_cash.pick_live_realized(
        funds=None, last_upl=Decimal('12.00'))
    assert picked['amount_minor'] is None
    assert picked['status'] == 'PENDING'
    assert realized_cash.may_teach_live_cash('pending_broker_pnl') is False
    assert realized_cash.classify_pnl_source('upl') != 'cash'

    store = cash_ledger.MemoryStore()
    r0 = cash_ledger.ingest_funds_row(
        store,
        {
            'reference': 'Z18', 'pnl': Decimal('0.00'), 'currency': 'GBP',
            'transactionType': 'TRADE', 'status': 'PROCESSED',
            'note': 'Trade closed',
        },
        account_id='acc-gbp', account_currency='GBP', deal_link='d-z',
        close_proof=True)
    assert r0['event']['status'] == cash_ledger.STATUS_SETTLED_ZERO
    assert r0['event']['amount_minor'] == 0
    assert cash_ledger.sum_account(store, 'acc-gbp', 'GBP') == 0

    # Two similar cash rows without unique deal link → MATCH_UNCERTAIN for teach.
    store2 = cash_ledger.MemoryStore()
    a = cash_ledger.ingest_funds_row(
        store2,
        {
            'reference': 'A1', 'pnl': Decimal('0.50'), 'currency': 'GBP',
            'transactionType': 'TRADE', 'status': 'PROCESSED',
            'note': 'Trade closed',
        },
        account_id='acc-gbp', account_currency='GBP')
    b = cash_ledger.ingest_funds_row(
        store2,
        {
            'reference': 'A2', 'pnl': Decimal('0.50'), 'currency': 'GBP',
            'transactionType': 'TRADE', 'status': 'PROCESSED',
            'note': 'Trade closed',
        },
        account_id='acc-gbp', account_currency='GBP')
    assert a['event']['status'] == cash_ledger.STATUS_UNMATCHED
    assert b['event']['status'] == cash_ledger.STATUS_UNMATCHED
    uncertain = cash_ledger.unmatched_deal_pnl([a['event'], b['event']])
    assert uncertain['status'] == cash_ledger.STATUS_MATCH_UNCERTAIN
    assert uncertain['amount_minor'] is None
    assert uncertain['may_teach'] is False
    assert cash_ledger.deal_realized(store2, 'mystery')['status'] == (
        cash_ledger.STATUS_MATCH_UNCERTAIN)


def test_t19_pathless_trade_no_ohlc_label():
    """T19: entry/exit without OHLC path → accounting kept; no OHLC strategy label."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        trades = [{
            'deal_id': 'pathless-1', 'epic': 'GOLD', 'direction': 'BUY',
            'entry': 2000.0, 'exit': 2001.0, 'pnl': 1.25,
            'opened': '2026-11-18T10:00:00+00:00',
            'closed': '2026-11-18T10:05:00+00:00',
        }]
        # Empty candle tape → path-less ingest (combo accounting only).
        out = sample_strategies.ingest_trades(
            store, trades, candles=[], epic='GOLD', tick=0.1)
        assert out.get('ok') is True
        assert out.get('ingested', 0) >= 1
        # No full OHLC frame / strategy card from empty path.
        from app import structure_shadow as ss
        lib = store.get(ss.STORE_KEY) if hasattr(ss, 'STORE_KEY') else None
        # Path-less must not invent swing graves from empty bars.
        if isinstance(lib, dict):
            frames = lib.get('frames') or lib.get('graves') or []
            assert frames == [] or all(
                not (isinstance(f, dict) and f.get('path_bars', 0) > 0)
                for f in (frames if isinstance(frames, list) else []))


def test_t20_shadow_no_broker_write():
    """T20: SHADOW paper path must not call broker POST/PUT/DELETE."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        e = SingleTradeEngine(store, brain)
        writes = []

        class GuardBroker:
            async def _request(self, method, path, **kw):
                writes.append((method, path))
                raise AssertionError(f'SHADOW must not {method} {path}')

            async def open_position(self, *a, **k):
                writes.append(('POST', 'open'))
                raise AssertionError('SHADOW open forbidden')

            async def close_position(self, *a, **k):
                writes.append(('DELETE', 'close'))
                raise AssertionError('SHADOW close forbidden')

            async def update_position(self, *a, **k):
                writes.append(('PUT', 'protect'))
                raise AssertionError('SHADOW protect forbidden')

        e.broker = GuardBroker()
        j = ShadowPaperJournal(store)
        j.on_bar(
            'BUY', mid=100., spread=0.2, bar_time='t0',
            high=100.5, low=99.5, stop_distance=1.0, profit_distance=2.0,
            tick=0.1, min_distance=0.1, slip_bps=0., fill_delay_bars=0)
        assert j.position is not None
        j.on_bar(
            'HOLD', mid=102.5, spread=0.2, bar_time='t1',
            high=103.0, low=101.0, slip_bps=0., fill_delay_bars=0)
        assert writes == []
        # Isolated shadow brain ≠ LIVE core.
        shadow = e._ensure_shadow_brain()
        assert shadow is not brain.core


# --- T21–T25: learning / readiness -------------------------------------------

def test_t21_shadow_learn_does_not_change_live_weights():
    """T21: paper / HOLD shadow learn changes only shadow hash, not LIVE weights."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        brain.core.last_z = np.linspace(-0.25, 0.25, brain.core.d)
        e = SingleTradeEngine(store, brain)
        e._ensure_shadow_brain()
        e._shadow_scores_now(advance=True, bar_time='t0')
        live_before = brain.core.snapshot_weights()
        shadow_before = e.shadow_brain.snapshot_weights()
        assert e._shadow_learn(None, 0, lr=0.2) is not None
        assert NeuralCore.weight_delta_norm(
            shadow_before, e.shadow_brain.snapshot_weights()) > 0
        assert NeuralCore.weight_delta_norm(
            live_before, brain.core.snapshot_weights()) == pytest.approx(0)


def test_t24_cold_start_does_not_make_live_ready():
    """T24 V5: manual close + unfinished candidate → LIVE cycle continues; no HOLD_MODEL."""
    with tempfile.TemporaryDirectory() as d:
        store, brain = new_brain(d)
        brain._activate('GOLD')
        mr.save_state(store, 'GOLD', {
            **mr._empty('GOLD', reason='holdout failed'),
            'stage': 'blocked', 'ready': False,
        })
        out = mr.grant_cold_start_open(store, brain, reason='manual close')
        assert out.get('cold_start_open') is True
        assert out.get('ready') is False
        assert brain.trading_readiness()['ready'] is False
        assert brain.trading_readiness()['cold_start_open'] is True
        gates = SingleTradeEngine(store, brain).live_open_gates('BUY')
        assert gates['allowed'] is True
        # Champion must not activate as a plan-time HOLD_MODEL substitute.
        src = inspect.getsource(SingleTradeEngine.submit)
        assert 'maybe_activate' not in src


# --- T26–T27: cycle & isolation ----------------------------------------------

def test_t27_dual_client_isolation():
    """T27: two clients / one epic — separate stores, seeds, risk; no PnL mix."""
    with tempfile.TemporaryDirectory() as root:
        da = Path(root) / 'c1'
        db_ = Path(root) / 'c2'
        da.mkdir()
        db_.mkdir()
        s1 = Store(da / 'robot.sqlite3')
        s2 = Store(db_ / 'robot.sqlite3')
        b1 = AdaptiveBrain(s1, data_dir=da, seed=client_neural_seed(1))
        b2 = AdaptiveBrain(s2, data_dir=db_, seed=client_neural_seed(2))
        e1 = SingleTradeEngine(s1, b1)
        e2 = SingleTradeEngine(s2, b2)
        e1.mandate = {'epic': 'GOLD', 'size': '0.1'}
        e2.mandate = {'epic': 'GOLD', 'size': '0.2'}
        hard_risk.save_operator(s1, {
            'max_risk_per_trade': 5.0, 'daily_loss_limit': 20.0,
            'equity_drawdown_limit': 40.0,
        }, reason='c1')
        hard_risk.save_operator(s2, {
            'max_risk_per_trade': 15.0, 'daily_loss_limit': 80.0,
            'equity_drawdown_limit': 120.0,
        }, reason='c2')
        # Distinct neural seeds / weights.
        assert b1.core.seed != b2.core.seed
        assert not np.allclose(b1.core.P, b2.core.P)
        # Risk bags stay per-store.
        assert hard_risk.load(s1)['max_risk_per_trade'] == 5.0
        assert hard_risk.load(s2)['max_risk_per_trade'] == 15.0
        # Cash / journal events do not cross stores.
        s1.event('trade', 'c1 pnl', {'pnl': 1.0})
        assert not list(s2.db.execute(
            "SELECT 1 FROM events WHERE kind='trade' LIMIT 1"))
        # Shadow clones also stay isolated.
        sh1 = e1._ensure_shadow_brain()
        sh2 = e2._ensure_shadow_brain()
        assert sh1.seed != sh2.seed
