"""1m playbook hard ENTRY checklist — AND confirm, session, regime SL."""
from datetime import datetime, timezone, timedelta

from app import playbook_entry as pb
from app import ohlc_regime_brackets as orb
from app import ohlc_entry_zones as oez


def test_session_window_london_ny():
    inside = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    assert pb.session_allows_open(now=inside, store=None)['allowed'] is True
    assert pb.session_allows_open(now=outside, store=None)['allowed'] is False


def test_impulse_from_level_detects_bull_leave():
    zone = {'lo': 100.0, 'hi': 101.0, 'side': 'BUY'}
    bar = {
        'open': 100.2, 'high': 102.0, 'low': 100.0, 'close': 101.8,
        'at': '2026-11-24T10:00:00+00:00',
    }
    out = pb.impulse_from_level('BUY', [bar], zone, tick=0.01)
    assert out['ok'] is True
    assert out['kind'] == 'impulse_bull'


def test_break_candle_extreme_and_sweep():
    rows = [
        {'open': 100, 'high': 101, 'low': 99.5, 'close': 100.5},
        {'open': 100.5, 'high': 102, 'low': 100.2, 'close': 101.8},
    ]
    assert pb.break_candle_extreme('BUY', rows) == 100.2
    assert pb.break_candle_extreme('SELL', rows) == 102.0
    assert pb.sweep_wick_extreme('BUY', rows) == 99.5
    assert pb.sweep_wick_extreme('SELL', rows) == 102.0


def test_regime_breakout_sl_uses_break_candle_low():
    # Flat path then impulsive break bar — SL should sit under that bar's low.
    rows = []
    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    for i in range(20):
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': 100.0, 'high': 100.3, 'low': 99.7, 'close': 100.0,
        })
    rows.append({
        'at': (t0 + timedelta(minutes=20)).isoformat(),
        'open': 100.0, 'high': 101.5, 'low': 99.9, 'close': 101.4,
    })
    out = orb.regime_brackets(
        candles=rows, entry=101.2, action='BUY',
        scenario={'scenario': 'BREAKOUT_UP',
                  'range': {'lo': 99.7, 'hi': 100.3}},
        trigger='break', tick=0.01, spread=0.02, min_distance=0.05)
    assert out.get('ready') is True
    # Break candle low 99.9 − buffer ≈ stop below entry.
    assert out['stop_level'] < 101.2
    assert abs(out['stop_level'] - (99.9 - out['buffer'])) < 1e-6


def test_zone_confirm_off_by_default():
    rows = [{'open': 100, 'high': 101, 'low': 99, 'close': 100.5,
             'at': '2026-11-24T10:00:00+00:00'}]
    zone = {'lo': 99.0, 'hi': 101.0, 'side': 'BUY'}
    conf = oez.zone_confirm_allows('BUY', candles=rows, zone=zone, tick=0.1)
    assert conf.get('confirm_required') is False
    assert conf['ok'] is True
    assert conf.get('reason') == 'confirm_skipped'
    assert pb.zone_confirm_required(None) is False


def test_zone_touch_allows_open_without_pin():
    """With confirm off, price inside Demand is enough for zone trigger."""
    from tests.ohlc_zone_path import demand_zone_path
    rows, _ = demand_zone_path(confirm=False)
    z = oez.compute_entry_zones(rows, tick=0.1, spread=0.2)
    buy = z['buy_entry']
    inside = (float(buy['lo']) + float(buy['hi'])) / 2
    gate = oez.entry_allows_open(
        'BUY', candles=rows, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert gate['allowed'] is True
    assert gate.get('trigger') == 'zone'
    assert (gate.get('confirm') or {}).get('reason') == 'confirm_skipped'
