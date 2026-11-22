"""Helpers so LIVE OPEN tests put price inside a confirmed OHLC entry zona."""
from app import ohlc_entry_zones
from tests.ohlc_zone_path import demand_zone_path, supply_zone_path


def zone_midprice(candles, action='BUY', *, tick=0.01, spread=0.02):
    z = ohlc_entry_zones.compute_entry_zones(
        candles, tick=tick, spread=spread)
    zone = z.get('buy_entry') if action == 'BUY' else z.get('sell_entry')
    if not zone:
        return None
    return (float(zone['lo']) + float(zone['hi'])) / 2


def seed_confirmed_zone_candles(store, epic='GOLD', action='BUY',
                                *, start=2000.0):
    """Write a base→impulse path with rejection confirm into the store."""
    if action == 'SELL':
        rows, mid = supply_zone_path(start=start, confirm=True)
    else:
        rows, mid = demand_zone_path(start=start, confirm=True)
    for row in rows:
        store.candle(epic, row, source='HISTORICAL')
    return rows, mid


def put_engine_in_entry_zone(engine, store, epic, action='BUY',
                             *, tick=0.01, spread=0.02, broker=None):
    """Set engine.market (+ optional broker.price) to the entry-zone mid.

    If current candles have no confirmed zone, reseeds a base→impulse path
    so LIVE gate can pass (1:1 confirm required).
    """
    candles = store.candles(epic, n=200, historical_only=False) or []
    candles = ohlc_entry_zones.drop_forming_minute(candles)
    px = zone_midprice(candles, action, tick=tick, spread=spread)
    gate_ok = False
    if px is not None:
        half = max(spread / 2, tick)
        ask = px + half
        bid = px - half
        g = ohlc_entry_zones.entry_allows_open(
            action, candles=candles, mid=px, ask=ask, bid=bid,
            tick=tick, spread=spread)
        gate_ok = bool(g.get('allowed'))
    if px is None or not gate_ok:
        rows, mid = seed_confirmed_zone_candles(store, epic, action)
        candles = ohlc_entry_zones.drop_forming_minute(rows)
        px = zone_midprice(candles, action, tick=tick, spread=spread)
        if px is None:
            px = mid
    half = max(spread / 2, tick)
    ask = px + half if action == 'BUY' else px + half
    bid = px - half
    mid = (ask + bid) / 2
    engine.market = {
        **(engine.market or {}),
        'epic': epic, 'mid': mid, 'bid': bid, 'ask': ask,
        'spread': ask - bid, 'tick': tick, 'status': 'TRADEABLE',
        'min_distance': (engine.market or {}).get('min_distance') or 0.15,
    }
    if broker is not None and hasattr(broker, 'price'):
        broker.price = mid
    return mid
