"""SL/TP from OHLC at open; cash P&L teaches when to raise / cut (audit #19)."""
import tempfile
from pathlib import Path

import pytest

from datetime import datetime, timedelta, timezone

from app.bracket_learn import (
    DEFAULT_KAPPA,DEFAULT_SL_SCALE,STRUCTURE_VS_MEDIAN_CAP,adapt_after_outcome,
    decide_brackets_from_ohlc,evolve_from_history,load_multipliers,load_params,
    note_open_bracket_meta,propose_for_open,recent_reachable_move,record_outcome,
    resample_m1_ohlc,save_multipliers,save_params,zone_at_open,_zone_single_tf,
)
from app import unified_cycle
from app.engine import Store
from app.math_rules import stop_take


def sandbox():
    return tempfile.TemporaryDirectory()


def test_stop_take_broker_min_only_no_20_tick_invent():
    bare=stop_take(100,'BUY',0.01,0.01,0,kappa=2,sl_scale=1)
    assert bare['stop_ticks']==2
    assert bare['profit_ticks']==4
    wide=stop_take(100,'BUY',0.01,0.01,0.15,kappa=1.5,sl_scale=2)
    assert wide['stop_ticks']>=17
    assert wide['sl_scale']==2.0
    assert wide['kappa']==1.5


def test_zone_at_open_computes_kappa_from_moves_not_grid():
    rows=[{'open':100,'high':103,'low':99,'close':100} for _ in range(30)]
    z=zone_at_open(rows)
    assert z['ready'] is True
    assert z['median_up']==3.0 and z['median_down']==1.0
    assert z['kappa']==3.0
    assert z['max_tp'] is not None
    assert z['lookback'] is not None and z['lookback']>=3
    assert z['noise_distance']==4.0
    assert z['structure']==4.0
    assert z['pull_share']==pytest.approx(0.25)
    assert z['favor_share']==pytest.approx(0.75)


def test_zone_sl_uses_market_shares_of_structure_not_hardcoded_fractions():
    """SL/TP shares come from OHLC up/down vs span — not coded 1/3 or 1/2."""
    rows=[]
    px=100.0
    for i in range(40):
        o=px
        px=px+0.05
        rows.append({'open':o,'high':px+0.05,'low':o-0.05,'close':px})
    z=zone_at_open(rows)
    assert z['ready'] is True
    assert z['structure'] is not None and z['structure']>z['noise_distance']
    assert z['pull_share'] is not None and z['favor_share'] is not None
    # Structure informs SL/TP but is capped vs median bar (win-streak inflate fix).
    capped_sl=min(z['structure']*z['pull_share'],
                  z['noise_distance']*STRUCTURE_VS_MEDIAN_CAP)
    capped_tp=min(z['structure']*z['favor_share'],
                  z['noise_distance']*STRUCTURE_VS_MEDIAN_CAP)
    assert z['range_distance']>=capped_sl-1e-9
    assert z['max_tp']>=min(capped_tp,z['max_tp'])-1e-9
    assert z['range_distance']<=z['noise_distance']*STRUCTURE_VS_MEDIAN_CAP+1e-6


def test_scratch_on_sl_does_not_raise_risk():
    """Scratch must not widen SL — that ratcheted £ losses with fixed lot size."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.04,exit_reason='sl',
            stop_distance=0.2,volatility=0.3,adverse_path=0.15)
        assert out['reason']=='scratch_hold_sl'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_win_does_not_inflate_kappa_or_sl():
    """Live bug: 3-win streak → fantasy SL/TP via win_reinforce + batch_raise."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_multipliers(store,1.4,1.3,reason='legacy_inflate')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=0.80,exit_reason='tp')
        assert out['reason']=='win_hold_ohlc'
        after=load_multipliers(store)
        assert after['kappa']<=before['kappa']+1e-9
        assert after['sl_scale']<=before['sl_scale']+1e-9
        # Legacy inflate heals toward 1.
        assert after['kappa']<=1.4
        assert after['sl_scale']<=1.3


