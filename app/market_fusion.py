"""Time-aligned cross-market and scheduled-news features for neural learning.

Every source is inspected for quality; unavailable data is never represented by
an invented neutral market. Feature numbers are bounded and dimensionless.
These observations are correlations and event timing, NOT causal predictions.
"""
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .external_risk import _utc, assess_news
from .market_context import FEATURE_NAMES, candle_time

FUSION_SCHEMA = 'closed_m1_m5_m15_h1_intermarket_news_shadow_v5'
EXTERNAL_NAMES = (
    'peer_corr_weighted_return_1m',
    'peer_corr_weighted_return_5m',
    'peer_corr_weighted_return_15m',
    'peer_direction_agreement_5m',
    'peer_mean_abs_correlation',
    'peer_stable_coverage',
    'high_impact_event_ahead_6h',
    'high_impact_event_after_2h',
)
FUSION_NAMES = FEATURE_NAMES + EXTERNAL_NAMES
FUSION_DIM = len(FUSION_NAMES)


def peer_features(state, bar_time):
    """Six comparable aggregates, built exclusively from completed aligned bars."""
    if not isinstance(state, dict) or state.get('status') != 'READY':
        return None
    timestamp = candle_time(bar_time)
    if timestamp is None:
        return None
    ready = []
    for p in state.get('peers', []):
        if not isinstance(p, dict) or not p.get('ready'):
            continue
        if candle_time(p.get('last_completed_bar')) != timestamp:
            continue
        try:
            data = [float(p[k]) for k in ('correlation', 'holdout_correlation',
                                           'peer_return_1m_bps', 'peer_return_5m_bps',
                                           'peer_return_15m_bps')]
        except (ValueError, TypeError, KeyError):
            continue
        if all(math.isfinite(x) for x in data):
            ready.append((p, data))
    if not ready:
        return None
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
    """Known scheduled event proximity only; never train on actual surprise data."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    current = current.astimezone(timezone.utc)
    guard = assess_news(epic, calendar_path, now=current, instrument_currencies=currencies)
    if not guard.get('enabled') or guard.get('status') not in ('CLEAR','NEWS_WINDOW'):
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
        return out if all(math.isfinite(x) and 0<=x<=1 for x in out) else None
    except (KeyError, TypeError, ValueError, OSError):
        return None


def fusion_features(base, intermarket, epic, calendar_path, currencies, *, now=None):
    """Return complete v4 features or None; never silently fill missing data."""
    if not isinstance(base,dict) or not base.get('ready'):
        return None
    peers = peer_features(intermarket, base.get('bar_time'))
    news = news_features(epic,calendar_path,currencies,now=now)
    if peers is None or news is None:
        return None
    values = list(base['features']) + peers + news
    return values if len(values)==FUSION_DIM and all(math.isfinite(x) for x in values) else None
