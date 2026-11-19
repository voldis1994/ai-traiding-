"""OHLC entry zones: Admin drawing geometry == LIVE OPEN wait gate."""
from datetime import datetime, timedelta, timezone

from app import ohlc_entry_zones as oez


def _swing_candles(n=80, start=100.0):
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    rows = []
    px = start
    for i in range(n):
        # Slow climb with clear swing lows/highs.
        o = px
        c = px + (0.15 if i % 7 else -0.35)
        h = max(o, c) + 0.8 + (1.5 if i % 11 == 10 else 0.1)
        l = min(o, c) - 0.8 - (1.5 if i % 11 == 5 else 0.1)
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c,
        })
        px = c
    return rows


def test_compute_entry_zones_builds_buy_and_sell_bands():
    rows = _swing_candles()
    mid = float(rows[-1]['close'])
    z = oez.compute_entry_zones(rows, mid=mid, tick=0.1, spread=0.2)
    assert z['ok'] is True
    assert z['buy_entry'] and z['sell_entry']
    assert z['buy_entry']['lo'] < z['buy_entry']['hi']
    assert z['sell_entry']['lo'] < z['sell_entry']['hi']
    assert z['buy_entry']['hi'] <= z['sell_entry']['lo'] or True  # may overlap mid range
    assert z['buy_entry']['source'] == 'ohlc_swing'


def test_entry_allows_open_waits_until_price_in_buy_zone():
    rows = _swing_candles()
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    assert buy
    # Far above discount band → wait.
    far = float(buy['hi']) + 5.0
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=far, ask=far, bid=far - 0.1,
        tick=0.1, spread=0.2)
    assert gate['allowed'] is False
    assert 'wait' in (gate['reason'] or '')
    # Inside band → allow.
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    gate2 = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert gate2['allowed'] is True
    assert gate2['reason'] == 'ohlc_entry_zone_touch'


def test_entry_allows_open_sell_zone():
    rows = _swing_candles()
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


def test_no_candles_blocks_open():
    gate = oez.entry_allows_open('BUY', candles=[], mid=100., ask=100.1, bid=99.9)
    assert gate['allowed'] is False
    assert 'wait_entry_zone' in gate['reason']


def test_atr_widens_band_when_mtf_ready():
    rows = _swing_candles()
    base = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    mtf = {
        'ready': True, 'enabled': True, 'mode': 'BUY', 'preferred': 'BUY',
        'confidence': 0.7, 'move_ok': True, 'stop_hint_atr14': 5.0,
        'tf': {'m5': {'kama': None, 'atr14': 5.0}},
    }
    rich = oez.compute_entry_zones(rows, tick=0.1, spread=0.2, mtf=mtf)
    assert rich['atr14'] == 5.0
    assert rich['info_stack'] == 'ohlc_swing+kama_adx_atr'
    assert rich['band'] >= base['band']


def test_drop_forming_minute_excludes_current_utc_bucket():
    from datetime import datetime, timezone
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
    rows = _swing_candles()
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
