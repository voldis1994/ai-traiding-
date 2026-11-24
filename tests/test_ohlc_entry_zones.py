"""OHLC entry zones: base→impulse Demand/Supply == LIVE OPEN wait gate."""
from datetime import datetime, timezone

from app import ohlc_entry_zones as oez
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


def test_compute_entry_zones_builds_demand_from_base_impulse():
    rows, mid = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    assert z['ok'] is True
    assert z['buy_entry']
    assert z['buy_entry']['kind'] == 'demand'
    assert z['buy_entry']['lo'] < z['buy_entry']['hi']
    assert 'base_impulse' in (z['buy_entry']['source'] or '')
    assert z['reason'] == 'base_impulse'
    # Structure labels present.
    assert z.get('structure') is not None


def test_compute_entry_zones_builds_supply_from_base_impulse():
    rows, mid = supply_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    assert z['ok'] is True
    assert z['sell_entry']
    assert z['sell_entry']['kind'] == 'supply'
    assert z['sell_entry']['lo'] < z['sell_entry']['hi']


def test_entry_allows_open_waits_until_confirm_in_demand_zone():
    """Confirm OFF: far → wait; inside Demand → allow without pin/structure."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    assert buy
    far = float(buy['hi']) + 5.0
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=far, ask=far, bid=far - 0.1,
        tick=0.1, spread=0.2)
    assert gate['allowed'] is False
    assert 'wait' in (gate['reason'] or '')

    # Inside band without pin/structure → wait (confirm ON by default).
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    gate_touch = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert gate_touch['allowed'] is False
    assert 'confirm' in (gate_touch.get('reason') or '') or 'wait' in (
        gate_touch.get('reason') or '')

    conf = oez.zone_confirm_allows(
        'BUY', candles=rows, zone=buy, tick=0.1)
    assert conf['ok'] is False
    assert conf.get('confirm_required') is True


def test_entry_allows_open_on_structure_break_without_zone():
    """Unified break trigger: structure UP + BUY intent → OPEN outside band."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    far = float(buy['hi']) + 5.0 if buy else float(rows[-1]['close']) + 5.0
    # Green tape so break is not blocked as against dump.
    t_last = rows[-1]
    rows = list(rows)
    for k in range(3):
        o = float(t_last['close']) + k * 0.4
        c = o + 0.5
        rows.append({
            'at': t_last['at'],  # timestamps not critical for tape streak
            'open': o, 'high': c + 0.1, 'low': o - 0.05, 'close': c,
        })
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.8, 'structure_break': 'UP', 'move_ok': True,
    }
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=far, ask=far, bid=far - 0.1,
        tick=0.1, spread=0.2, mtf=mtf)
    assert gate['allowed'] is True
    assert gate.get('trigger') == 'break'
    assert 'break' in (gate.get('reason') or '')


def test_break_refuses_buy_against_m1_dump():
    """Micro UP break must not BUY into a completed red dump."""
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    rows = []
    px = 3712.0
    for i in range(40):
        o = px
        c = px - 0.8
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': o + 0.2, 'low': c - 0.2, 'close': c,
        })
        px = c
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z.get('buy_entry')
    far = float(buy['hi']) + 3.0 if buy else float(rows[-1]['close']) + 5.0
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'HOLD', 'preferred': 'HOLD',
        'confidence': 0.4, 'structure_break': 'UP', 'move_ok': True,
    }
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=far, ask=far, bid=far - 0.1,
        tick=0.1, spread=0.2, mtf=mtf)
    assert gate['allowed'] is False
    reason = (gate.get('break') or {}).get('reason') or gate.get('reason') or ''
    assert 'against_m1_tape' in reason or 'wait' in reason


def test_entry_allows_open_sell_zone_with_confirm():
    rows, _ = supply_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    sell = z['sell_entry']
    assert sell
    below = float(sell['lo']) - 4.0
    gate = oez.entry_allows_open(
        'SELL', candles=rows, mid=below, ask=below + 0.1, bid=below,
        tick=0.1, spread=0.2)
    assert gate['allowed'] is False
    inside = (float(sell['lo']) + float(sell['hi'])) / 2
    gate2 = oez.entry_allows_open(
        'SELL', candles=rows, mid=inside, ask=inside + 0.1, bid=inside,
        tick=0.1, spread=0.2)
    assert gate2['allowed'] is True
    assert gate2.get('trigger') == 'zone'


def test_no_candles_blocks_open():
    gate = oez.entry_allows_open('BUY', candles=[], mid=100., ask=100.1, bid=99.9)
    assert gate['allowed'] is False
    assert 'wait_entry_zone' in gate['reason']


def test_label_swing_structure_hh_hl():
    from app import structure_memory
    rows, _ = demand_zone_path(confirm=False)
    st = structure_memory.label_swing_structure(rows, left=2, right=2)
    assert st['ready'] is True
    assert st['last_high_tag'] in ('HH', 'LH', 'SH')
    assert st['last_low_tag'] in ('HL', 'LL', 'SL')


