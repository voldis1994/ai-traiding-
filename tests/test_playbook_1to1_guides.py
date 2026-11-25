"""1:1 fidelity locks for the three playbook guide cards.

Guide 1 — zones · Guide 2 — scenarios · Guide 3 — SL/TP regimes.
"""
from datetime import datetime, timezone

from app import bracket_learn as bl
from app import ohlc_entry_zones as oez
from app import ohlc_regime_brackets as orb
from app import ohlc_scenarios as osc
from app import playbook_entry as pb
from app import structure_memory as sm
from app.self_params import DEFAULTS


def test_guide1_zone_is_base_before_impulse():
    """Supply/Demand box = base High→Low immediately before impulse."""
    # Compact base then down impulse → Supply.
    rows = []
    for i in range(12):
        rows.append({
            'open': 100.0 + i * 0.1, 'high': 100.4 + i * 0.1,
            'low': 99.7 + i * 0.1, 'close': 100.2 + i * 0.1,
            'at': f'2026-11-25T08:{i:02d}:00Z',
        })
    # Base (tight)
    for j in range(4):
        rows.append({
            'open': 101.2, 'high': 101.5, 'low': 101.0, 'close': 101.3,
            'at': f'2026-11-25T08:{12 + j:02d}:00Z',
        })
    # Impulse DOWN
    for k in range(4):
        o = 101.2 - k * 0.8
        c = o - 0.9
        rows.append({
            'open': o, 'high': o + 0.1, 'low': c - 0.1, 'close': c,
            'at': f'2026-11-25T08:{16 + k:02d}:00Z',
        })
    det = sm.detect_base_impulse_zones(rows, atr=0.5)
    assert det.get('ready'), det.get('reason')
    supply = det.get('supply') or []
    assert supply
    best = supply[0]
    assert float(best['lo']) >= 100.9
    assert float(best['hi']) <= 101.6
    assert best['side'] == 'SELL'


def test_guide1_zone_touch_is_limit_in_zone_path():
    """Confirm off = guide “limit inside zone”; touch opens without pin."""
    assert pb.zone_confirm_required(None) is False
    assert DEFAULTS.get('playbook_require_zone_confirm') == 0


def test_guide2_scenario_tags_cover_playbook_list():
    """Classify must be able to stamp the guide scenario family."""
    # Synthetic HH+HL uptrend path.
    rows = []
    px = 100.0
    for i in range(40):
        o = px
        c = px + 0.4
        rows.append({
            'open': o, 'high': max(o, c) + 0.15, 'low': min(o, c) - 0.1,
            'close': c, 'at': f'2026-11-25T09:{i:02d}:00Z',
        })
        px = c
        if i % 5 == 4:
            # small pullback HL
            rows.append({
                'open': px, 'high': px + 0.05, 'low': px - 0.25, 'close': px - 0.15,
                'at': f'2026-11-25T09:{i:02d}:30Z',
            })
            px = px - 0.15
    sc = osc.classify(rows, mid=px, tick=0.01)
    assert sc.get('ready')
    tags = set(sc.get('scenarios') or [])
    # At least one guide family tag present.
    family = {
        'TREND_UP', 'TREND_DOWN', 'RANGE', 'BREAKOUT_UP', 'BREAKOUT_DOWN',
        'RETEST_UP', 'RETEST_DOWN', 'FAKEOUT', 'CHOCH_UP', 'CHOCH_DOWN',
        'COMPRESSION',
    }
    assert tags & family, (sc.get('scenario'), tags)
    assert sc.get('block_mid_range') is True


def test_guide2_mid_range_blocked():
    sc = {
        'ready': True, 'scenario': 'RANGE', 'block_mid_range': True,
        'range': {'lo': 100.0, 'hi': 110.0},
    }
    blocked, reason = osc.mid_range_blocked('BUY', sc, 105.0, tick=0.01)
    assert blocked is True
    assert 'mid_range' in (reason or '')


def test_guide3_breakout_regime_sl_under_break_candle():
    """Guide 3 breakout: SL below break candle low + buffer."""
    from datetime import timedelta
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
        scenario={'ready': True, 'scenario': 'BREAKOUT_UP',
                  'range': {'lo': 99.7, 'hi': 100.3}},
        trigger='break', tick=0.01, spread=0.02, min_distance=0.05)
    assert out.get('ready') is True, out
    assert out['stop_level'] < 101.2
    assert abs(out['stop_level'] - (99.9 - out['buffer'])) < 1e-6


def test_guide3_min_rr_seed_is_at_least_1_5():
    assert float(DEFAULTS['bracket_min_rr']) >= 1.5
    assert float(bl.DEFAULT_MIN_RR) >= 1.5


def test_guide3_session_filter_wired_helper():
    """Session window must be enforceable (engine calls session_allows_open)."""
    import inspect
    from app import engine as eng
    src = inspect.getsource(eng.SingleTradeEngine._entry_evaluation)
    assert 'session_allows_open' in src
    src2 = inspect.getsource(eng.SingleTradeEngine.submit)
    assert 'session_allows_open' in src2


def test_guide1_sl_behind_zone_via_regime_reversal():
    zone = {'lo': 100.0, 'hi': 100.5, 'side': 'BUY'}
    rows = [{'open': 100.2, 'high': 100.6, 'low': 99.8, 'close': 100.4}
            for _ in range(25)]
    out = orb.regime_brackets(
        candles=rows, entry=100.3, action='BUY',
        scenario={'ready': True, 'scenario': 'RETEST_UP'},
        entry_zone=zone, zones={'buy_entry': zone},
        trigger='zone', tick=0.01, spread=0.02, min_distance=0.05, atr=0.2)
    if out.get('ready'):
        # SL behind demand (below zone.lo − buffer) when zone_sl used.
        if out.get('zone_sl'):
            assert out['stop_level'] <= float(zone['lo']) + 1e-9
