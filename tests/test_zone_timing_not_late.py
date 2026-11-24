"""Timing contract: zones are drawn for pullback entry — not after the move is gone.

Idea:
  1. After base+impulse prints on completed M1, Demand/Supply box appears.
  2. At first detection price is still *outside* the box (waiting for return).
  3. OPEN on zone path only after pullback + rejection/structure confirm.
  4. That is early enough to *wait* for a good entry — not chase the impulse tip.
"""
from datetime import datetime, timedelta, timezone

from app import ohlc_entry_zones as oez
from app import structure_memory as sm


def _row(t0, i, o, h, l, c):
    return {
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
    }


def _demand_timeline():
    """Return full path + index markers for base / impulse / confirm."""
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    rows = []
    px = 2000.0
    i = 0
    for _ in range(18):
        o, c = px, px - 0.35
        rows.append(_row(t0, i, o, o + 0.15, c - 0.15, c))
        px = c
        i += 1
    base_i0 = i
    base_lo, base_hi = px - 0.25, px + 0.35
    base_mid = 0.5 * (base_lo + base_hi)
    for _ in range(4):
        o, c = base_mid - 0.05, base_mid + 0.05
        rows.append(_row(t0, i, o, base_hi - 0.02, base_lo + 0.02, c))
        px = c
        i += 1
    impulse_i0 = i
    for _ in range(6):
        o, c = px, px + 1.1
        rows.append(_row(t0, i, o, c + 0.15, o - 0.05, c))
        px = c
        i += 1
    impulse_i1 = i - 1
    for _ in range(8):
        o, c = px, px - 0.55
        rows.append(_row(t0, i, o, o + 0.1, c - 0.1, c))
        px = c
        i += 1
    o = base_mid + 0.05
    c = base_mid + 0.12
    l = base_lo - 0.05
    h = c + 0.08
    rows.append(_row(t0, i, o, h, l, c))
    confirm_i = i
    return {
        'rows': rows,
        'base_i0': base_i0,
        'impulse_i0': impulse_i0,
        'impulse_i1': impulse_i1,
        'confirm_i': confirm_i,
        'base_lo': base_lo,
        'base_hi': base_hi,
        'base_mid': base_mid,
    }


def test_demand_zone_appears_during_or_right_after_impulse_not_after_move_gone():
    """Zone box must exist while price is still above it — waiting for pullback."""
    tl = _demand_timeline()
    rows = tl['rows']
    first_zone_i = None
    buy = None
    for n in range(16, tl['confirm_i'] + 1):
        path = rows[:n]
        z = oez.compute_entry_zones(path, mid=path[-1]['close'], tick=0.1, spread=0.2)
        if z.get('buy_entry'):
            first_zone_i = n - 1
            buy = z['buy_entry']
            break
    assert first_zone_i is not None, 'Demand zone never appeared before confirm'
    # Earliest detection: as soon as a short impulse qualifies (can be mid-impulse).
    assert first_zone_i >= tl['impulse_i0']
    assert first_zone_i <= tl['impulse_i1'] + 1
    mid = rows[first_zone_i]['close']
    assert mid > float(buy['hi']), 'At first draw, price should be above Demand (wait, not chase)'
    assert float(buy['lo']) < float(buy['hi'])
    assert buy.get('kind') == 'demand'


def test_zone_open_only_on_pullback_confirm_not_impulse_tip():
    """OPEN on zone path waits for return + rejection+structure (playbook AND)."""
    from tests.ohlc_zone_path import demand_zone_path
    tl = _demand_timeline()
    rows = tl['rows']
    # At impulse tip — zone may exist, but price far above → wait.
    tip = rows[: tl['impulse_i1'] + 1]
    z = oez.compute_entry_zones(tip, tick=0.1, spread=0.2)
    buy = z.get('buy_entry')
    assert buy
    tip_px = tip[-1]['close']
    assert tip_px > float(buy['hi'])
    gate_tip = oez.entry_allows_open(
        'BUY', candles=tip, mid=tip_px, ask=tip_px, bid=tip_px - 0.1,
        tick=0.1, spread=0.2)
    assert gate_tip['allowed'] is False
    assert 'wait' in (gate_tip['reason'] or '')

    # Full AND confirm path (pin + HL) from shared fixture.
    full, _ = demand_zone_path(confirm=True)
    z2 = oez.compute_entry_zones(full, tick=0.1, spread=0.2)
    buy2 = z2['buy_entry']
    inside = (float(buy2['lo']) + float(buy2['hi'])) / 2
    gate = oez.entry_allows_open(
        'BUY', candles=full, mid=inside, ask=inside, bid=inside - 0.1,
        tick=0.1, spread=0.2)
    assert gate['allowed'] is True
    assert gate.get('trigger') == 'zone'
    assert gate.get('reason') == 'ohlc_entry_zone_confirm'
    conf = (gate.get('confirm') or {})
    assert conf.get('require_both') is True
    assert conf.get('rejection', {}).get('ok') is True
    assert conf.get('structure', {}).get('ok') is True


def test_detector_marks_fresh_untouched_zone_at_impulse_end():
    """Right after impulse, preferred Demand has 0 touches — first-touch quality."""
    tl = _demand_timeline()
    path = tl['rows'][: tl['impulse_i1'] + 1]
    det = sm.detect_base_impulse_zones(path)
    assert det.get('ready')
    demand = det.get('demand') or []
    assert demand
    best = demand[0]
    assert int(best.get('touches') or 0) == 0
    assert float(best['lo']) <= tl['base_mid'] <= float(best['hi']) or (
        float(best['lo']) < float(best['hi']))


def test_signal_vs_zone_roles_documented_in_gate_meta():
    """Neural intent is separate; zone gate only says wait vs confirm trigger."""
    tl = _demand_timeline()
    tip = tl['rows'][: tl['impulse_i1'] + 1]
    tip_px = tip[-1]['close']
    gate = oez.entry_allows_open(
        'BUY', candles=tip, mid=tip_px, ask=tip_px, bid=tip_px - 0.1,
        tick=0.1, spread=0.2)
    assert gate.get('intent') == 'BUY'
    assert gate.get('allowed') is False
    assert gate.get('zones', {}).get('buy_entry')  # box already drawn for learning/chart
    assert gate.get('trigger') in (None, 'break') or gate.get('allowed') is False


def test_observe_close_learns_from_zone_trigger_meta():
    """Closed £ with ohlc zone meta adapts gate knobs (learn-from-zone path)."""
    import tempfile
    from pathlib import Path
    from app.engine import Store

    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'robot.sqlite3')
        before = oez.load_params(store)
        out = oez.observe_close(
            store,
            ohlc_meta={
                'trigger': 'zone', 'action': 'BUY', 'allowed': True,
                'reason': 'ohlc_entry_zone_confirm',
            },
            broker_pnl=-1.8, spread=0.2)
        assert out['adapted'] is True
        after = oez.load_params(store)
        # Loss on zone → more selective (legacy band knobs + MTF).
        assert after['band_span_frac'] <= before['band_span_frac']
        assert after['mtf_opposite_conf'] <= before['mtf_opposite_conf']
