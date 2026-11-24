"""OHLC regime detectors — soft by default; enable to assert ban reasons."""
from app import ohlc_regime_gate as rg


class _EnableStore:
    """Minimal store so evaluate() runs with gate enabled."""

    def get(self, key, default=None):
        if key == 'self_params_v1':
            return {'ohlc_regime_gate_enabled': 1}
        return default


def _bar(o, h, l, c):
    return {'open': o, 'high': h, 'low': l, 'close': c, 'mid': c}


def _eval(**kwargs):
    return rg.evaluate(store=_EnableStore(), **kwargs)


def test_gate_disabled_when_playbook_soft():
    """Explicit soft playbook keeps regime gate off (legacy soft notes)."""
    class SoftStore:
        def get(self, key, default=None):
            if key == 'self_params_v1':
                return {'playbook_hard_entry': 0, 'ohlc_regime_gate_enabled': 0}
            return default

    candles = [_bar(100, 100.02, 99.98, 100.01) for _ in range(20)]
    out = rg.evaluate(candles=candles, mid=100.0, spread=0.05, atr=0.02,
                      store=SoftStore())
    assert out['allowed'] is True
    assert out['reason'] == 'disabled'
    assert out['enabled'] is False


def test_playbook_hard_forces_regime_gate_on():
    candles = [_bar(100, 100.02, 99.98, 100.01) for _ in range(20)]
    out = rg.evaluate(candles=candles, mid=100.0, spread=0.05, atr=0.02)
    assert out['enabled'] is True
    # Low vol path should ban under playbook hard (no store → hard default).
    assert out['allowed'] is False
    assert out['reason'] == 'no_trade_low_vol'


def test_chop_blocks():
    # Alternating tiny progress — classic chop.
    candles = []
    px = 100.0
    for i in range(14):
        if i % 2 == 0:
            candles.append(_bar(px, px + 0.4, px - 0.1, px + 0.2))
            px += 0.05
        else:
            candles.append(_bar(px, px + 0.1, px - 0.4, px - 0.2))
            px -= 0.05
    out = _eval(candles=candles, mid=100.0, spread=0.05, atr=0.5)
    assert out['allowed'] is False
    assert out['reason'] == 'no_trade_chop'


def test_low_vol_blocks():
    candles = [_bar(100, 100.02, 99.98, 100.01) for _ in range(20)]
    out = _eval(candles=candles, mid=100.0, spread=0.05, atr=0.02)
    assert out['allowed'] is False
    assert out['reason'] == 'no_trade_low_vol'


def test_doji_blocks():
    candles = [_bar(100, 100.8, 99.2, 100.01) for _ in range(12)]  # long wicks, tiny body
    out = _eval(candles=candles, mid=100.0, spread=0.05, atr=1.0)
    assert out['allowed'] is False
    assert out['reason'] == 'no_trade_doji'


def test_wick_structure_with_atr_body_is_not_doji():
    """Swing-structure bar: body ~0.3×ATR with wicks must not count as doji."""
    candles = []
    px = 100.0
    for i in range(16):
        o = px
        c = px + 0.35
        candles.append(_bar(o, max(o, c) + 0.85, min(o, c) - 0.85, c))
        px = c
    assert rg._is_doji(candles[-1], 0.18, atr=1.0) is False
    out = _eval(candles=candles, mid=px, spread=0.05, atr=1.0)
    assert out['allowed'] is True
    assert out['reason'] == 'pass'
    assert (out.get('flags') or {}).get('doji_share', 1) < 0.55


def test_impulse_blocks():
    candles = [_bar(100 + i * 0.1, 100.3 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1)
               for i in range(15)]
    candles.append(_bar(101.5, 106.0, 101.4, 105.5))
    out = _eval(candles=candles, mid=105.0, spread=0.1, atr=0.8)
    assert out['allowed'] is False
    assert out['reason'] == 'no_trade_impulse'


def test_chaos_structure_label_blocks():
    candles = [_bar(100, 100.5, 99.5, 100.2) for _ in range(12)]
    out = _eval(candles=candles, mid=100.0, spread=0.05, atr=1.0,
                structure='CHOP')
    assert out['allowed'] is False
    assert out['reason'] == 'no_trade_chaos'


def test_clean_trend_passes():
    candles = []
    px = 100.0
    for _ in range(16):
        candles.append(_bar(px, px + 0.6, px - 0.1, px + 0.5))
        px += 0.5
    out = _eval(candles=candles, mid=px, spread=0.05, atr=0.7)
    assert out['allowed'] is True
    assert out['reason'] == 'pass'
