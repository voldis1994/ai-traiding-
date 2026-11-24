"""Time-aligned cross-market and scheduled-news features for neural learning.

Every source is inspected for quality; unavailable data is never represented by
an invented neutral market. Feature numbers are bounded and dimensionless.
These observations are correlations and event timing, NOT causal predictions.

Fusion length = OHLC (base ± approved dynamic) + EXTERNAL_NAMES.
Fixed 18-dim is the empty-registry default only (13 OHLC + 8 external today).
"""
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .external_risk import _utc, assess_news
from .liquidity_feed import (
    LIQUIDITY_NAMES, liquidity_features_from_path, liquidity_observation_from_path,
)
from .market_context import FEATURE_NAMES, FEATURE_DIM, candle_time

# Bumped with OHLC structure channels (market_context shadow_v5).
FUSION_SCHEMA = 'closed_m1_m5_m15_h1_intermarket_news_liquidity_shadow_v7'
EXTERNAL_NAMES = (
    'peer_corr_weighted_return_1m',
    'peer_corr_weighted_return_5m',
    'peer_corr_weighted_return_15m',
    'peer_direction_agreement_5m',
    'peer_mean_abs_correlation',
    'peer_stable_coverage',
    'high_impact_event_ahead_6h',
    'high_impact_event_after_2h',
) + LIQUIDITY_NAMES
FUSION_NAMES = FEATURE_NAMES + EXTERNAL_NAMES
FUSION_DIM = len(FUSION_NAMES)  # default empty-registry size; runtime may grow

# V3 D08 — last-good carry must keep original observed_at; never refresh age on re-read.
# Design defaults (seconds); operator may override via stamp max_age.
DEFAULT_LAST_GOOD_TTL = {
    'peers': 120.0,
    'news': 24 * 3600.0,
    'liquidity': 10.0,
}


def stamp_last_good(values, *, source, now=None, max_age=None):
    """Wrap a live EXTERNAL vector with provenance for TTL checks (V3 D08)."""
    if values is None:
        return None
    observed = now if now is not None else datetime.now(timezone.utc)
    if getattr(observed, 'tzinfo', None) is None:
        observed = observed.replace(tzinfo=timezone.utc)
    ttl = max_age if max_age is not None else DEFAULT_LAST_GOOD_TTL.get(source, 120.0)
    return {
        'values': [float(x) for x in values],
        'source': str(source),
        'observed_at': observed.astimezone(timezone.utc).isoformat(),
        'max_age': float(ttl),
        'quality': 'live',
    }


def last_good_ttl_expired(stamp, *, now=None, source=None):
    """True when a stamped last-good vector is missing or older than max_age."""
    if stamp is None:
        return True
    if isinstance(stamp, (list, tuple)):
        # Legacy unstamped vector — treat as expired (must re-observe).
        return True
    if not isinstance(stamp, dict) or stamp.get('values') is None:
        return True
    if source is not None and stamp.get('source') not in (None, source):
        return True
    raw = stamp.get('observed_at')
    try:
        observed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    current = now if now is not None else datetime.now(timezone.utc)
    if getattr(current, 'tzinfo', None) is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    max_age = stamp.get('max_age')
    if max_age is None:
        max_age = DEFAULT_LAST_GOOD_TTL.get(stamp.get('source') or source or 'peers', 120.0)
    try:
        max_age = float(max_age)
    except (TypeError, ValueError):
        return True
    return age > max_age


def last_good_values(stamp, *, now=None, source=None):
    """Return values only if stamp is present and within TTL; else None."""
    if last_good_ttl_expired(stamp, now=now, source=source):
        return None
    if isinstance(stamp, dict):
        vals = stamp.get('values')
        return list(vals) if isinstance(vals, (list, tuple)) else None
    return None


