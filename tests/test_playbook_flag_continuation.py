"""US Tech-style flag → dump: playbook must SEE and OPEN the break.

Regression for: tight consolidation (green circle) then one red impulse —
LIVE used to miss it via no_trade_impulse + COMPRESSION waiting for pivot BOS
+ impulse_len≥3 only.
"""
from app import ohlc_entry_zones as oez
from app import ohlc_regime_gate as rg
from app import ohlc_scenarios
from app import structure_memory as sm


def _bar(o, h, l, c, i=0):
    return {
        'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
        'mid': float(c), 'at': f'2026-11-25T07:{i:02d}:00Z',
    }


def _us_tech_flag_dump_path():
    """Downtrend → tight flag ~25640–25680 → single red dump through flag lo."""
    rows = []
    # Prior downtrend so structure bias can lean DOWN / ATR settles.
    px = 25880.0
    for i in range(20):
        o = px
        c = px - 12.0
        rows.append(_bar(o, o + 3, c - 2, c, i))
        px = c
    # Tight flag / compression (~40pt box).
    flag_lo, flag_hi = 25640.0, 25680.0
    mid = 0.5 * (flag_lo + flag_hi)
    for j in range(7):
        if j % 2 == 0:
            rows.append(_bar(mid, flag_hi - 2, mid - 5, mid + 8, 20 + j))
        else:
            rows.append(_bar(mid + 5, mid + 10, flag_lo + 2, mid - 6, 20 + j))
    # Single-bar impulse dump through the flag floor.
    dump_o = mid
    dump_c = 25599.0
    rows.append(_bar(dump_o, dump_o + 2, dump_c - 1, dump_c, 27))
    return rows, flag_lo, flag_hi, dump_c


def test_single_bar_impulse_stamps_supply_from_flag():
    rows, flag_lo, flag_hi, _ = _us_tech_flag_dump_path()
    det = sm.detect_base_impulse_zones(rows, atr=18.0)
    assert det.get('ready'), det.get('reason')
    supply = det.get('supply') or []
    assert supply, 'expected Supply = flag base before dump'
    # Freshest preferred Supply should overlap the circled flag (not only
    # older higher boxes from the prior leg down).
    best = supply[0]
    overlap_lo = max(float(best['lo']), flag_lo)
    overlap_hi = min(float(best['hi']), flag_hi)
    assert overlap_hi >= overlap_lo, (best, flag_lo, flag_hi)
    assert int(best.get('impulse_len') or 99) <= 4


def test_compression_break_allows_sell_on_flag_dump():
    rows, flag_lo, flag_hi, dump_c = _us_tech_flag_dump_path()
    # Scenario from bars *before* classifying with zones — use classify on path.
    sc = ohlc_scenarios.classify(rows, mid=dump_c, tick=0.1)
    # Force compression box to the flag if swing box is wider.
    sc = dict(sc)
    sc['compression'] = True
    sc['scenario'] = 'COMPRESSION'
    tags = list(sc.get('scenarios') or [])
    if 'COMPRESSION' not in tags:
        tags.append('COMPRESSION')
    sc['scenarios'] = tags
    sc['range'] = {
        'lo': flag_lo, 'hi': flag_hi, 'span': flag_hi - flag_lo,
        'atr': 18.0, 'inside_frac': 0.85, 'compression': True,
    }
    sc['ready'] = True
    sc['enabled'] = True
    cb = oez.compression_break_allows(
        'SELL', price=dump_c, scenario=sc, candles=rows, tick=0.1)
    assert cb.get('ok') is True, cb
    assert 'compression' in (cb.get('reason') or '')


def test_entry_allows_open_sell_on_flag_break():
    rows, flag_lo, flag_hi, dump_c = _us_tech_flag_dump_path()
    mtf = {
        'ready': True, 'preferred': 'SELL', 'mode': 'SELL',
        'confidence': 0.7, 'structure_break': None,
    }
    gate = oez.entry_allows_open(
        'SELL', candles=rows, mid=dump_c, ask=dump_c + 1.0, bid=dump_c,
        tick=0.1, spread=1.8, mtf=mtf)
    assert gate.get('allowed') is True, gate.get('reason')
    assert gate.get('trigger') == 'break'
    assert 'compression' in (gate.get('reason') or '')
    box = (gate.get('scenario') or {}).get('range') or {}
    assert float(box.get('lo') or 0) <= flag_hi
    assert float(box.get('hi') or 0) >= flag_lo


def test_impulse_regime_overridden_by_playbook_break():
    rows, _, _, dump_c = _us_tech_flag_dump_path()
    regime = rg.evaluate(candles=rows, mid=dump_c, spread=1.8, atr=18.0)
    assert regime.get('allowed') is False
    assert regime.get('reason') == 'no_trade_impulse'
    zone_gate = {
        'allowed': True, 'trigger': 'break',
        'reason': 'ohlc_break_compression_low',
    }
    assert rg.playbook_overrides_regime(regime, zone_gate) is True
    assert rg.playbook_overrides_regime(
        regime, {'allowed': False, 'trigger': 'break'}) is False
    assert rg.playbook_overrides_regime(
        regime, {'allowed': True, 'trigger': 'zone'}) is False
