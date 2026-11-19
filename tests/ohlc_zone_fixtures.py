"""Helpers so LIVE OPEN tests put price inside the OHLC entry zona."""
from app import ohlc_entry_zones


def zone_midprice(candles, action='BUY', *, tick=0.01, spread=0.02):
    z = ohlc_entry_zones.compute_entry_zones(
        candles, tick=tick, spread=spread)
    zone = z.get('buy_entry') if action == 'BUY' else z.get('sell_entry')
    if not zone:
        return None
    return (float(zone['lo']) + float(zone['hi'])) / 2


def put_engine_in_entry_zone(engine, store, epic, action='BUY',
                             *, tick=0.01, spread=0.02, broker=None):
    """Set engine.market (+ optional broker.price) to the entry-zone mid."""
    candles = store.candles(epic, n=200, historical_only=False) or []
    # Drop forming minute the same way LIVE does.
    candles = ohlc_entry_zones.drop_forming_minute(candles)
    px = zone_midprice(candles, action, tick=tick, spread=spread)
    if px is None:
        return None
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