def peer_features(state, bar_time, *, max_lag_seconds=0):
    """Six comparable aggregates, built exclusively from completed aligned bars.

    Default is exact bar match — ``abs()`` lag windows allowed future peer bars
    (lookahead in research) and conflicted with fusion tests that reject a
    60s-stale peer. LIVE one-bar peer gaps use ``peers_fallback`` carry-forward
    instead of inventing alignment. ``max_lag_seconds`` only accepts *past*
    peers (peer_t ≤ bar_time), never future.
    """
    if not isinstance(state, dict) or state.get('status') != 'READY':
        return None
    timestamp = candle_time(bar_time)
    if timestamp is None:
        return None
    lag = max(0, int(max_lag_seconds))
    ready = []
    for p in state.get('peers', []):
        if not isinstance(p, dict) or not p.get('ready'):
            continue
        peer_t = candle_time(p.get('last_completed_bar'))
        if peer_t is None:
            continue
        # Past-only: reject future peers (lookahead). Exact match when lag=0.
        age = (timestamp - peer_t).total_seconds()
        if age < -1e-9:
            continue
        if age > lag + 1e-9:
            continue
        try:
            data = [float(p[k]) for k in ('correlation', 'holdout_correlation',
                                           'peer_return_1m_bps', 'peer_return_5m_bps',
                                           'peer_return_15m_bps')]
        except (ValueError, TypeError, KeyError):
            continue
        if all(math.isfinite(x) for x in data):
            ready.append((p, data, age))
    if not ready:
        return None
    # Prefer the tightest-aligned peers when several qualify.
    ready.sort(key=lambda x: x[2])
    ready = [(p, data) for p, data, _ in ready]
    trusted = [(p, v) for p,v in ready if p.get('stable') and v[0]*v[1]>0]
    # Correlation sign has been fitted and validated using earlier observations.
    weighted = []
    for j in (2, 3, 4):
        values = [v[0]*v[j] for _,v in trusted]
        denom = sum(abs(v[0]) for _,v in trusted)
        weighted.append(max(-10., min(10., sum(values)/denom/10.)) if denom else 0.)
    agreement = (sum(math.copysign(1., v[0])*math.copysign(1., v[3])
                    for _,v in trusted if abs(v[3])>=.01) / len(trusted)) if trusted else 0.
    corr = sum(abs(v[0]) for _,v in trusted)/len(trusted) if trusted else 0.
    # Coverage is a quality input, not a pretext to fabricate correlations.
    coverage = min(1.,len(trusted)/3.)  # three discovery slots per instrument
    out = weighted + [agreement, corr, coverage]
    return out if all(math.isfinite(x) for x in out) else None