def test_drop_forming_minute_excludes_current_utc_bucket():
    now = datetime(2026, 11, 19, 12, 34, 20, tzinfo=timezone.utc)
    rows = [
        {'at': '2026-11-19T12:32:00+00:00', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5},
        {'at': '2026-11-19T12:33:00+00:00', 'open': 1.5, 'high': 2, 'low': 1, 'close': 1.8},
        {'at': '2026-11-19T12:34:00+00:00', 'open': 1.8, 'high': 3, 'low': 1.7, 'close': 2.9},
    ]
    done = oez.drop_forming_minute(rows, now_utc=now)
    assert [c['at'] for c in done] == [
        '2026-11-19T12:32:00+00:00', '2026-11-19T12:33:00+00:00']


def test_mtf_conflict_blocks_open_even_in_zone():
    rows, _ = demand_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'SELL', 'preferred': 'SELL',
        'confidence': 0.8, 'move_ok': True, 'reason': 'kama_down_confirmed_edge_covers_costs',
        'stop_hint_atr14': 1.0,
        'tf': {'m5': {'kama': inside, 'atr14': 1.0}},
    }
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2, mtf=mtf)
    assert gate['allowed'] is False
    assert 'mtf' in (gate['reason'] or '')


def test_regime_sl_behind_demand_zone():
    """Guide: SL sits behind the Demand box (not invented mid±κ)."""
    from app import ohlc_regime_brackets as orb
    from app import ohlc_scenarios as osc
    rows, mid = demand_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    assert buy
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    sc = dict(osc.classify(rows, mid=entry, zones=z, tick=0.1),
              scenario='TREND_UP', entry_hint='BUY')
    br = orb.regime_brackets(
        candles=rows, entry=entry, action='BUY', scenario=sc, zones=z,
        entry_zone=buy, trigger='zone', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0)
    assert br.get('ready') is True
    assert float(br['stop_level']) < float(buy['lo']) + 1e-9
    assert br.get('zone_sl') is True or float(br['stop_level']) <= float(buy['lo'])


def test_z03_invalidated_demand_never_selected_even_high_quality():
    """V3 T05: close below demand lo−tick → INVALIDATED; not selected."""
    zone = {
        'lo': 100.0, 'hi': 102.0, 'kind': 'demand', 'side': 'BUY',
        'quality': 999.0, 'touches': 0, 'impulse_i1': 0,
        'at': '2026-11-18T10:00:00+00:00',
    }
    candles = [
        {'at': '2026-11-18T10:00:00+00:00',
         'open': 101.0, 'high': 102.0, 'low': 100.0, 'close': 101.5},
        {'at': '2026-11-18T10:01:00+00:00',
         'open': 101.0, 'high': 101.2, 'low': 98.5, 'close': 99.0},  # close 99, tick 0.1
    ]
    inv = oez.zone_invalidation_status(zone, candles, tick=0.1)
    assert inv['invalidated'] is True
    assert inv['state'] == 'INVALIDATED'
    assert inv['invalidation_reason'] == 'demand_close_below'

    picked = oez._pick_best_zone([zone], mid=101.0, candles=candles, tick=0.1)
    assert picked is None

    # Wick below lo without close-through is NOT invalidation.
    wick_only = [
        candles[0],
        {'at': '2026-11-18T10:01:00+00:00',
         'open': 101.0, 'high': 101.5, 'low': 99.0, 'close': 100.5},
    ]
    inv2 = oez.zone_invalidation_status(zone, wick_only, tick=0.1)
    assert inv2['invalidated'] is False


def test_z03_high_quality_invalidated_loses_to_valid_lower_quality():
    """Invalidated high-quality zone must not win over a valid weaker zone."""
    bad = {
        'lo': 100.0, 'hi': 102.0, 'kind': 'demand', 'side': 'BUY',
        'quality': 500.0, 'touches': 0, 'impulse_i1': 0,
        'at': 'a',
    }
    # Lower demand — close 99 invalidates 100–102 but not 90–92.
    good = {
        'lo': 90.0, 'hi': 92.0, 'kind': 'demand', 'side': 'BUY',
        'quality': 5.0, 'touches': 0, 'impulse_i1': 0,
        'at': 'b',
    }
    candles = [
        {'at': 't0', 'open': 101, 'high': 102, 'low': 100, 'close': 101},
        {'at': 't1', 'open': 101, 'high': 101, 'low': 98, 'close': 99},
    ]
    picked = oez._pick_best_zone(
        [bad, good], mid=91.0, candles=candles, tick=0.1)
    assert picked is not None
    assert float(picked['lo']) == 90.0
    assert picked.get('invalidated') is False


