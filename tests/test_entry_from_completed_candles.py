"""Entry trigger must come from completed candle layout — not ask/bid alone."""
from app import ohlc_entry_zones as oez
from tests.ohlc_zone_path import demand_zone_path


def test_ask_bid_inside_zone_without_candle_touch_does_not_open():
    """Live ask in Demand is not enough if last completed bar never tagged it."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    # Push last completed bar clearly above the Demand box.
    tip = float(buy['hi']) + 5.0
    rows = list(rows)
    rows[-1] = {
        **rows[-1],
        'open': tip, 'high': tip + 0.5, 'low': tip - 0.2, 'close': tip,
        'mid': tip,
    }
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert gate.get('allowed') is False, gate
    assert 'candle' in (gate.get('reason') or '') or 'wait' in (
        gate.get('reason') or '')
    assert gate.get('decision_from') == 'completed_candle'


def test_candle_close_in_zone_opens_even_if_fill_quote_differs():
    """Structure close inside Demand unlocks; ask is fill metadata only."""
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    # Last bar closes inside the zone.
    rows = list(rows)
    rows[-1] = {
        **rows[-1],
        'open': inside - 0.1, 'high': inside + 0.2, 'low': inside - 0.3,
        'close': inside, 'mid': inside,
    }
    # Fill quote slightly away — must not veto candle proof.
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside,
        ask=inside + 0.05, bid=inside - 0.05,
        tick=0.1, spread=0.1)
    assert gate.get('allowed') is True, gate
    assert gate.get('trigger') == 'zone'
    assert gate.get('decision_from') == 'completed_candle'
    assert abs(float(gate.get('price')) - inside) < 1e-9


def test_break_live_mid_default_off():
    cfg = oez.load_params(None)
    assert int(cfg.get('break_live_mid') or 0) == 0


def test_engine_does_not_midbar_retry_zone_candle_waits():
    from app.engine import SingleTradeEngine

    class _E(SingleTradeEngine):
        def __init__(self):
            pass

    e = _E()
    assert e._entry_wait_reason_retryable(
        'wait_buy_entry_zone (candle 101 > zona 100)') is False
    assert e._entry_wait_reason_retryable('wait_mid_range') is False
    assert e._entry_wait_reason_retryable('wait_zone_confirm (pin)') is False
    assert e._entry_wait_reason_retryable('reentry_confirm_hold') is True