def news_features(epic, calendar_path, currencies, *, now=None):
    """Known scheduled event proximity only; never train on actual surprise data.

    When the calendar feed is rate-limited / stale, return neutral [0,0] so
    fusion does not freeze LIVE OPEN (operator: calendar soft-fail).
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    current = current.astimezone(timezone.utc)
    guard = assess_news(epic, calendar_path, now=current, instrument_currencies=currencies)
    status = guard.get('status')
    if not guard.get('enabled') or status not in ('CLEAR','NEWS_WINDOW'):
        # Soft degrade — missing/stale/rate-limited calendar must not gap fusion.
        if status in ('CALENDAR_UNAVAILABLE','UNCONFIGURED','DISABLED','CALENDAR_WAITING',
                      'CALENDAR_FEED_ERROR','CALENDAR_FEED_DEGRADED',None):
            return [0., 0.]
        return None
    try:
        config = json.loads(Path(calendar_path).read_text(encoding='utf-8'))
        ahead = after = 0.
        for event in config['events']:
            if str(event.get('impact','')).upper()!='HIGH':
                continue
            targets = event.get('epics',[])
            affected = event.get('currencies',[])
            if (epic not in targets and '*' not in targets
                    and not set(currencies).intersection(affected) and 'ALL' not in affected):
                continue
            remaining = (_utc(event['at'])-current).total_seconds()/60.
            if 0 <= remaining <= 360:
                ahead=max(ahead, 1.-remaining/360.)
            if -120 <= remaining < 0:
                after=max(after, 1.+remaining/120.)
        out = [ahead,after]
        return out if all(math.isfinite(x) and 0<=x<=1 for x in out) else [0., 0.]
    except (KeyError, TypeError, ValueError, OSError):
        return [0., 0.]


def _liquidity_vector_ok(values):
    if not isinstance(values, (list, tuple)) or len(values) != len(LIQUIDITY_NAMES):
        return False
    return all(math.isfinite(float(x)) for x in values)


def _peer_vector_ok(values):
    if not isinstance(values, (list, tuple)) or len(values) != 6:
        return False
    return all(math.isfinite(float(x)) for x in values)


def _news_vector_ok(values):
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        return False
    return all(math.isfinite(float(x)) and 0 <= float(x) <= 1 for x in values)


def fusion_gap_reason(base, intermarket, epic, calendar_path, currencies, *, now=None,
                      expected_ohlc_dim=None, liquidity_path=None,
                      liquidity_fallback=None, peers_fallback=None,
                      news_fallback=None):
    """Which EXTERNAL source blocks a fused decision after fusion transition.

    Returns None when fusion can be built; otherwise a short operator-facing
    reason naming the blocking feed (peers / news / liquidity / OHLC dim).

    Fallbacks (last good vectors) keep OPEN from freezing on a one-bar peer lag
    or calendar refresh — values are carried, never invented from zeros.
    """
    if not isinstance(base, dict) or not base.get('ready'):
        return 'ohlc: market context not ready'
    ohlc = list(base.get('features') or [])
    need = FEATURE_DIM if expected_ohlc_dim is None else int(expected_ohlc_dim)
    if len(ohlc) != need:
        return f'ohlc: feature dim {len(ohlc)} != expected {need}'
    peers = peer_features(intermarket, base.get('bar_time'))
    peers_fb = last_good_values(peers_fallback, now=now, source='peers')
    if peers is None and not _peer_vector_ok(peers_fb):
        status = (intermarket or {}).get('status') if isinstance(intermarket, dict) else None
        detail = (intermarket or {}).get('reason') if isinstance(intermarket, dict) else None
        ttl_note = ''
        if peers_fallback is not None and last_good_ttl_expired(
                peers_fallback, now=now, source='peers'):
            ttl_note = ' (last-good TTL expired)'
        return 'peers: ' + (detail or status or 'aligned peer features unavailable') + ttl_note
    news = news_features(epic, calendar_path, currencies, now=now)
    news_fb = last_good_values(news_fallback, now=now, source='news')
    if news is None and not _news_vector_ok(news_fb):
        ttl_note = ''
        if news_fallback is not None and last_good_ttl_expired(
                news_fallback, now=now, source='news'):
            ttl_note = ' (last-good TTL expired)'
        return 'news: calendar features unavailable or not CLEAR/NEWS_WINDOW' + ttl_note
    liq_fb = last_good_values(liquidity_fallback, now=now, source='liquidity')
    if liquidity_path is None and not _liquidity_vector_ok(liq_fb):
        return 'liquidity: path not configured'
    obs = None
    if liquidity_path is not None:
        obs = liquidity_observation_from_path(
            liquidity_path, base.get('bar_time'), now=now, epic=epic)
    live_ok = bool(obs and obs.get('ok') and obs.get('features') is not None)
    if not live_ok and not _liquidity_vector_ok(liq_fb):
        ttl_note = ''
        if liquidity_fallback is not None and last_good_ttl_expired(
                liquidity_fallback, now=now, source='liquidity'):
            ttl_note = ' (last-good TTL expired)'
        return 'liquidity: ' + ((obs or {}).get('reason')
                                or 'volume/DOM observation unavailable') + ttl_note
    return None


def fusion_features(base, intermarket, epic, calendar_path, currencies, *, now=None,
                    expected_ohlc_dim=None, fusion_schema=None, liquidity_path=None,
                    liquidity_fallback=None, peers_fallback=None, news_fallback=None):
    """Return complete fusion vector or None.

    Layout (must match NeuralCore columns): [BASE_OHLC | DYNAMIC | EXTERNAL].
    `base['features']` is already BASE+DYNAMIC from market_context; peers+news+
    public gold liquidity are appended as EXTERNAL — never interleaved.

    `expected_ohlc_dim`: when registry has dynamic features, base['features'] must
    match that length. Default = FEATURE_DIM (static base only).

    When the live minute has no fresh volume/DOM/peers/news, last-good fallbacks
    are used so OPEN is not frozen waiting for a late feed. OHLC is never invented.
    """
    if not isinstance(base,dict) or not base.get('ready'):
        return None
    peers = peer_features(intermarket, base.get('bar_time'))
    if peers is None:
        peers_fb = last_good_values(peers_fallback, now=now, source='peers')
        if _peer_vector_ok(peers_fb):
            peers = [float(x) for x in peers_fb]
    news = news_features(epic,calendar_path,currencies,now=now)
    if news is None:
        news_fb = last_good_values(news_fallback, now=now, source='news')
        if _news_vector_ok(news_fb):
            news = [float(x) for x in news_fb]
    if peers is None or news is None:
        return None
    liq = None
    if liquidity_path is not None:
        liq = liquidity_features_from_path(
            liquidity_path, base.get('bar_time'), now=now, epic=epic)
    if liq is None:
        liq_fb = last_good_values(liquidity_fallback, now=now, source='liquidity')
        if _liquidity_vector_ok(liq_fb):
            liq = [float(x) for x in liq_fb]
    if liq is None:
        return None
    ohlc = list(base['features'])
    need = FEATURE_DIM if expected_ohlc_dim is None else int(expected_ohlc_dim)
    if len(ohlc)!=need:
        return None
    # EXTERNAL only at the tail — same order expand_inputs uses when appending.
    values = ohlc + list(peers) + list(news) + list(liq)
    if len(values)!=need+len(EXTERNAL_NAMES):
        return None
    if not all(math.isfinite(x) for x in values):
        return None
    return values


def fusion_liquidity_tail(fused, *, ohlc_dim=None):
    """Extract liquidity EXTERNAL tail from a fused vector (for carry-forward)."""
    if not isinstance(fused, (list, tuple)):
        return None
    need = FEATURE_DIM if ohlc_dim is None else int(ohlc_dim)
    start = need + (len(EXTERNAL_NAMES) - len(LIQUIDITY_NAMES))
    end = start + len(LIQUIDITY_NAMES)
    if len(fused) < end:
        return None
    tail = list(fused[start:end])
    return tail if _liquidity_vector_ok(tail) else None


def fusion_peers_tail(fused, *, ohlc_dim=None):
    """Extract peer EXTERNAL block (6 dims) for carry-forward."""
    if not isinstance(fused, (list, tuple)):
        return None
    need = FEATURE_DIM if ohlc_dim is None else int(ohlc_dim)
    start = need
    end = start + 6
    if len(fused) < end:
        return None
    tail = list(fused[start:end])
    return tail if _peer_vector_ok(tail) else None


def fusion_news_tail(fused, *, ohlc_dim=None):
    """Extract news EXTERNAL block (2 dims) for carry-forward."""
    if not isinstance(fused, (list, tuple)):
        return None
    need = FEATURE_DIM if ohlc_dim is None else int(ohlc_dim)
    start = need + 6
    end = start + 2
    if len(fused) < end:
        return None
    tail = list(fused[start:end])
    return tail if _news_vector_ok(tail) else None


def fusion_dim_for(ohlc_dim):
    return int(ohlc_dim)+len(EXTERNAL_NAMES)