def test_z04_zone_confirm_requires_rejection_and_structure():
    """Confirm ON+AND by default: pin alone waits; full path allows."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    assert buy
    from datetime import datetime, timedelta
    last = rows[-1]
    base_lo, base_hi = float(buy['lo']), float(buy['hi'])
    mid = 0.5 * (base_lo + base_hi)
    ts = datetime.fromisoformat(last['at'])
    pin = {
        'at': (ts + timedelta(minutes=1)).isoformat(),
        'open': mid + 0.05, 'high': mid + 0.2,
        'low': base_lo - 0.05, 'close': mid + 0.12,
    }
    pin_only = list(rows) + [pin]
    # Default AND: pin alone waits for structure.
    conf_and = oez.zone_confirm_allows(
        'BUY', candles=pin_only, zone=buy, tick=0.1)
    assert conf_and['ok'] is False
    assert conf_and.get('confirm_required') is True
    assert conf_and['rejection'].get('ok') is True
    assert 'wait_structure' in (conf_and.get('reason') or '')
    # Soft OR still available when explicitly requested.
    conf_or = oez.zone_confirm_allows(
        'BUY', candles=pin_only, zone=buy, tick=0.1,
        require_confirm=True, require_both=False)
    assert conf_or['ok'] is True
    assert conf_or['rejection'].get('ok') is True

    rows_ok, _ = demand_zone_path(confirm=True)
    z2 = oez.compute_entry_zones(rows_ok, tick=0.1, spread=0.2)
    buy2 = z2['buy_entry']
    conf2 = oez.zone_confirm_allows(
        'BUY', candles=rows_ok, zone=buy2, tick=0.1)
    assert conf2['ok'] is True
    assert conf2['rejection'].get('ok') is True
    assert conf2['structure'].get('ok') is True


def test_z04_invalidated_zone_confirm_refused():
    zone = {
        'lo': 100.0, 'hi': 102.0, 'kind': 'demand', 'side': 'BUY',
        'state': 'INVALIDATED', 'invalidated': True, 'quality': 99.0,
    }
    conf = oez.zone_confirm_allows('BUY', candles=[], zone=zone, tick=0.1)
    assert conf['ok'] is False
    assert conf['reason'] == 'zone_invalidated'


def test_break_fires_on_one_green_m1_not_two():
    """Default tape_need=1 — structure break is not one extra M1 late."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    far = float(buy['hi']) + 5.0 if buy else float(rows[-1]['close']) + 5.0
    t_last = rows[-1]
    o = float(t_last['close'])
    c = o + 0.6
    rows = list(rows) + [{
        'at': t_last['at'], 'open': o, 'high': c + 0.1, 'low': o - 0.05, 'close': c,
    }]
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.55, 'structure_break': 'UP', 'move_ok': True,
    }
    br = oez.break_trigger_allows(
        'BUY', price=far, zones=z, mtf=mtf, tick=0.1, candles=rows)
    assert br['ok'] is True
    assert br['tape_need'] == 1
    assert br['tape_streak'] >= 1
    assert br['reason'] == 'ohlc_break_structure'


def test_range_break_live_mid_when_mtf_aligned():
    """Live mid through range_hi fires early — do not wait for completed close."""
    from datetime import timedelta
    t0 = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)
    rows = []
    px = 2000.0
    for i in range(30):
        o, c = px, px + 0.05
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': c + 0.1, 'low': o - 0.1, 'close': c,
        })
        px = c
    # Last completed close still inside range; live mid already clears hi.
    range_hi = 2005.0
    range_lo = 1998.0
    # Keep last close below hi.
    rows[-1] = dict(rows[-1], open=2003.5, high=2004.2, low=2003.0, close=2004.0)
    # One green tape bar for early proof.
    rows.append({
        'at': (t0 + timedelta(minutes=30)).isoformat(),
        'open': 2003.8, 'high': 2004.5, 'low': 2003.6, 'close': 2004.2,
    })
    zones = {
        'ok': True, 'range_hi': range_hi, 'range_lo': range_lo,
        'buy_entry': None, 'sell_entry': None,
    }
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.62, 'structure_break': None, 'move_ok': True,
    }
    # Close still below hi → old path would wait; live mid clears → early.
    live = range_hi + 0.3
    br = oez.break_trigger_allows(
        'BUY', price=live, zones=zones, mtf=mtf, tick=0.1, candles=rows)
    assert br['ok'] is True
    assert br['reason'] == 'ohlc_break_range_high_live'

    # Without tape + strong conf, live mid alone must not chase.
    weak = dict(mtf, confidence=0.2)
    red = list(rows[:-1]) + [{
        'at': (t0 + timedelta(minutes=30)).isoformat(),
        'open': 2004.5, 'high': 2004.6, 'low': 2003.0, 'close': 2003.2,
    }]
    br_no = oez.break_trigger_allows(
        'BUY', price=live, zones=zones, mtf=weak, tick=0.1, candles=red)
    assert br_no['ok'] is False