def test_growing_sl_loss_streak_cuts_risk():
    """Rising |£| SL losses with fixed lot → cut sl_scale, do not widen again."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_multipliers(store,1.0,2.0,reason='seed_wide')
        store.set('bracket_outcome_history',[
            {'kappa':1.0,'sl_scale':1.5,'pnl':-0.50,'exit_reason':'sl'},
            {'kappa':1.0,'sl_scale':1.8,'pnl':-0.80,'exit_reason':'sl'},
        ])
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=2.0,pnl=-1.34,exit_reason='sl',
            stop_distance=1.0,volatility=0.2,adverse_path=1.0)
        assert out['reason']=='sl_loss_streak_cut_risk'
        assert out.get('growing_sl_losses') is True
        assert load_multipliers(store)['sl_scale']<2.0


def test_adaptive_lookback_picks_elbow_from_curve():
    from app.math_rules import adaptive_lookback,ohlc_range_distance
    rows=[{'open':100,'high':100.5,'low':99.5,'close':100} for _ in range(40)]
    n=adaptive_lookback(rows)
    assert 3<=n<=40
    assert ohlc_range_distance(rows) is not None


def test_propose_uses_ohlc_not_random_fantasy_kappa():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        rows=[{'open':100,'high':100.4,'low':99.7,'close':100.1} for _ in range(50)]
        p=propose_for_open(store,candles=rows,base_sl=1.0,max_tp=1.2)
        assert p['explored'] is False
        assert p['kappa']*p['sl_scale']*1.0 <= 1.2*1.05+1e-9
        assert p['kappa']<5.0


def test_loss_without_path_context_does_not_auto_raise_sl():
    """Audit #19: a bare loss is NOT proof that SL was too tight."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.20)
        assert out['reason']=='loss_insufficient_context_hold'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_sl_noise_vs_vol_raises_sl_when_tight():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.20,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=99.9,
            stop_distance=0.1,adverse_path=0.1,volatility=0.12)
        assert out['reason']=='sl_noise_raise_sl'
        assert load_multipliers(store)['sl_scale']>before['sl_scale']


def test_adverse_path_beyond_vol_holds_sl():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-1.50,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=98.0,
            stop_distance=2.0,adverse_path=2.0,volatility=0.5)
        assert out['reason']=='loss_adverse_hold_sl'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_wide_loss_teaches_cut_tp():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        adapt_after_outcome(
            store,kappa=2.5,sl_scale=1.5,pnl=-0.30,exit_reason='sl',
            direction='BUY',entry=100.0,exit_level=99.5,
            stop_distance=0.5,adverse_path=0.5,volatility=0.4)
        assert load_multipliers(store)['kappa']<1.0


