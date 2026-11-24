"""V3 B05/B08 / H03–H04 — shadow paper bid/ask SL/TP and incomplete OHLC."""
import tempfile
from pathlib import Path

import pytest

from app.engine import Store
from app.shadow_paper import (
    SAME_BAR_BOTH_HIT_POLICY,
    ShadowPaperJournal,
    hit_sl_tp,
    simulate_action_on_path,
)


def test_same_bar_both_hit_policy_is_conservative_sl():
    assert SAME_BAR_BOTH_HIT_POLICY == 'conservative_sl'


def test_buy_tp_not_hit_on_ask_side_of_mid_bar():
    """B08: BUY TP=105 on bid; high_mid=104, spread=2 → highest bid=103, no TP.

    Old bug used ask=high+half=105 and falsely triggered TP. SL kept far away.
    """
    hit = hit_sl_tp(
        'BUY', high=104., low=100., stop=90., profit=105.,
        mid=102., spread=2., slip_bps=0.)
    assert hit is None


def test_sell_tp_not_hit_on_bid_side_of_mid_bar():
    """B08: SELL TP=95 on ask; low_mid=96, spread=2 → lowest ask=97, no TP."""
    hit = hit_sl_tp(
        'SELL', high=100., low=96., stop=110., profit=95.,
        mid=98., spread=2., slip_bps=0.)
    assert hit is None


def test_buy_tp_hits_when_bid_extreme_reaches_level():
    """BUY TP on bid: high_mid=106, spread=2 → bid_hi=105 reaches TP=105."""
    exit_px, reason = hit_sl_tp(
        'BUY', high=106., low=100., stop=90., profit=105.,
        mid=103., spread=2., slip_bps=0.)
    assert reason == 'tp'
    assert exit_px == pytest.approx(105.)


def test_sell_tp_hits_when_ask_extreme_reaches_level():
    """SELL TP on ask: low_mid=94, spread=2 → ask_lo=95 reaches TP=95."""
    exit_px, reason = hit_sl_tp(
        'SELL', high=100., low=94., stop=110., profit=95.,
        mid=97., spread=2., slip_bps=0.)
    assert reason == 'tp'
    assert exit_px == pytest.approx(95.)


def test_same_bar_sl_and_tp_takes_conservative_sl_not_tp():
    """B08/H04: both touched, no tick sequence → conservative SL."""
    # BUY: bid_lo=98 <= SL 99, bid_hi=105 >= TP 105
    exit_px, reason = hit_sl_tp(
        'BUY', high=106., low=99., stop=99., profit=105.,
        mid=102., spread=2., slip_bps=0.)
    assert reason == 'sl'
    # Gap-aware: lowest bid = 99-1 = 98 → fill min(99, 98) = 98
    assert exit_px == pytest.approx(98.)


def test_buy_sl_applies_gap_and_adverse_slip():
    exit_px, reason = hit_sl_tp(
        'BUY', high=101., low=97., stop=99., profit=105.,
        mid=100., spread=0., slip_bps=10.)  # 10 bps of mid=0.1
    assert reason == 'sl'
    assert exit_px == pytest.approx(97. - 0.1)


def test_missing_high_low_does_not_invent_from_mid():
    """H03: no H/L → no SL/TP hit and no fabricated range."""
    assert hit_sl_tp(
        'BUY', high=None, low=None, stop=99., profit=105.,
        mid=100., spread=2., slip_bps=0.) is None
    assert hit_sl_tp(
        'BUY', high=None, low=99., stop=99., profit=105.,
        mid=100., spread=2., slip_bps=0.) is None


def test_on_bar_skips_sl_tp_when_ohlc_incomplete():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.position = {
            'direction': 'BUY', 'entry': 100., 'stop': 98., 'profit': 104.,
            'original_risk': 2., 'tick': 0.1, 'min_distance': 0.1,
            'trail_extreme': None,
        }
        # Mid alone would look like a stop if we invented low=mid — must not close.
        closed = j.on_bar(
            'HOLD', mid=97., spread=0.2, bar_time='t1',
            high=None, low=None, slip_bps=0., fill_delay_bars=0)
        assert closed is None
        assert j.position is not None


def test_simulate_path_does_not_invent_hl_from_mid_only_bars():
    # Mid-only bars: incomplete path → path_end, not fabricated SL/TP.
    bars = [{'mid': 100.} for _ in range(3)]
    sim = simulate_action_on_path(
        'BUY', bars, spread=0.2, stop_distance=1.0, profit_distance=2.0, tick=0.1)
    assert sim is not None
    assert sim['reason'] == 'path_end'


def test_paper_cash_excludes_live_funds_rows():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        j = ShadowPaperJournal(store)
        j.history = [
            {'paper_pnl': 5.0, 'unit': 'account_currency', 'source': 'shadow_paper'},
            {'paper_pnl': 99.0, 'unit': 'account_currency', 'source': 'capital_journal'},
            {'paper_pnl': 7.0, 'unit': 'account_currency', 'source': 'live_funds_mirror',
             'pnl_source': 'funds'},
        ]
        assert j.paper_cash_pnls() == [5.0]
        s = j.summary()
        assert s['independent_of_live'] is True
        assert s['paper_net'] == pytest.approx(5.0)
