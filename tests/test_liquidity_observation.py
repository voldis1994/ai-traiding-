"""Observation-time freshness, no silent bar substitution, availability & labels."""
from datetime import datetime, timedelta, timezone
import json

from app.liquidity_feed import (
    ALLOWED_BAR_SKEW_SEC, MAX_OBSERVATION_AGE_SEC,
    build_liquidity_features, build_liquidity_observation, _series_at,
)
from app.market_fusion import fusion_gap_reason
from app.market_context import FEATURE_DIM


BASE = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _vol_bars(n=40, start=None, base_vol=100.0, end=None):
    """Bars from start for n minutes, or until end (inclusive) if given."""
    start = start or (BASE - timedelta(minutes=40))
    if end is not None:
        n = int((end - start).total_seconds() // 60) + 1
    bars = []
    for i in range(n):
        t = start + timedelta(minutes=i)
        bars.append({
            't': t.replace(second=0, microsecond=0).isoformat(),
            'ts': int(t.timestamp()),
            'volume': float(base_vol + (i % 7) * 3),
            'close': 4000.0 + i * 0.1,
        })
    return bars


def _snapshot(*, bar=BASE, epic='GOLD', extra_vol=0.0, with_book=True,
              series_end=None, updated_at=None, book_observed_at=None):
    end = series_end or bar
    start = end - timedelta(minutes=39)
    bars_a = _vol_bars(start=start, end=end)
    bars_b = _vol_bars(start=start, end=end, base_vol=50.0)
    target = bar.replace(second=0, microsecond=0).isoformat()
    for series in (bars_a, bars_b):
        for b in series:
            if b['t'] == target:
                b['volume'] += extra_vol
    snap = {
        'ok': True,
        'epic': epic,
        'updated_at': (updated_at or bar).isoformat(),
        'routes': {
            'epic': epic, 'kind': 'METALS', 'label': 'METAL_GOLD',
            'yahoo': ['GC=F', 'GLD'], 'coinbase': 'PAXG-USD', 'mapped': True,
        },
        'series': [
            {'symbol': 'GC=F', 'source': 'yahoo_finance', 'bars': bars_a},
            {'symbol': 'GLD', 'source': 'yahoo_finance', 'bars': bars_b},
        ],
        'book': None,
        'sources': ['yahoo:GC=F', 'yahoo:GLD'],
        'errors': [],
    }
    if with_book:
        obs = book_observed_at or updated_at or bar
        snap['book'] = {
            'source': 'coinbase', 'symbol': 'PAXG-USD',
            'bid': 4000.0, 'ask': 4001.0,
            'imbalance': 0.25, 'micro_bps': 0.1, 'spread_bps': 2.5, 'levels': 10,
            'observed_at': obs.isoformat(),
        }
        snap['sources'].append('coinbase:book:PAXG-USD')
    return snap


def test_stale_observation_rejected_despite_fresh_download():
    """Item 10: bar observation clock matters — not only snapshot updated_at."""
    # Snapshot just refreshed at BASE, but futures tape ends 10 minutes earlier.
    stale_end = BASE - timedelta(minutes=10)
    snap = _snapshot(bar=stale_end, epic='GOLD', with_book=False,
                     series_end=stale_end, updated_at=BASE)
    # Asking for the stale bar while "now" is BASE: observation age >> max.
    obs = build_liquidity_observation(snap, stale_end.isoformat(), now=BASE, epic='GOLD')
    assert obs['ok'] is False
    assert obs['features'] is None
    assert 'observation' in (obs['reason'] or '').lower() or 'fresh' in (obs['reason'] or '').lower()
    assert build_liquidity_features(snap, stale_end.isoformat(), now=BASE, epic='GOLD') is None

    # Control: same bars when now ≈ observation time still work.
    fresh = build_liquidity_observation(
        _snapshot(bar=stale_end, with_book=False, updated_at=stale_end),
        stale_end.isoformat(), now=stale_end, epic='GOLD')
    assert fresh['ok'] is True and fresh['features'] is not None


def test_no_silent_older_bar_substitution():
    """Item 11: missing target minute must not silently reuse arbitrary older volume."""
    # Bars stop 5 minutes before needed minute — beyond futures skew (60s).
    needed = BASE
    series_end = BASE - timedelta(minutes=5)
    snap = _snapshot(bar=series_end, epic='GOLD', with_book=False,
                     series_end=series_end, updated_at=BASE)
    # Force updated_at fresh and ask for needed minute that has no bar.
    snap['updated_at'] = BASE.isoformat()
    obs = build_liquidity_observation(snap, needed.isoformat(), now=BASE, epic='GOLD')
    assert obs['ok'] is False
    assert obs['features'] is None

    hist, cur, used = _series_at(
        snap['series'][0]['bars'], needed.isoformat(),
        allowed_skew_sec=ALLOWED_BAR_SKEW_SEC['yahoo_futures'])
    assert hist is None and cur is None and used is None

    # Within skew: prior minute is allowed for futures, and observation_time recorded.
    skew_end = BASE - timedelta(minutes=1)
    snap2 = _snapshot(bar=skew_end, with_book=False, series_end=skew_end, updated_at=BASE)
    snap2['updated_at'] = BASE.isoformat()
    # Age of skew_end vs now=BASE is 60s — at futures max observation age edge for the bar.
    # Use now close to skew_end so observation-age passes; target is BASE (1m ahead).
    now_ok = skew_end + timedelta(seconds=30)
    snap2['updated_at'] = now_ok.isoformat()
    obs2 = build_liquidity_observation(snap2, needed.isoformat(), now=now_ok, epic='GOLD')
    assert obs2['ok'] is True
    assert obs2['series_used']
    assert obs2['series_used'][0]['observation_time'] is not None
    assert obs2['series_used'][0]['skew_sec'] == 60
    assert obs2['series_used'][0]['within_skew'] is True


def test_availability_flags_missing_not_observed_neutral():
    """Item 13: missing DOM/confirm get flags; zero placeholder ≠ observed neutral."""
    # Primary only — no confirm series, no book.
    end = BASE
    start = end - timedelta(minutes=39)
    bars = _vol_bars(start=start, end=end)
    snap = {
        'ok': True, 'epic': 'GOLD', 'updated_at': BASE.isoformat(),
        'routes': {'label': 'METAL_GOLD', 'yahoo': ['GC=F'], 'mapped': True},
        'series': [{'symbol': 'GC=F', 'source': 'yahoo_finance', 'bars': bars}],
        'book': None, 'sources': ['yahoo:GC=F'], 'errors': [],
    }
    obs = build_liquidity_observation(snap, BASE.isoformat(), now=BASE, epic='GOLD')
    assert obs['ok'] is True
    avail = obs['availability']
    assert avail['primary_volume'] is True
    assert avail['confirm_volume'] is False
    assert avail['dom_book'] is False
    # Feature slots still finite for neural dim stability, but flags mark absence.
    assert obs['features'][1] == 0.0  # confirm placeholder
    assert obs['features'][3] == 0.0  # book placeholder
    assert obs['coverage'] < 1.0
    assert obs['book']['available'] is False

    # Observed near-neutral confirm must set availability True (contrast with missing).
    snap2 = _snapshot(bar=BASE, with_book=True, extra_vol=0.0)
    obs2 = build_liquidity_observation(snap2, BASE.isoformat(), now=BASE, epic='GOLD')
    assert obs2['availability']['confirm_volume'] is True
    assert obs2['availability']['dom_book'] is True
    assert obs2['features'][3] == 0.25  # real imbalance, not missing-zero


def test_substitute_labeling_paxg_not_capital_gold_book():
    """Item 12: PAXG book must not be labeled as Capital GOLD book."""
    snap = _snapshot(bar=BASE, epic='GOLD', with_book=True)
    obs = build_liquidity_observation(snap, BASE.isoformat(), now=BASE, epic='GOLD')
    assert obs['ok'] is True
    book = obs['book']
    assert book['available'] is True
    assert book['instrument'] == 'PAXG-USD'
    assert book['exchange'] == 'Coinbase'
    assert book['source'] == 'coinbase'
    assert book['relation'] == 'substitute'
    assert book['capital_instrument'] == 'GOLD'
    label = book['label']
    assert 'PAXG' in label and 'Coinbase' in label
    assert 'substitute' in label.lower()
    assert 'not Capital' in label
    assert 'Capital GOLD book' not in label

    primary = obs['series_used'][0]
    assert primary['instrument'] == 'GC=F'
    assert primary['exchange'] == 'CME'
    assert primary['relation'] == 'substitute'
    assert primary['source'] == 'yahoo_finance'
    assert primary['observation_time'] is not None
    assert obs['coverage'] == 1.0


def test_fusion_gap_reason_names_liquidity_block(tmp_path):
    """Item 14 helper: diagnose which EXTERNAL source blocks fusion."""
    from tests.test_neural_external_fusion import market_state, calendar
    cal = tmp_path / 'calendar.json'
    calendar(cal, when=BASE + timedelta(hours=1))
    raw = json.loads(cal.read_text())
    raw['updated_at'] = BASE.isoformat()
    cal.write_text(json.dumps(raw), encoding='utf-8')
    base = {'ready': True, 'bar_time': BASE.isoformat(), 'features': [0.05] * FEATURE_DIM}
    peers = market_state(bar=BASE)

    # Missing liquidity path.
    assert fusion_gap_reason(
        base, peers, 'GOLD', cal, ['USD'], now=BASE, liquidity_path=None
    ).startswith('liquidity:')

    # Stale observation on disk.
    liq = tmp_path / 'liquidity_snapshot.json'
    stale = _snapshot(bar=BASE - timedelta(minutes=15), with_book=False,
                      series_end=BASE - timedelta(minutes=15), updated_at=BASE)
    liq.write_text(json.dumps(stale), encoding='utf-8')
    reason = fusion_gap_reason(
        base, peers, 'GOLD', cal, ['USD'], now=BASE, liquidity_path=liq)
    assert reason.startswith('liquidity:')

    # Healthy snapshot → no gap.
    liq.write_text(json.dumps(_snapshot(bar=BASE, epic='GOLD')), encoding='utf-8')
    assert fusion_gap_reason(
        base, peers, 'GOLD', cal, ['USD'], now=BASE, liquidity_path=liq) is None


def test_fusion_gap_blocks_new_entry_not_open_protection(tmp_path):
    """Item 14: after fusion transition, gap blocks entry; open position still managed."""
    from app.engine import SingleTradeEngine, AdaptiveBrain, Store

    st = Store(tmp_path / 'db.sqlite3')
    brain = AdaptiveBrain(st, data_dir=tmp_path)
    eng = SingleTradeEngine(st, brain)
    eng.mandate = {'epic': 'GOLD', 'size': '1'}
    eng.market_context = {
        'ready': True, 'bar_time': BASE.isoformat(), 'trend': 'RANGE',
        'regime': 'RANGE', 'features': [0.0] * FEATURE_DIM, 'close': 4000.0,
    }
    # Simulate completed fusion-model transition.
    brain.feature_schema = brain.fusion_schema
    brain.core.d = brain.fusion_dim
    eng.latest_fusion_ready = False
    eng.fusion_block_reason = 'liquidity: no fresh volume observation for target minute'
    eng.last_closed_step = 0
    eng.armed = True
    eng.execution_enabled = True
    eng.last_signal = {'scores': [0.9, 0.05, 0.05], 'action': 'BUY'}  # stale

    assert eng._fusion_entry_blocked() is True
    ev = eng._entry_evaluation()
    assert ev['action'] == 'HOLD'
    assert 'fusion gap' in ev['reason'].lower()
    assert 'liquidity' in ev['reason'].lower()
    snap = eng.snapshot()
    assert snap['entry_can_submit_now'] is False
    assert 'liquidity' in (snap['entry_block_reason'] or '').lower()
    assert snap['fusion_block_reason'].startswith('liquidity:')

    # Open position: protection path is independent — fusion gap does not disarm.
    eng.positions = [{'dealId': 'd1', 'direction': 'BUY', 'epic': 'GOLD',
                      'level': 4000.0, 'size': '1', 'stopLevel': 3990.0}]
    assert eng._fusion_entry_blocked() is True  # still blocks *new* entry
    assert eng.armed and eng.execution_enabled
    # Snapshot reports existing position (not fusion) as the flat-entry N/A reason.
    assert eng.snapshot()['entry_block_reason'] == 'Existing open position'


def test_max_observation_age_constants_cover_venues():
    assert MAX_OBSERVATION_AGE_SEC['yahoo_futures'] <= 180
    assert MAX_OBSERVATION_AGE_SEC['coinbase'] <= 180
    assert ALLOWED_BAR_SKEW_SEC['coinbase'] == 0
    assert ALLOWED_BAR_SKEW_SEC['yahoo_futures'] >= 0