def test_scratch_does_not_raise_sl():
    """Old heuristic test_scratch_teaches_raise_sl — inverted by audit #19."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.02)
        assert out['reason']=='scratch_hold_sl'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_vanish_holds_without_widening_sl():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.0,sl_scale=1.0,pnl=-0.40,exit_reason='vanish')
        assert out['reason']=='vanish_hold'
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_tp_win_holds_ohlc_width():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        before=load_multipliers(store)
        out=adapt_after_outcome(
            store,kappa=1.2,sl_scale=1.0,pnl=0.80,exit_reason='tp')
        assert out['reason']=='win_hold_ohlc'
        assert load_multipliers(store)['kappa']<=before['kappa']+1e-9
        assert load_multipliers(store)['sl_scale']==before['sl_scale']


def test_batch_never_raises_after_wide_wins():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_multipliers(store,1.2,1.2,reason='seed')
        save_params(store,1.0,1.0,reason='ohlc_tag')
        for _ in range(6):
            record_outcome(store,kappa=1.0,sl_scale=1.0,pnl=-0.4,currency='GBP',
                           exit_reason='sl',direction='BUY',entry=100,exit_level=99.5,
                           stop_distance=0.5,adverse_path=0.5,volatility=0.2)
        for _ in range(6):
            record_outcome(store,kappa=2.0,sl_scale=1.5,pnl=0.8,currency='GBP',
                           exit_reason='tp')
        before=load_multipliers(store)
        evolve_from_history(store,min_samples=6)
        after=load_multipliers(store)
        # Wide winning must NOT raise κ/SL (old ratchet).
        assert after['kappa']<=before['kappa']+1e-9
        assert after['sl_scale']<=before['sl_scale']+1e-9
        # Multipliers must not overwrite OHLC decision tags.
        assert load_params(store)=={'kappa':1.0,'sl_scale':1.0}


def test_structure_capped_vs_median_bar_after_big_swing():
    """One huge swing must not set SL ≈ full window peak-trough."""
    rows=[]
    for i in range(40):
        rows.append({'open':100,'high':100.3,'low':99.8,'close':100.1})
    # One massive impulse like a 3-win streak move inside the lookback.
    rows.append({'open':100,'high':120,'low':99.5,'close':118})
    z=_zone_single_tf(rows)
    assert z['ready'] is True
    assert z['noise_distance']>0
    assert z['range_distance']<=z['noise_distance']*STRUCTURE_VS_MEDIAN_CAP+1e-6
    assert z['max_tp'] is not None
    assert z['max_tp']<=max(z['noise_distance']*STRUCTURE_VS_MEDIAN_CAP,
                            z.get('median_up') or 0)+1e-6


def test_recent_reachable_move_from_candles():
    # Upper-half median of spans — no fixed 90%ile; quiet bars + larger cluster.
    rows=[{'open':100,'high':100.2,'low':100,'close':100} for _ in range(8)]
    rows+=[{'open':100,'high':107,'low':100,'close':103} for _ in range(5)]
    got=recent_reachable_move(rows,n=60)
    assert got is not None and got>=6.0


def test_save_and_load_reject_invalid():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_params(store,1.2,1.1,reason='nudge')
        assert load_params(store)=={'kappa':1.2,'sl_scale':1.1}
        store.set('bracket_kappa',-1)
        store.set('bracket_sl_scale',0.5)
        restored=load_params(store)
        assert restored['kappa']==DEFAULT_KAPPA
        assert restored['sl_scale']==DEFAULT_SL_SCALE


def _m1_series(n=120, start=None, step=0.05):
    start = start or datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    rows = []
    px = 100.0
    for i in range(n):
        o = px
        px = px + step * (1 if i % 7 != 0 else -0.5)
        rows.append({
            'at': (start + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': max(o, px) + 0.08, 'low': min(o, px) - 0.06,
            'close': px, 'spread': 0.05,
        })
    return rows


def test_resample_m1_builds_higher_timeframes():
    rows = _m1_series(60)
    m5 = resample_m1_ohlc(rows, 5)
    m15 = resample_m1_ohlc(rows, 15)
    h1 = resample_m1_ohlc(rows, 60)
    assert len(m5) == 12
    assert len(m15) == 4
    assert len(h1) == 1
    assert m5[0]['open'] == rows[0]['open']
    assert m5[0]['close'] == rows[4]['close']
    assert m5[0]['high'] >= max(r['high'] for r in rows[:5]) - 1e-9


def test_zone_at_open_uses_multi_timeframe_consensus():
    rows = _m1_series(120)
    z = zone_at_open(rows, multi_tf=True)
    assert z['ready'] is True
    assert z['multi_tf'] is True
    assert set(z['horizons_minutes']) >= {1, 5, 15}
    assert '1' in z['timeframes'] and '5' in z['timeframes']
    assert z['range_distance'] > 0 and z['max_tp'] is not None
    # Coherent pair: consensus TP is not crushed vs SL (old bug: H1 SL + M1 TP
    # → κ≈0.5 like live 25.05 / 12.53). Effective κ should stay near zone κ.
    assert z['kappa'] >= 0.75
    decided = decide_brackets_from_ohlc(z, tick=0.01, spread=0.05, min_distance=0.2)
    if decided['stop_distance'] > 0:
        rr = decided['profit_distance'] / decided['stop_distance']
        assert rr >= 0.75


def test_multi_tf_does_not_mix_wide_sl_with_tiny_tp():
    """Regression: live Gold showed SL~25 / TP~12.5 — broken TF mix."""
    # Quiet M1 noise + larger packed swings so higher TFs propose wider SL.
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    rows = []
    px = 3950.0
    for i in range(120):
        # Mostly tiny M1 ranges; every 15th bar a bigger impulse.
        impulse = 8.0 if i % 15 == 0 else 0.15
        o = px
        px = px + (0.05 if i % 2 == 0 else -0.04)
        rows.append({
            'at': (start + timedelta(minutes=i)).isoformat(),
            'open': o,
            'high': max(o, px) + impulse,
            'low': min(o, px) - impulse * 0.4,
            'close': px, 'spread': 0.5,
        })
    z = zone_at_open(rows, multi_tf=True)
    assert z['multi_tf'] is True
    decided = decide_brackets_from_ohlc(
        z, tick=0.01, spread=0.5, min_distance=0.3)
    sl = decided['stop_distance']
    tp = decided['profit_distance']
    assert sl > 0 and tp > 0
    # Must not reproduce the live asymmetry (TP ≈ 0.5 × SL).
    assert tp / sl >= 0.75
    assert abs(decided['kappa'] - (tp / sl)) < 0.15 or tp <= (z['max_tp'] or tp) + 1e-6


def test_decide_brackets_from_ohlc_uses_zone_not_multipliers():
    zone={'kappa':1.5,'range_distance':1.0,'max_tp':1.2,'noise_distance':0.8,
          'ready':True,'lookback':20,'median_up':1.5,'median_down':1.0}
    out=decide_brackets_from_ohlc(zone,tick=0.01,spread=0.05,min_distance=0.2)
    assert out['source']=='ohlc_zone'
    assert out['sl_scale']==1.0
    assert out['stop_distance']==pytest.approx(1.0,abs=0.02)
    # TP follows OHLC κ×SL but not beyond reachable max_tp.
    assert out['profit_distance']<=1.2+1e-9
    assert out['ohlc_kappa']==pytest.approx(1.5)


def _swing_rows(seq, t0=None):
    t0 = t0 or datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    return [{
        'at': (t0 + timedelta(minutes=i)).isoformat(),
        'open': o, 'high': h, 'low': l, 'close': c,
    } for i, (o, h, l, c) in enumerate(seq)]


def test_swing_anchor_brackets_not_symmetric_entry_plus_minus():
    """SL/TP must sit on swing high/low — not random ±κ around entry."""
    from app.bracket_learn import swing_anchor_brackets
    # Clear swings: SL~103, SH~112, entry mid-range.
    seq = [
        (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 102, 99.8, 100.2),
        (100.2, 105, 100, 104), (104, 108, 103.5, 107), (107, 110, 106.5, 109),
        (109, 109.5, 106, 106.5), (106.5, 107, 104.5, 105), (105, 105.5, 103, 103.5),
        (103.5, 108, 103.2, 107), (107, 111, 106.5, 110), (110, 112, 109.5, 111.5),
        (111.5, 111.8, 110.5, 111), (111, 111.2, 110.2, 110.5),
    ]
    rows = _swing_rows(seq)
    entry = 107.0
    zone = {'kappa': 1.0, 'range_distance': 4.0, 'max_tp': 6.0, 'noise_distance': 3.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.01, spread=0.05,
        min_distance=0.2, atr=2.0, zone=zone)
    assert swing['ready'] is True
    assert swing['swing_lo'] == pytest.approx(103.0, abs=0.05)
    # TP is nearest swing high above entry (reachable), not a far day extreme.
    assert swing['swing_hi'] >= entry
    assert swing['stop_level'] < entry < swing['profit_level']
    # Structural TP stays at/above a swing high; SL stays on 1m OHLC (no soft-cap).
    assert swing['profit_level'] >= entry
    assert not swing.get('rr_capped')
    assert swing['stop_level'] < swing['swing_lo']
    # Not the live Gold invent bug (±same distance both sides).
    assert abs(swing['stop_distance'] - swing['profit_distance']) > 0.5

    decided = decide_brackets_from_ohlc(
        zone, tick=0.01, spread=0.05, min_distance=0.2,
        entry=entry, action='BUY', candles=rows, atr=2.0)
    assert decided['source'] == 'swing_anchor'
    assert decided['swing_lo'] == pytest.approx(103.0, abs=0.05)
    assert decided['profit_level'] > entry


def test_post_bos_reentry_keeps_swing_not_ohlc_zone():
    """2nd OPEN after BOS must stay swing-sourced — not invent median ±distance."""
    from app.bracket_learn import swing_anchor_brackets
    # Build clear SL/SH then push close above last SH (continuation / BOS).
    seq = [
        (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 102, 99.8, 100.2),
        (100.2, 105, 100, 104), (104, 108, 103.5, 107), (107, 110, 106.5, 109),
        (109, 109.5, 106, 106.5), (106.5, 107, 104.5, 105), (105, 105.5, 103, 103.5),
        (103.5, 108, 103.2, 107), (107, 111, 106.5, 110), (110, 112, 109.5, 111.5),
        (111.5, 111.8, 110.5, 111), (111, 111.2, 110.2, 110.5),
        # Break above last SH=112
        (110.5, 114.5, 110.4, 114.0), (114.0, 115.0, 113.5, 114.5),
        (114.5, 114.8, 113.8, 114.2),
    ]
    rows = _swing_rows(seq)
    entry = 114.2  # above last confirmed SH — old code returned entry_above_swing_high
    zone = {'kappa': 1.0, 'range_distance': 4.0, 'max_tp': 6.0, 'noise_distance': 3.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.01, spread=0.05,
        min_distance=0.2, atr=2.0, zone=zone)
    assert swing['ready'] is True, swing.get('reason')
    assert swing['source'] == 'swing_anchor'
    assert swing['stop_level'] < entry < swing['profit_level']
    base = str(swing.get('reason') or '').split('+')[0]
    assert base in ('swing_levels', 'swing_measured_move', 'swing_levels_closer_sl')
    if swing.get('rr_capped'):
        assert swing['rr'] + 1e-9 >= float(swing.get('min_rr') or 1.0)
    else:
        assert abs(swing['stop_distance'] - swing['profit_distance']) > 0.3

    decided = decide_brackets_from_ohlc(
        zone, tick=0.01, spread=0.05, min_distance=0.2,
        entry=entry, action='BUY', candles=rows, atr=2.0, require_swing=True)
    assert decided['ready'] is True
    assert decided['source'] == 'swing_anchor'
    assert decided['source'] != 'ohlc_zone'


def test_apply_min_rr_caps_wide_sl_toward_entry():
    """Gold-style ~21pt SL vs ~3pt TP → SL capped to profit/min_rr (not fantasy TP)."""
    from app.bracket_learn import apply_min_rr
    # SELL: entry 2650, SL far above (21), TP only 3 below.
    entry, stop_d, profit_d = 2650.0, 21.0, 3.0
    stop_level = entry + stop_d
    profit_level = entry - profit_d
    out = apply_min_rr(
        action='SELL', entry=entry, stop_level=stop_level,
        profit_level=profit_level, stop_d=stop_d, profit_d=profit_d,
        floor=1.0, min_rr=1.0)
    assert out['ok'] is True
    assert out['capped'] is True
    assert out['stop_distance'] == pytest.approx(3.0, abs=1e-9)
    assert out['profit_distance'] == pytest.approx(3.0, abs=1e-9)
    assert out['rr'] == pytest.approx(1.0, abs=1e-9)
    assert out['stop_level'] == pytest.approx(entry + 3.0, abs=1e-9)
    assert out['profit_level'] == pytest.approx(profit_level, abs=1e-9)


def test_apply_min_rr_refuses_when_floor_blocks_cap():
    """If SL cannot shrink to hit RR without going below ATR floor → refuse."""
    from app.bracket_learn import apply_min_rr
    out = apply_min_rr(
        action='BUY', entry=100.0, stop_level=90.0, profit_level=101.0,
        stop_d=10.0, profit_d=1.0, floor=5.0, min_rr=1.0)
    assert out['ok'] is False
    assert out['reason'] == 'rr_unachievable_vs_floor'


def test_swing_anchor_prefers_rr_extend_or_cap_not_zone_invent():
    """Wide 1m swing vs tight TP: extend TP / soft-cap SL — never median invent."""
    from app.bracket_learn import swing_anchor_brackets
    # Deep swing low far under entry; nearest high only a few pts above.
    seq = [
        (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 102, 99.8, 100.2),
        (100.2, 105, 100, 104), (104, 108, 103.5, 107), (107, 110, 106.5, 109),
        # Deep swing low ~95 then grind up to entry ~112 with tiny upside room.
        (109, 109.5, 95.0, 96.0), (96.0, 100, 94.8, 99.5), (99.5, 105, 99, 104),
        (104, 108, 103.5, 107), (107, 111, 106.5, 110), (110, 113, 109.5, 112.5),
        (112.5, 113.0, 111.8, 112.2), (112.2, 112.5, 111.5, 112.0),
    ]
    rows = _swing_rows(seq)
    entry = 112.0
    zone = {'kappa': 1.0, 'range_distance': 4.0, 'max_tp': 6.0, 'noise_distance': 3.0}
    swing = swing_anchor_brackets(
        candles=rows, entry=entry, action='BUY', tick=0.01, spread=0.05,
        min_distance=0.2, atr=2.0, zone=zone)
    if swing.get('ready'):
        assert swing['rr'] + 1e-9 >= float(swing.get('min_rr') or 1.0)
        assert float(swing['stop_level']) < entry
        assert swing['source'] == 'swing_anchor'
    else:
        assert swing.get('reason') in (
            'rr_keep_structural_sl', 'rr_unachievable_vs_floor',
            'rr_unachievable', 'rr_fail',
            'swing_distances_below_floor', 'no_structural_swings_for_entry')


def test_live_open_does_not_silently_fall_back_to_ohlc_zone():
    """When swings cannot place levels, refuse — do not invent median ±distance."""
    zone = {'kappa': 1.5, 'range_distance': 12.0, 'max_tp': 20.0, 'noise_distance': 10.0,
            'ready': True}
    # Too few bars → no pivots → require_swing must refuse zone invent.
    rows = _swing_rows([(100, 100.5, 99.5, 100.2)] * 4)
    out = decide_brackets_from_ohlc(
        zone, tick=0.01, spread=0.05, min_distance=0.2,
        entry=100.0, action='BUY', candles=rows, require_swing=True)
    assert out['ready'] is False
    assert out['source'] == 'swing_anchor'
    assert out.get('stop_distance') is None


def test_ohlc_decision_ignores_inflated_learned_multipliers_on_open():
    """Even if Store mults are huge, OPEN width still comes from OHLC zone."""
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        save_multipliers(store,2.5,2.5,reason='inflated')
        zone={'kappa':1.2,'range_distance':1.0,'max_tp':1.5,'ready':True}
        pkg=unified_cycle.compose_open(
            action='BUY',
            scores={'BUY':0.55,'SELL':0.25,'HOLD':0.20},
            evaluation={'action':'BUY','score_edge':0.30},
            zone=zone,learned=load_multipliers(store),
            cycle_state=unified_cycle.default_state(),
            base_sl=1.0,max_tp=1.5)
        assert pkg['bracket_source']=='ohlc_zone'
        assert pkg['sl_scale']==1.0
        assert pkg['kappa']==pytest.approx(1.2) or pkg['kappa']<=1.5+1e-9
        decided=decide_brackets_from_ohlc(
            zone,tick=0.01,spread=0.05,min_distance=0.2,
            tighten=float(pkg.get('tighten') or 1.0))
        assert decided['stop_distance']<=1.0+0.02
        assert decided['profit_distance']<=1.5+1e-9


def test_win_at_full_ohlc_tp_does_not_raise_unused_mult_for_width():
    with sandbox() as d:
        store=Store(Path(d)/'db.sqlite3')
        note_open_bracket_meta(store,{
            'source':'ohlc_zone','tp_at_ohlc_cap':True,
            'ohlc_tp':1.5,'profit_distance':1.5,'stop_distance':1.0})
        before=load_multipliers(store)['kappa']
        out=adapt_after_outcome(
            store,kappa=1.2,sl_scale=1.0,pnl=0.80,exit_reason='tp')
        assert out['reason']=='tp_at_ohlc_cap_hold'
        assert load_multipliers(store)['kappa']==pytest.approx(before)
