"""Instrument-independent, explicitly sourced external-event safety gate.

No economic calendar is bundled. When enabled, missing/stale calendar data
blocks NEW entries; existing positions remain managed by the engine.
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _utc(value):
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return dt.astimezone(timezone.utc)


def assess_news(epic, path, *, now=None, instrument_currencies=None):
    """Return explicit entry permission from a user-provided calendar snapshot.

    Config format: {enabled: bool, updated_at: ISO8601, max_age_hours: 24,
      before_minutes: 30, after_minutes: 30,
      events: [{at: ISO8601, impact: HIGH, epics: [exact epic or '*'],
                name: '...', currencies: [...] }],
      instrument_currencies: {EPIC: ['USD', 'EUR']}}
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    current = current.astimezone(timezone.utc)
    path = Path(path)
    if not path.exists():
        return {'enabled': False, 'entry_allowed': True,
                'status': 'UNCONFIGURED', 'reason': 'No verified economic-calendar feed configured; news protection inactive'}
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValueError('calendar must be an object')
        if config.get('enabled') is not True:
            return {'enabled': False, 'entry_allowed': True,
                    'status': 'DISABLED', 'reason': 'Economic news protection explicitly disabled'}
        updated = _utc(config['updated_at'])
        max_age = float(config.get('max_age_hours', 24))
        before = float(config.get('before_minutes', 30))
        after = float(config.get('after_minutes', 30))
        if not (0 < max_age <= 168 and 0 <= before <= 1440 and 0 <= after <= 1440):
            raise ValueError('invalid safety window')
        if updated > current + timedelta(minutes=5) or current-updated > timedelta(hours=max_age):
            raise ValueError('economic calendar is stale or future-dated')
        events = config['events']
        if not isinstance(events, list):
            raise ValueError('events must be an array')
        currencies = (instrument_currencies if instrument_currencies is not None else
                      config.get('instrument_currencies', {}).get(epic, []))
        if not isinstance(currencies, list):
            raise ValueError('instrument currencies must be a list')
        if config.get('source') == 'Forex Factory weekly JSON':
            start = datetime.fromisoformat(config['coverage_start']).date()
            end = datetime.fromisoformat(config['coverage_end']).date()
            if not start <= current.date() < end:
                raise ValueError('weekly economic calendar does not cover today')
            if not currencies:
                return {'enabled':True,'entry_allowed':False,'status':'UNMAPPED_INSTRUMENT',
                        'reason':'Cannot map broker instrument to economic event currencies'}
        for event in events:
            if not isinstance(event, dict):
                raise ValueError('invalid event')
            at = _utc(event['at'])
            if str(event.get('impact', '')).upper() != 'HIGH':
                continue
            targets = event.get('epics', [])
            affected = event.get('currencies', [])
            if not isinstance(targets, list) or not isinstance(affected, list):
                raise ValueError('invalid event targeting')
            if epic not in targets and '*' not in targets and not set(currencies).intersection(affected) and 'ALL' not in affected:
                continue
            if at-timedelta(minutes=before) <= current <= at+timedelta(minutes=after):
                return {'enabled': True, 'entry_allowed': False, 'status': 'NEWS_WINDOW',
                        'reason': 'High-impact event: '+str(event.get('name', 'Unnamed event'))[:100],
                        'event_at': at.isoformat()}
        return {'enabled': True, 'entry_allowed': True, 'status': 'CLEAR',
                'reason': 'Verified calendar has no matching high-impact event in safety window'}
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
        return {'enabled': True, 'entry_allowed': False, 'status': 'CALENDAR_UNAVAILABLE',
                'reason': 'Economic calendar unavailable or invalid: '+str(exc)[:160]}
