"""Situation memory — scene → £ → decay → soft OPEN influence (ready loop)."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import Store
from app import situation_memory as sm
from app.autonomy import AutoEvolution


def _store(tmp):
    return Store(Path(tmp) / 't.sqlite3')


def test_fingerprint_describes_scene_not_ban():
    fp = sm.fingerprint(
        epic='GOLD', trend='DOWN',
        returns_bps={'5': -12.0, '15': -8.0},
        structure='TREND_DOWN')
    assert fp == 'GOLD|TREND_DOWN|FLOW_DOWN|TREND_DOWN'
    # Fingerprint is a label only — module never hardcodes DOWN→no BUY.


def test_record_and_compare_prefers_paying_side():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        sit = 'GOLD|TREND_DOWN|FLOW_DOWN|TREND_DOWN'
        for _ in range(4):
            sm.record_outcome(s, situation=sit, action='BUY', pnl=-2.0)
            sm.record_outcome(s, situation=sit, action='SELL', pnl=1.5)
        out = sm.compare_situation(s, situation=sit)
        assert out['advice'] == 'SELL'
        assert out['confidence'] >= 0.35
        assert 'SELL' in out['reason']


def test_both_sides_negative_advice_hold():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        sit = 'GOLD|TREND_UP|FLOW_MIXED|CHOP'
        for _ in range(4):
            sm.record_outcome(s, situation=sit, action='BUY', pnl=-1.0)
            sm.record_outcome(s, situation=sit, action='SELL', pnl=-0.8)
        out = sm.compare_situation(s, situation=sit)
        assert out['advice'] == 'HOLD'


def test_soft_nudge_renormalizes_for_autonomy():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        advice = {
            'advice': 'HOLD', 'confidence': 0.8, 'reason': 'test',
            'situation': 'X',
        }
        scores = {'BUY': 0.55, 'SELL': 0.30, 'HOLD': 0.15}
        nudged, meta = sm.soft_nudge_scores(scores, advice, store=s)
        assert meta['nudged'] is True
        assert nudged['HOLD'] > scores['HOLD']
        assert nudged['BUY'] < scores['BUY']
        assert abs(sum(nudged.values()) - 1.0) < 1e-9
        assert AutoEvolution._safe_scores(nudged)


def test_decay_forgets_stale_losses():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        sit = 'GOLD|TREND_DOWN|FLOW_DOWN|TREND_DOWN'
        old = datetime.now(timezone.utc) - timedelta(days=60)
        # Seed stale BUY losses with old timestamp via direct bucket write.
        state = sm.load(s)
        key = sm._bucket_key(sit, 'BUY')
        state['buckets'][key] = {
            'situation': sit, 'action': 'BUY',
            'samples_eff': 10.0, 'total_pnl_eff': -20.0,
            'mean_pnl': -2.0, 'win_eff': 0.0, 'loss_eff': 10.0,
            'raw_count': 10, 'updated_at': old.isoformat(timespec='seconds'),
        }
        sm.save(s, state)
        # Fresh SELL wins (recent).
        for _ in range(4):
            sm.record_outcome(s, situation=sit, action='SELL', pnl=1.0)
        out = sm.compare_situation(s, situation=sit)
        # After half-life decay, stale BUY mass shrinks; SELL can advise.
        buy = sm.side_stats(s, sit, 'BUY')
        assert buy['samples'] < 5.0  # decayed from 10
        assert out['advice'] in ('SELL', 'HOLD', None) or out['buy']['samples'] < out['sell']['samples']


def test_insufficient_evidence_no_advice():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        sit = 'GOLD|TREND_UP|FLOW_UP|TREND_UP'
        sm.record_outcome(s, situation=sit, action='BUY', pnl=1.0)
        out = sm.compare_situation(s, situation=sit)
        assert out['advice'] is None
        assert out['reason'] == 'insufficient_evidence'


def test_engine_status_exposes_situation_memory():
    """Status self_development includes situation_memory snapshot key."""
    from app import situation_memory as mod
    snap = mod.snapshot(None)
    assert 'bucket_count' in snap
