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

    # Inside band but no rejection / structure confirm → still wait.
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    gate_touch = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert gate_touch['allowed'] is False
    assert 'confirm' in (gate_touch['reason'] or '')

    # With bullish rejection in zone → allow.
    rows_ok, mid = demand_zone_path(confirm=True)
    z2 = oez.compute_entry_zones(rows_ok, tick=0.1, spread=0.2)
    buy2 = z2['buy_entry']
    assert buy2
    inside2 = (float(buy2['lo']) + float(buy2['hi'])) / 2
    gate2 = oez.entry_allows_open(
        'BUY', candles=rows_ok, mid=inside2, ask=inside2, bid=inside2 - 0.1,
        tick=0.1, spread=0.2)
    assert gate2['allowed'] is True
    assert gate2['reason'] == 'ohlc_entry_zone_confirm'
    assert gate2.get('trigger') == 'zone'


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


def test_swing_anchor_sl_behind_demand_zone():
    from app.bracket_learn import swing_anchor_brackets
    rows, mid = demand_zone_path(confirm=True)
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    assert buy
    entry = (float(buy['lo']) + float(buy['hi'])) / 2
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.1, spread=0.2,
        min_distance=0.5, atr=1.0, entry_zone=buy)
    assert swing.get('ready') is True
    assert float(swing['stop_level']) < float(buy['lo']) + 1e-9
    assert swing.get('zone_sl') is True or float(swing['stop_level']) <= float(buy['lo'])
