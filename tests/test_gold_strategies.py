"""GOLD audit strategies: structure align / min RR / BUY-strict + teach labels."""
from app import gold_strategies as gs


def test_applies_only_to_gold_xau():
    assert gs.applies_to_epic('GOLD') is True
    assert gs.applies_to_epic('CS.D.GOLD.CFD.IP') is True
    assert gs.applies_to_epic('XAUUSD') is True
    assert gs.applies_to_epic('SILVER') is False
    assert gs.applies_to_epic('EURUSD') is False


def test_structure_align_blocks_against():
    bad = gs.structure_allows('BUY', 'BREAKOUT_DOWN')
    assert bad['ok'] is False
    assert bad['strategy'] == 'structure_align'
    good = gs.structure_allows('SELL', 'BREAKOUT_DOWN')
    assert good['ok'] is True
    assert gs.structure_allows('BUY', 'TREND_UP')['ok'] is True
    assert gs.structure_allows('SELL', 'BREAKOUT_UP')['ok'] is False


def test_min_rr_floor():
    assert gs.rr_allows(2.0, 3.0, floor=1.0)['ok'] is True
    bad = gs.rr_allows(5.0, 4.0, floor=1.0)
    assert bad['ok'] is False
    assert bad['rr'] == 0.8
    assert gs.rr_allows(None, 2.0)['ok'] is False


def test_buy_strict_down():
    assert gs.buy_strict_allows('SELL', 'TREND_DOWN', score_edge=0.01)['ok'] is True
    assert gs.buy_strict_allows('BUY', 'TREND_UP', score_edge=0.01)['ok'] is True
    bad = gs.buy_strict_allows('BUY', 'TREND_DOWN', score_edge=0.05, min_edge=0.12)
    assert bad['ok'] is False
    ok = gs.buy_strict_allows('BUY', 'BREAKOUT_DOWN', score_edge=0.20, min_edge=0.12)
    assert ok['ok'] is True


def test_evaluate_soft_rejects_against_structure_by_default():
    """Default: teach correct approach — still allowed, soft_reject + preferred."""
    out = gs.evaluate(
        epic='GOLD', action='BUY', structure='BREAKOUT_DOWN',
        stop_distance=4.0, profit_distance=8.0, score_edge=0.3)
    assert out['applies'] is True
    assert out['allowed'] is True
    assert out.get('soft_reject') is True
    assert out['preferred'] == 'SELL'
    assert out['blocked_by'] == 'structure_align'


def test_evaluate_hard_block_when_enabled(tmp_path):
    from app.engine import Store
    from app import self_params
    store = Store(tmp_path / 's.db')
    self_params.save(store, {
        **{k: self_params.DEFAULTS[k] for k in self_params.DEFAULTS},
        'gold_strat_enabled': 1,
        'gold_strat_hard_block': 1,
        'gold_strat_structure_align': 1,
        'gold_strat_min_rr': 0,
        'gold_strat_buy_strict_down': 0,
    }, reason='test')
    out = gs.evaluate(
        epic='GOLD', action='BUY', structure='BREAKOUT_DOWN',
        stop_distance=4.0, profit_distance=8.0, score_edge=0.3, store=store)
    assert out['allowed'] is False
    assert out['hard_block'] is True


def test_soft_nudge_teaches_sell_on_breakdown():
    gate = gs.evaluate(
        epic='GOLD', action='BUY', structure='TREND_DOWN',
        stop_distance=3.0, profit_distance=6.0, score_edge=0.3)
    scores = {'BUY': 0.65, 'SELL': 0.25, 'HOLD': 0.10}
    nudged, meta = gs.soft_nudge_scores(scores, gate)
    assert meta['nudged'] is True
    assert meta['target'] == 'SELL'
    assert nudged['SELL'] > nudged['BUY']
    assert abs(sum(nudged.values()) - 1.0) < 1e-9


def test_evaluate_blocks_weak_rr_when_enabled(tmp_path):
    from app.engine import Store
    from app import self_params
    store = Store(tmp_path / 's.db')
    self_params.save(store, {
        **{k: self_params.DEFAULTS[k] for k in self_params.DEFAULTS},
        'gold_strat_enabled': 1,
        'gold_strat_hard_block': 1,
        'gold_strat_structure_align': 0,
        'gold_strat_min_rr': 1,
        'gold_strat_buy_strict_down': 0,
        'gold_strat_min_rr_floor': 1.0,
    }, reason='test')
    out = gs.evaluate(
        epic='GOLD', action='BUY', structure='TREND_UP',
        stop_distance=5.0, profit_distance=4.0, score_edge=0.3,
        store=store)
    assert out['allowed'] is False
    assert out['blocked_by'] == 'min_rr'


def test_evaluate_passes_aligned_good_rr():
    out = gs.evaluate(
        epic='GOLD', action='SELL', structure='TREND_DOWN',
        stop_distance=3.0, profit_distance=6.0, score_edge=0.2)
    assert out['allowed'] is True
    assert out['reason'] == 'pass'


def test_evaluate_skips_non_gold():
    out = gs.evaluate(
        epic='SILVER', action='BUY', structure='BREAKOUT_DOWN',
        stop_distance=1.0, profit_distance=0.5)
    assert out['applies'] is False
    assert out['allowed'] is True
    assert out['reason'] == 'not_gold'


def test_teach_against_structure_loss():
    t = gs.teach_label_from_outcome(
        action='BUY', structure='BREAKOUT_DOWN', pnl=-0.5,
        stop_distance=5.0, profit_distance=5.0)
    assert t['label'] == 'HOLD'
    assert t['mode'] == 'gold_against_structure_loss'
    assert t['weight'] >= 2


def test_teach_aligned_win():
    t = gs.teach_label_from_outcome(
        action='SELL', structure='TREND_DOWN', pnl=0.4,
        stop_distance=3.0, profit_distance=6.0)
    assert t['label'] == 'SELL'
    assert t['mode'] == 'gold_aligned_win'
