"""Mistake-audit + scenario memory + promote-to-LIVE gate (SHADOW-visible).

Operator loop (not blind £ chase):
  1. diagnose — OHLC / zones / news / intermarket vs outcome
  2. save scenario fingerprint + mistake class
  3. propose targeted fix (only knobs that match the mistake)
  4. commit fix after N repeats of the same fingerprint+mistake
  5. mark ready_for_live after proof wins post-fix

LIVE still runs ohlc_scenarios.audit_close for mild immediate learning;
this layer adds delayed surgical commits + scenario promote memory.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

from . import ohlc_scenarios
from . import structure_memory

STORE_KEY = 'shadow_mistake_audit_v1'
HIST_KEY = 'shadow_mistake_audit_v1:hist'
SCENARIOS_KEY = 'shadow_mistake_audit_v1:scenarios'
READY_KEY = 'shadow_mistake_audit_v1:ready'
LAST_KEY = 'shadow_mistake_audit_v1:last'
PENDING_FIX_KEY = 'shadow_mistake_audit_v1:pending_fix'

# Loss → diagnose → change knobs → re-test same situation next time.
# One repeat is enough: neural must improve itself without waiting for 3 losses.
PROMOTE_N = 1   # first same fingerprint+mistake commits the targeted fix
READY_N = 1     # first proof win after fix marks ready_for_live


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _clip01(v):
    n = _finite(v)
    if n is None:
        return 0.0
    return max(0.0, min(1.0, float(n)))


def _fp_news_bucket(status) -> str:
    """Fingerprint news without waiting on calendar boot.

    UNCONFIGURED / WAITING / CALENDAR_UNAVAILABLE used to mint a new scenario
    class every boot, so SHADOW looked stuck on "special events" instead of
    grouping real Capital closes.
    """
    s = str(status or '').strip().upper()
    if not s or s in ('UNCONFIGURED', 'WAITING', 'CALENDAR_UNAVAILABLE',
                      'NONE', '—', '-', 'NEWS?'):
        return 'NEWS_NA'
    if s in ('CLEAR', 'OK', 'READY', 'ALLOW'):
        return 'NEWS_CLEAR'
    if s in ('NEWS_WINDOW', 'HIGH_IMPACT') or 'BLOCK' in s:
        return 'NEWS_BLOCK'
    return s[:24]


def _fp_peers_bucket(status) -> str:
    """Fingerprint peers without waiting on intermarket READY."""
    s = str(status or '').strip().upper()
    if not s or s in ('WAITING', 'UNCONFIGURED', 'NONE', '—', '-', 'IM?'):
        return 'PEERS_NA'
    if s in ('READY', 'OK', 'CLEAR', 'ALLOW'):
        return 'PEERS_OK'
    if s in ('CONTRADICTION', 'FIGHT', 'VETO') or 'BLOCK' in s:
        return 'PEERS_FIGHT'
    return s[:24]


def fingerprint(*, scenario=None, zone_side=None, news_status=None,
                intermarket_status=None, direction=None, trigger=None) -> str:
    """Stable short id for a market-situation class (not a trade id)."""
    parts = [
        str(scenario or 'UNK'),
        str(zone_side or 'mid'),
        _fp_news_bucket(news_status),
        _fp_peers_bucket(intermarket_status),
        str((direction or '?')).upper(),
        str(trigger or 'trig?'),
    ]
    raw = '|'.join(parts)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]


def _zone_side(zone, mid=None) -> str:
    if not isinstance(zone, dict):
        return 'none'
    lo = _finite(zone.get('lo'))
    hi = _finite(zone.get('hi'))
    m = _finite(mid if mid is not None else zone.get('mid'))
    if lo is None or hi is None or not (hi > lo):
        return 'none'
    if m is None:
        return 'box'
    width = hi - lo
    if m <= lo + 0.35 * width:
        return 'demand'
    if m >= hi - 0.35 * width:
        return 'supply'
    return 'mid'


def snapshot_context(*, candles=None, scenario_meta=None, ohlc_meta=None,
                     news=None, intermarket=None, direction=None,
                     mid=None, store=None) -> dict:
    """Build the full decision context used for diagnose + fingerprint."""
    meta_s = scenario_meta if isinstance(scenario_meta, dict) else {}
    meta_o = ohlc_meta if isinstance(ohlc_meta, dict) else {}
    news = news if isinstance(news, dict) else {}
    intermarket = intermarket if isinstance(intermarket, dict) else {}
    path = structure_memory.normalise(candles, limit=80) if candles else []
    live_sc = ohlc_scenarios.classify(path, store=store) if path else {'ready': False}
    stamped = str(meta_s.get('scenario') or '')
    live_name = str(live_sc.get('scenario') or '') if live_sc.get('ready') else ''
    zone = meta_o.get('zone') if isinstance(meta_o.get('zone'), dict) else None
    if zone is None and isinstance(meta_o.get('entry_zone'), dict):
        zone = meta_o.get('entry_zone')
    trigger = meta_o.get('trigger') or _infer_trigger(meta_o)
    zside = _zone_side(zone, mid=mid)
    news_status = str(news.get('status') or 'UNCONFIGURED')
    im_status = str(intermarket.get('status') or 'WAITING')
    # Assess peer fight against the trade direction when peers are ready.
    # Peers still WAITING must NOT freeze diagnose — Capital closes analyse now.
    im_fight = False
    if direction in ('BUY', 'SELL') and im_status == 'READY':
        try:
            from .intermarket import IntermarketMonitor
            mon = IntermarketMonitor()
            mon.state = dict(intermarket)
            ass = mon.assess(direction)
            im_fight = not bool(ass.get('entry_allowed'))
            im_status = str(ass.get('status') or im_status)
        except Exception:
            pass
    news_block = (
        news.get('entry_allowed') is False
        or news_status in ('NEWS_WINDOW', 'HIGH_IMPACT')
    )
    fp = fingerprint(
        scenario=stamped or live_name or 'UNK',
        zone_side=zside,
        news_status=news_status,
        intermarket_status=im_status,
        direction=direction,
        trigger=trigger,
    )
    return {
        'fingerprint': fp,
        'stamped_scenario': stamped,
        'live_scenario': live_name,
        'zone_side': zside,
        'trigger': trigger,
        'news_status': news_status,
        'news_block': bool(news_block),
        'news_reason': news.get('reason'),
        'intermarket_status': im_status,
        'intermarket_fight': bool(im_fight),
        'direction': (direction or '').upper() or None,
        'zone': zone,
        'path_n': len(path),
        'live_sc': live_sc if live_sc.get('ready') else None,
    }


def _infer_trigger(ohlc_meta) -> str | None:
    """Recover break|zone when entry meta omitted trigger but left clues."""
    meta = ohlc_meta if isinstance(ohlc_meta, dict) else {}
    trig = str(meta.get('trigger') or '').lower()
    if trig in ('break', 'zone'):
        return trig
    reason = str(meta.get('reason') or '').lower()
    if 'break' in reason or str(meta.get('break_reason') or ''):
        return 'break'
    if isinstance(meta.get('zone'), dict) or isinstance(meta.get('entry_zone'), dict):
        return 'zone'
    return None


def _ready_stamped_now(ready) -> bool:
    """True only when this close actually stamped ready_for_live."""
    return bool(
        isinstance(ready, dict)
        and ready.get('ready_at')
        and not ready.get('need')
        and not ready.get('already'))


def diagnose(*, pnl, spread=0.0, candles=None, scenario_meta=None,
             ohlc_meta=None, news=None, intermarket=None, direction=None,
             mid=None, entry=None, store=None, source='shadow') -> dict:
    """Classify mistakes from full context — settings changes are separate."""
    pnl_n = _finite(pnl)
    if pnl_n is None:
        return {'audited': False, 'reason': 'no_pnl'}
    cost = abs(_finite(spread) or 0.0)
    hurt = pnl_n < -max(0.02, 0.25 * cost)
    useful = pnl_n > max(0.02, 0.25 * cost)
    ctx = snapshot_context(
        candles=candles, scenario_meta=scenario_meta, ohlc_meta=ohlc_meta,
        news=news, intermarket=intermarket, direction=direction,
        mid=mid if mid is not None else entry, store=store)
    action = (direction or (ohlc_meta or {}).get('action') or '').upper()
    mistakes = []
    notes = []
    stamped = ctx['stamped_scenario']
    live = ctx['live_scenario']
    zone = ctx.get('zone')
    path = structure_memory.normalise(candles, limit=80) if candles else []

    if zone and path and action in ('BUY', 'SELL') and hurt:
        lo = _finite(zone.get('lo'))
        hi = _finite(zone.get('hi'))
        if lo is not None and hi is not None:
            if action == 'BUY' and any(b['close'] < lo for b in path[-8:]):
                mistakes.append('zone_fail')
                notes.append('price_closed_through_demand')
            if action == 'SELL' and any(b['close'] > hi for b in path[-8:]):
                mistakes.append('zone_fail')
                notes.append('price_closed_through_supply')

    # Prefer OPEN stamp; fall back to live regime so CMD matches diagnose.
    # COMPRESSION is a tight RANGE — same mid-box / blind-break class.
    regime = stamped or live
    trig = ctx.get('trigger') or _infer_trigger(ohlc_meta)

    if stamped.startswith('TREND') and live.startswith('CHOCH') and hurt:
        mistakes.append('scenario_fail')
        notes.append(f'trend_stamped_but_{live}')
    # Fakeout: OPEN stamped FAKEOUT, live flipped to FAKEOUT, or break→fake.
    # Desk often stamps FAKEOUT already — old rule only caught BREAKOUT→FAKEOUT
    # and every FAKEOUT £ loss fell through to outcome_hurt (same 4 nudges).
    if hurt and (
            stamped == 'FAKEOUT'
            or live == 'FAKEOUT'
            or (stamped.startswith('BREAKOUT') and live == 'FAKEOUT')):
        if 'fakeout_missed' not in mistakes:
            mistakes.append('fakeout_missed')
        if stamped == 'FAKEOUT':
            notes.append('fakeout_stamp_loss')
        elif stamped.startswith('BREAKOUT') and live == 'FAKEOUT':
            notes.append('breakout_was_fakeout')
        else:
            notes.append('live_fakeout_loss')
    if regime in ('RANGE', 'COMPRESSION') and trig == 'break' and hurt:
        mistakes.append('mid_range_mistake')
        notes.append('blind_break_in_range')
        if not stamped and live in ('RANGE', 'COMPRESSION'):
            notes.append('regime_from_live_classify')
    if (stamped.startswith('RETEST') or live.startswith('RETEST')) and hurt:
        if 'zone_fail' not in mistakes and 'retest_fail' not in mistakes:
            mistakes.append('retest_fail')
            notes.append('retest_did_not_hold')
    # CHOCH / TREND stamped losses without a finer tag → scenario_fail (OHLC
    # penalty_trend), not the catch-all self_params quartet.
    if hurt and not mistakes:
        if stamped.startswith('CHOCH') or live.startswith('CHOCH'):
            mistakes.append('scenario_fail')
            notes.append('choch_stamp_loss')
        elif stamped.startswith('TREND') or (
                not stamped and live.startswith('TREND')):
            mistakes.append('scenario_fail')
            notes.append('trend_stamp_loss')
        elif stamped.startswith('BREAKOUT') or (
                not stamped and live.startswith('BREAKOUT')):
            mistakes.append('fakeout_missed')
            notes.append('breakout_stamp_loss')
        elif regime in ('RANGE', 'COMPRESSION'):
            mistakes.append('mid_range_mistake')
            notes.append('range_stamp_loss')

    if ctx.get('news_block') and hurt:
        mistakes.append('news_risk_ignored')
        notes.append(f"news_{ctx.get('news_status')}")
    if ctx.get('intermarket_fight') and hurt:
        mistakes.append('intermarket_fight')
        notes.append(f"peers_{ctx.get('intermarket_status')}")

    if not mistakes and useful:
        return {
            'audited': True, 'mistakes': [], 'notes': ['analysis_ok'],
            'reason': 'analysis_ok', 'hurt': False, 'useful': True,
            'pnl': pnl_n, 'source': source, 'context': ctx,
            'proposed_fix': None,
        }
    if not mistakes and not hurt and not useful:
        return {
            'audited': True, 'mistakes': [], 'notes': ['scratch'],
            'reason': 'scratch', 'hurt': False, 'useful': False,
            'pnl': pnl_n, 'source': source, 'context': ctx,
            'proposed_fix': None,
        }
    if not mistakes and hurt:
        # Last resort only when regime/stamp truly unknown — never for FAKEOUT.
        mistakes.append('outcome_hurt')
        notes.append('loss_without_clear_structure_miss')
        notes.append('regime=' + (regime or 'UNK'))

    proposed = propose_fix(mistakes)
    return {
        'audited': True, 'mistakes': mistakes, 'notes': notes,
        'reason': 'mistakes' if mistakes else 'ok',
        'hurt': hurt, 'useful': useful, 'pnl': pnl_n,
        'source': source, 'context': ctx, 'proposed_fix': proposed,
    }


# Mistake → concrete knob path (store key + field). Desk CMD prints these.
_OHLC_DELTA_KEYS = (
    'penalty_zone', 'penalty_trend', 'penalty_range',
    'penalty_breakout', 'penalty_retest', 'fakeout_block_conf',
)
_OHLC_FLAG_KEYS = ('block_mid_range', 'prefer_retest_over_blind_break')
_AUDIT_DELTA_KEYS = ('news_block_weight', 'intermarket_veto_weight')
_AUDIT_FLAG_KEYS = ('respect_news_window', 'respect_intermarket_veto')
# Full-system soft levers (neural decide path) — not OHLC-only.
_SELF_DELTA_KEYS = (
    'structure_edge_nudge',
    'situation_memory_nudge',
    'forecast_bridge_nudge',
    'mtf_momentum_nudge',
)
_ALL_TRACKED_OHLC = _OHLC_DELTA_KEYS + _OHLC_FLAG_KEYS
_ALL_TRACKED_SELF = _SELF_DELTA_KEYS


def _knob_path(key: str) -> str:
    """Full storage path for a knob — what CMD shows as the change target."""
    if key in _OHLC_DELTA_KEYS or key in _OHLC_FLAG_KEYS:
        return f'ohlc_scenarios_v1.{key}'
    if key in _AUDIT_DELTA_KEYS or key in _AUDIT_FLAG_KEYS:
        return f'{STORE_KEY}.{key}'
    if key in _SELF_DELTA_KEYS:
        return f'self_params_v1.{key}'
    return f'unknown.{key}'


def propose_fix(mistakes) -> dict | None:
    """Map mistake classes → only the knobs that address them (not £ chase)."""
    if not mistakes:
        return None
    deltas = {}
    flags = {}
    routes = []  # concrete mistake → knob map for CMD
    for m in mistakes:
        if m == 'zone_fail':
            deltas['penalty_zone'] = deltas.get('penalty_zone', 0) + 0.08
            routes.append({'mistake': m, 'knob': 'penalty_zone', 'delta': 0.08,
                           'path': _knob_path('penalty_zone')})
        elif m == 'scenario_fail':
            deltas['penalty_trend'] = deltas.get('penalty_trend', 0) + 0.06
            routes.append({'mistake': m, 'knob': 'penalty_trend', 'delta': 0.06,
                           'path': _knob_path('penalty_trend')})
        elif m == 'fakeout_missed':
            deltas['penalty_breakout'] = deltas.get('penalty_breakout', 0) + 0.08
            deltas['fakeout_block_conf'] = deltas.get('fakeout_block_conf', 0) - 0.02
            flags['prefer_retest_over_blind_break'] = 1
            routes.append({'mistake': m, 'knob': 'penalty_breakout', 'delta': 0.08,
                           'path': _knob_path('penalty_breakout')})
            routes.append({'mistake': m, 'knob': 'fakeout_block_conf', 'delta': -0.02,
                           'path': _knob_path('fakeout_block_conf')})
            routes.append({'mistake': m, 'knob': 'prefer_retest_over_blind_break',
                           'flag': 1, 'path': _knob_path('prefer_retest_over_blind_break')})
        elif m == 'mid_range_mistake':
            deltas['penalty_range'] = deltas.get('penalty_range', 0) + 0.08
            flags['block_mid_range'] = 1
            flags['prefer_retest_over_blind_break'] = 1
            routes.append({'mistake': m, 'knob': 'penalty_range', 'delta': 0.08,
                           'path': _knob_path('penalty_range')})
            routes.append({'mistake': m, 'knob': 'block_mid_range', 'flag': 1,
                           'path': _knob_path('block_mid_range')})
            routes.append({'mistake': m, 'knob': 'prefer_retest_over_blind_break',
                           'flag': 1, 'path': _knob_path('prefer_retest_over_blind_break')})
        elif m == 'retest_fail':
            deltas['penalty_retest'] = deltas.get('penalty_retest', 0) + 0.06
            routes.append({'mistake': m, 'knob': 'penalty_retest', 'delta': 0.06,
                           'path': _knob_path('penalty_retest')})
        elif m == 'outcome_hurt':
            # Catch-all £ loss: teach the FULL decide stack (structure graves,
            # situation memory, forecast bridge, MTF) — never invent zone-only.
            for knob, delta in (
                    ('structure_edge_nudge', 0.02),
                    ('situation_memory_nudge', 0.02),
                    ('forecast_bridge_nudge', 0.01),
                    ('mtf_momentum_nudge', 0.01)):
                deltas[knob] = deltas.get(knob, 0) + delta
                routes.append({'mistake': m, 'knob': knob, 'delta': delta,
                               'path': _knob_path(knob)})
        elif m == 'news_risk_ignored':
            flags['respect_news_window'] = 1
            deltas['news_block_weight'] = deltas.get('news_block_weight', 0) + 0.1
            routes.append({'mistake': m, 'knob': 'respect_news_window', 'flag': 1,
                           'path': _knob_path('respect_news_window')})
            routes.append({'mistake': m, 'knob': 'news_block_weight', 'delta': 0.1,
                           'path': _knob_path('news_block_weight')})
        elif m == 'intermarket_fight':
            flags['respect_intermarket_veto'] = 1
            deltas['intermarket_veto_weight'] = (
                deltas.get('intermarket_veto_weight', 0) + 0.1)
            routes.append({'mistake': m, 'knob': 'respect_intermarket_veto', 'flag': 1,
                           'path': _knob_path('respect_intermarket_veto')})
            routes.append({'mistake': m, 'knob': 'intermarket_veto_weight', 'delta': 0.1,
                           'path': _knob_path('intermarket_veto_weight')})
    if not deltas and not flags:
        return None
    return {
        'deltas': deltas,
        'flags': flags,
        'mistakes': list(mistakes),
        'routes': routes,
        'why': 'targeted_to_mistake_class_not_pnl',
    }


def describe_fix_mutations(store, proposed: dict | None, *,
                             applied=False, before_params=None,
                             after_params=None, before_audit=None,
                             after_audit=None, before_self=None,
                             after_self=None) -> dict:
    """Concrete before→after for every knob in the proposal + untouched peers."""
    from . import self_params as sp
    proposed = proposed if isinstance(proposed, dict) else {}
    deltas = proposed.get('deltas') or {}
    flags = proposed.get('flags') or {}
    routes = list(proposed.get('routes') or [])
    params_before = dict(before_params) if isinstance(before_params, dict) else (
        ohlc_scenarios.load_params(store))
    audit_before = dict(before_audit) if isinstance(before_audit, dict) else (
        dict(store.get(STORE_KEY) or {}) if store is not None else {})
    self_before = dict(before_self) if isinstance(before_self, dict) else (
        sp.load(store) if store is not None else dict(sp.DEFAULTS))
    params_after = dict(after_params) if isinstance(after_params, dict) else None
    audit_after = dict(after_audit) if isinstance(after_audit, dict) else None
    self_after = dict(after_self) if isinstance(after_self, dict) else None

    mutations = []
    touched = set()

    def _fmt(v):
        if v is None:
            return '—'
        if isinstance(v, bool):
            return '1' if v else '0'
        try:
            f = float(v)
            if abs(f - int(f)) < 1e-9:
                return str(int(f))
            return f'{f:.4g}'
        except (TypeError, ValueError):
            return str(v)

    def _clip_self(key, before, delta):
        try:
            return float(sp._clip(key, float(before) + float(delta)))
        except Exception:
            return float(before) + float(delta)

    for k, d in deltas.items():
        path = _knob_path(k)
        touched.add(k)
        if k in _OHLC_DELTA_KEYS:
            before = float(params_before.get(k) or 0)
            after = (_clip01(before + float(d)) if params_after is None
                     else float(params_after.get(k) or 0))
            mutations.append({
                'knob': k, 'path': path, 'kind': 'delta',
                'before': before, 'after': after, 'delta': float(d),
                'applied': bool(applied),
                'line': (f'{path}  {_fmt(before)} → {_fmt(after)}  '
                         f'(Δ{float(d):+g})'
                         + ('  [WRITTEN]' if applied else '  [PROPOSED]')),
            })
        elif k in _AUDIT_DELTA_KEYS:
            before = float(audit_before.get(k) or 0)
            after = (_clip01(before + float(d)) if audit_after is None
                     else float(audit_after.get(k) or 0))
            mutations.append({
                'knob': k, 'path': path, 'kind': 'delta',
                'before': before, 'after': after, 'delta': float(d),
                'applied': bool(applied),
                'line': (f'{path}  {_fmt(before)} → {_fmt(after)}  '
                         f'(Δ{float(d):+g})'
                         + ('  [WRITTEN]' if applied else '  [PROPOSED]')),
            })
        elif k in _SELF_DELTA_KEYS:
            before = float(self_before.get(k) or 0)
            after = (_clip_self(k, before, d) if self_after is None
                     else float(self_after.get(k) or 0))
            mutations.append({
                'knob': k, 'path': path, 'kind': 'delta',
                'before': before, 'after': after, 'delta': float(d),
                'applied': bool(applied),
                'line': (f'{path}  {_fmt(before)} → {_fmt(after)}  '
                         f'(Δ{float(d):+g})'
                         + ('  [WRITTEN]' if applied else '  [PROPOSED]')),
            })

    for k, v in flags.items():
        path = _knob_path(k)
        touched.add(k)
        if k in _OHLC_FLAG_KEYS:
            before = int(params_before.get(k) or 0)
            after = int(v) if params_after is None else int(params_after.get(k) or 0)
            mutations.append({
                'knob': k, 'path': path, 'kind': 'flag',
                'before': before, 'after': after, 'delta': None,
                'applied': bool(applied),
                'line': (f'{path}  {_fmt(before)} → {_fmt(after)}'
                         + ('  [WRITTEN]' if applied else '  [PROPOSED]')),
            })
        elif k in _AUDIT_FLAG_KEYS:
            before = int(audit_before.get(k) or 0)
            after = int(v) if audit_after is None else int(audit_after.get(k) or 0)
            mutations.append({
                'knob': k, 'path': path, 'kind': 'flag',
                'before': before, 'after': after, 'delta': None,
                'applied': bool(applied),
                'line': (f'{path}  {_fmt(before)} → {_fmt(after)}'
                         + ('  [WRITTEN]' if applied else '  [PROPOSED]')),
            })

    untouched = []
    touched_ohlc = any(k in _ALL_TRACKED_OHLC for k in touched)
    touched_self = any(k in _SELF_DELTA_KEYS for k in touched)
    # System-review (self_params only): do not spam OHLC KEEP noise.
    if touched_ohlc or not touched_self:
        for k in _ALL_TRACKED_OHLC:
            if k in touched:
                continue
            path = _knob_path(k)
            cur = params_before.get(k)
            untouched.append({
                'knob': k, 'path': path, 'value': cur,
                'line': f'{path}  = {_fmt(cur)}  [UNTOUCHED]',
            })
        for k in _AUDIT_DELTA_KEYS + _AUDIT_FLAG_KEYS:
            if k in touched:
                continue
            path = _knob_path(k)
            cur = audit_before.get(k)
            untouched.append({
                'knob': k, 'path': path, 'value': cur,
                'line': (f'{path}  = {_fmt(cur) if cur is not None else "0"}'
                         f'  [UNTOUCHED]'),
            })
    if touched_self:
        for k in _ALL_TRACKED_SELF:
            if k in touched:
                continue
            path = _knob_path(k)
            cur = self_before.get(k)
            untouched.append({
                'knob': k, 'path': path, 'value': cur,
                'line': f'{path}  = {_fmt(cur)}  [UNTOUCHED]',
            })

    map_lines = []
    for r in routes:
        mist = r.get('mistake')
        path = r.get('path') or _knob_path(str(r.get('knob') or ''))
        if r.get('delta') is not None:
            map_lines.append(f'{mist} → {path}  Δ{float(r["delta"]):+g}')
        elif r.get('flag') is not None:
            map_lines.append(f'{mist} → {path}  = {r["flag"]}')
        else:
            map_lines.append(f'{mist} → {path}')

    return {
        'mutations': mutations,
        'untouched': untouched,
        'routes': routes,
        'map_lines': map_lines,
        'applied': bool(applied),
    }


def _scenario_bag(store) -> dict:
    raw = store.get(SCENARIOS_KEY) if store is not None else None
    return dict(raw) if isinstance(raw, dict) else {}


def _scenario_memory_undo(store, prior: dict) -> bool:
    """Reverse one prior observation so Funds £ re-audit does not double-count."""
    if store is None or not isinstance(prior, dict):
        return False
    fp = str(prior.get('fingerprint') or '')
    if not fp:
        return False
    bag = _scenario_bag(store)
    row = dict(bag.get(fp) or {})
    if not row:
        return False
    row['seen'] = max(0, int(row.get('seen') or 0) - 1)
    mistakes = list(prior.get('mistakes') or [])
    if mistakes:
        row['mistake_hits'] = max(0, int(row.get('mistake_hits') or 0) - 1)
        key = '+'.join(sorted(mistakes))
        counts = dict(row.get('mistake_counts') or {})
        counts[key] = max(0, int(counts.get(key) or 0) - 1)
        if counts[key] <= 0:
            counts.pop(key, None)
        row['mistake_counts'] = counts
    try:
        prior_pnl = float(prior.get('pnl'))
    except (TypeError, ValueError):
        prior_pnl = None
    if prior_pnl is not None:
        if prior_pnl > 0.02:
            row['wins'] = max(0, int(row.get('wins') or 0) - 1)
        elif prior_pnl < -0.02 or mistakes:
            row['losses'] = max(0, int(row.get('losses') or 0) - 1)
    # Only reverse wins_after_fix when THIS observation actually bumped it.
    # Never infer from current fix_committed (re-audit after a later fix would
    # wrongly subtract proof wins). Param commits are NOT rolled back here.
    if prior.get('bumped_wins_after_fix'):
        row['wins_after_fix'] = max(0, int(row.get('wins_after_fix') or 0) - 1)
    deal_s = str(prior.get('deal_id') or '')
    if deal_s:
        row['deal_ids'] = [d for d in (row.get('deal_ids') or [])
                           if str(d) != deal_s]
    row['updated_at'] = _now_iso()
    bag[fp] = row
    store.set(SCENARIOS_KEY, bag)
    return True


def scenario_memory_save(store, diagnosis: dict, *, deal_id=None,
                         prior=None) -> dict:
    """Persist fingerprint counts + last mistake for promote / ready gates.

    When the same ``deal_id`` is re-audited (Funds −0.81 → −0.89), undo the
    prior observation first so ``seen`` / ``mistake_hits`` / promote counters
    count each Capital trade once.
    """
    if store is None or not isinstance(diagnosis, dict) or not diagnosis.get('audited'):
        return {'saved': False}
    ctx = diagnosis.get('context') if isinstance(diagnosis.get('context'), dict) else {}
    fp = str(ctx.get('fingerprint') or '')
    if not fp:
        return {'saved': False, 'reason': 'no_fingerprint'}
    undone = False
    if prior and deal_id and str(prior.get('deal_id') or '') == str(deal_id):
        undone = _scenario_memory_undo(store, prior)
    bag = _scenario_bag(store)
    row = dict(bag.get(fp) or {})
    row['fingerprint'] = fp
    row['scenario'] = ctx.get('stamped_scenario') or ctx.get('live_scenario')
    row['zone_side'] = ctx.get('zone_side')
    row['news_status'] = ctx.get('news_status')
    row['intermarket_status'] = ctx.get('intermarket_status')
    row['direction'] = ctx.get('direction')
    row['trigger'] = ctx.get('trigger')
    # Snapshot whether a fix was already live BEFORE this observation.
    fix_already = bool(row.get('fix_committed'))
    row['seen'] = int(row.get('seen') or 0) + 1
    mistakes = list(diagnosis.get('mistakes') or [])
    if mistakes:
        row['mistake_hits'] = int(row.get('mistake_hits') or 0) + 1
        key = '+'.join(sorted(mistakes))
        counts = dict(row.get('mistake_counts') or {})
        counts[key] = int(counts.get(key) or 0) + 1
        row['mistake_counts'] = counts
        row['last_mistakes'] = mistakes
        row['last_notes'] = list(diagnosis.get('notes') or [])
    bumped_waf = False
    if diagnosis.get('useful'):
        row['wins'] = int(row.get('wins') or 0) + 1
        if fix_already:
            row['wins_after_fix'] = int(row.get('wins_after_fix') or 0) + 1
            bumped_waf = True
    if diagnosis.get('hurt'):
        row['losses'] = int(row.get('losses') or 0) + 1
    deal_s = str(deal_id) if deal_id is not None else ''
    if deal_s:
        ids = [str(d) for d in (row.get('deal_ids') or []) if str(d) != deal_s]
        ids.append(deal_s)
        row['deal_ids'] = ids[-40:]
    row['last_pnl'] = diagnosis.get('pnl')
    row['last_source'] = diagnosis.get('source')
    row['last_deal_id'] = deal_s or row.get('last_deal_id')
    row['updated_at'] = _now_iso()
    bag[fp] = row
    # Cap memory size.
    if len(bag) > 80:
        ordered = sorted(
            bag.items(),
            key=lambda kv: str((kv[1] or {}).get('updated_at') or ''),
            reverse=True)
        bag = dict(ordered[:80])
    store.set(SCENARIOS_KEY, bag)
    return {'saved': True, 'fingerprint': fp, 'seen': row['seen'],
            'row': row, 'undone_prior': undone,
            'bumped_wins_after_fix': bumped_waf}


def promote_gate(store, fingerprint: str, mistakes, *, n=PROMOTE_N) -> dict:
    """True when the same fingerprint+mistake class has repeated ≥ n times."""
    out = {'ready': False, 'count': 0, 'need': int(n), 'fingerprint': fingerprint}
    if store is None or not fingerprint or not mistakes:
        return out
    bag = _scenario_bag(store)
    row = bag.get(fingerprint) if isinstance(bag.get(fingerprint), dict) else {}
    key = '+'.join(sorted(mistakes))
    count = int((row.get('mistake_counts') or {}).get(key) or 0)
    out['count'] = count
    out['mistake_key'] = key
    out['ready'] = count >= int(n)
    already = bool(row.get('fix_committed'))
    out['already_committed'] = already
    # Only commit once per fingerprint until cleared by wins / manual.
    if out['ready'] and already:
        out['ready'] = False
        out['reason'] = 'already_committed'
    return out


def build_fix_cycle_steps(diagnosis: dict | None, mem: dict | None,
                            gate: dict | None, commit_result: dict | None,
                            ready=None, *, apply_ohlc_penalties=True,
                            store=None) -> list:
    """Explicit CLOSE→ERROR→FIX steps with concrete before→after paths."""
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    mem = mem if isinstance(mem, dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    commit_result = commit_result if isinstance(commit_result, dict) else {}
    mistakes = [m for m in (diagnosis.get('mistakes') or []) if m]
    proposed = diagnosis.get('proposed_fix') if isinstance(
        diagnosis.get('proposed_fix'), dict) else None
    if not proposed and isinstance(commit_result.get('proposed'), dict):
        proposed = commit_result.get('proposed')
    if isinstance(proposed, dict) and not proposed.get('routes') and mistakes:
        rebuilt = propose_fix(mistakes)
        if isinstance(rebuilt, dict):
            proposed = {**proposed, 'routes': rebuilt.get('routes') or []}
    ctx = diagnosis.get('context') if isinstance(diagnosis.get('context'), dict) else {}
    fp = str(ctx.get('fingerprint') or mem.get('fingerprint')
             or gate.get('fingerprint') or '')
    mut_pack = None
    if isinstance(proposed, dict) and (proposed.get('deltas') or proposed.get('flags')):
        mut_pack = describe_fix_mutations(
            store, proposed,
            applied=bool(commit_result.get('applied')),
            before_params=commit_result.get('before_params'),
            after_params=commit_result.get('after_params') or commit_result.get('params'),
        )
        # Prefer mutations already computed at apply time (exact written values).
        if commit_result.get('mutations'):
            mut_pack = {
                **mut_pack,
                'mutations': commit_result.get('mutations'),
                'untouched': commit_result.get('untouched') or mut_pack.get('untouched'),
                'map_lines': commit_result.get('map_lines') or mut_pack.get('map_lines'),
                'applied': bool(commit_result.get('applied')),
            }
    steps = []

    # 1) Diagnose
    if mistakes:
        steps.append({
            'step': 'diagnose', 'status': 'ok',
            'detail': 'mistakes: ' + ' · '.join(str(m) for m in mistakes[:6]),
            'changed': list(mistakes), 'skipped': [],
        })
    else:
        steps.append({
            'step': 'diagnose', 'status': 'skip',
            'detail': 'no mistake class (scratch/win/flat)',
            'changed': [], 'skipped': ['targeted_fix'],
        })

    # 2) Propose knobs — concrete mistake→path map + before→after
    deltas = (proposed or {}).get('deltas') or {}
    flags = (proposed or {}).get('flags') or {}
    would = []
    for k, d in deltas.items():
        try:
            would.append(f'{_knob_path(k)} Δ{float(d):+g}')
        except (TypeError, ValueError):
            would.append(_knob_path(k))
    for k, v in flags.items():
        would.append(f'{_knob_path(k)}={v}')
    if would:
        detail = '; '.join((mut_pack or {}).get('map_lines') or would)[:160]
        steps.append({
            'step': 'propose', 'status': 'ok',
            'detail': detail,
            'changed': would,
            'skipped': [],
            'mutations': (mut_pack or {}).get('mutations') or [],
            'map_lines': (mut_pack or {}).get('map_lines') or [],
            'untouched': (mut_pack or {}).get('untouched') or [],
        })
    elif mistakes:
        steps.append({
            'step': 'propose', 'status': 'skip',
            'detail': 'no knobs mapped for this mistake class',
            'changed': [], 'skipped': list(mistakes),
        })
    else:
        steps.append({
            'step': 'propose', 'status': 'skip',
            'detail': 'nothing to propose',
            'changed': [], 'skipped': ['deltas', 'flags'],
        })

    # 3) Scenario memory
    if mem.get('saved'):
        steps.append({
            'step': 'memory', 'status': 'ok',
            'detail': (f"fp={fp[:10] or '—'} seen={mem.get('seen')}"
                       + (' · undone_prior' if mem.get('undone_prior') else '')),
            'changed': ['scenario_bag'] if fp else [],
            'skipped': ([] if fp else ['fingerprint']),
        })
    else:
        steps.append({
            'step': 'memory', 'status': 'skip',
            'detail': str(mem.get('reason') or 'scenario memory not saved'),
            'changed': [],
            'skipped': ['fingerprint', 'scenario_bag'],
        })

    # 4) Promote gate
    if mistakes and proposed:
        cnt = int(gate.get('count') or 0)
        need = int(gate.get('need') or PROMOTE_N)
        if gate.get('already_committed'):
            steps.append({
                'step': 'promote', 'status': 'skip',
                'detail': f'already_committed · gate {cnt}/{need}',
                'changed': [], 'skipped': ['recommit'],
            })
        elif gate.get('ready'):
            steps.append({
                'step': 'promote', 'status': 'ok',
                'detail': f'gate ready {cnt}/{need} — may commit',
                'changed': [], 'skipped': [],
            })
        else:
            steps.append({
                'step': 'promote', 'status': 'wait',
                'detail': f'gate {cnt}/{need} — need more same fingerprint+mistake',
                'changed': [], 'skipped': ['commit'],
            })
    elif mistakes:
        steps.append({
            'step': 'promote', 'status': 'skip',
            'detail': 'no proposal → promote gate idle',
            'changed': [], 'skipped': ['promote_gate'],
        })

    # 5) Commit — attach concrete WRITE lines (before → after)
    applied = bool(commit_result.get('applied'))
    changed = list(commit_result.get('changed') or [])
    mut_lines = [m.get('line') for m in ((mut_pack or {}).get('mutations') or [])
                 if isinstance(m, dict) and m.get('line')]
    unt_lines = [u.get('line') for u in ((mut_pack or {}).get('untouched') or [])
                 if isinstance(u, dict) and u.get('line')]
    commit_extra = {
        'mutations': (mut_pack or {}).get('mutations') or [],
        'untouched': (mut_pack or {}).get('untouched') or [],
        'map_lines': (mut_pack or {}).get('map_lines') or [],
        'mutation_lines': mut_lines,
        'untouched_lines': unt_lines[:12],
    }
    if applied:
        detail = ('; '.join(mut_lines[:3])
                  if mut_lines else
                  ('COMMITTED knobs: ' + (', '.join(changed[:8]) or 'flags')))
        steps.append({
            'step': 'commit', 'status': 'ok',
            'detail': detail[:180],
            'changed': changed,
            'skipped': [],
            **commit_extra,
        })
    elif commit_result.get('pending'):
        reason = []
        if not apply_ohlc_penalties:
            reason.append('ohlc_penalties_off')
        if not gate.get('ready'):
            reason.append('promote_gate_not_ready')
        if not fp:
            reason.append('no_fingerprint')
        steps.append({
            'step': 'commit', 'status': 'wait',
            'detail': 'PENDING — ' + (', '.join(reason) or 'awaiting gate'),
            'changed': [], 'skipped': would[:8],
            **commit_extra,
        })
    elif mistakes and proposed:
        reason = str(commit_result.get('reason') or '')
        if not apply_ohlc_penalties:
            reason = reason or 'ohlc_penalties_off'
        elif not gate.get('ready'):
            reason = reason or 'promote_gate_not_ready'
        elif not fp:
            reason = reason or 'no_fingerprint'
        steps.append({
            'step': 'commit', 'status': 'skip',
            'detail': 'NOT committed — ' + (reason or 'unknown'),
            'changed': [], 'skipped': would[:8],
            **commit_extra,
        })
    else:
        steps.append({
            'step': 'commit', 'status': 'skip',
            'detail': 'nothing to commit',
            'changed': [], 'skipped': ['params'],
        })

    # 6) Ready LIVE — check wait/already BEFORE any fingerprint success claim.
    if isinstance(ready, dict) and ready.get('need') is not None:
        steps.append({
            'step': 'ready', 'status': 'wait',
            'detail': (f"proof wins {ready.get('wins_after_fix') or 0}/"
                       f"{ready.get('need')} after fix"),
            'changed': [], 'skipped': ['ready_for_live'],
        })
    elif isinstance(ready, dict) and ready.get('already'):
        steps.append({
            'step': 'ready', 'status': 'skip',
            'detail': 'already ready_for_live',
            'changed': [], 'skipped': ['ready_queue'],
        })
    elif _ready_stamped_now(ready):
        steps.append({
            'step': 'ready', 'status': 'ok',
            'detail': 'ready_for_live stamped',
            'changed': ['ready_queue'], 'skipped': [],
        })
    elif applied:
        steps.append({
            'step': 'ready', 'status': 'wait',
            'detail': f'need {READY_N} proof win(s) on next same situation',
            'changed': [], 'skipped': ['ready_for_live'],
        })
    else:
        steps.append({
            'step': 'ready', 'status': 'skip',
            'detail': 'fix not committed — LIVE promote idle',
            'changed': [], 'skipped': ['ready_for_live'],
        })
    return steps


def journal_seed_fix_steps(audit_row: dict | None, store=None) -> list:
    """Honest wait steps — never invent a fake MAP to one OHLC knob."""
    a = audit_row if isinstance(audit_row, dict) else {}
    try:
        pnl = float(a.get('pnl'))
    except (TypeError, ValueError):
        pnl = None
    hurt = pnl is not None and pnl < -0.02
    need = [
        'ohlc_entry/scenario stamp',
        'structure frame',
        'news + peers',
        'neural scores at OPEN',
        'fingerprint from on_close',
    ]
    detail_diag = (
        'Capital £ only — full diagnose needs OHLC+structure+news+peers+neural'
        if hurt else 'Capital £ scratch/flat — waiting on_close context')
    return [{
        'step': 'diagnose', 'status': 'wait',
        'detail': detail_diag,
        'changed': [], 'skipped': ['mistakes'],
    }, {
        'step': 'propose', 'status': 'skip',
        'detail': 'no MAP until on_close — no invented knobs',
        'changed': [], 'skipped': ['knobs'],
        'mutations': [], 'map_lines': [], 'untouched': [],
        'mutation_lines': [], 'untouched_lines': [],
    }, {
        'step': 'memory', 'status': 'skip',
        'detail': 'journal display only — no fingerprint / entry_brain',
        'changed': [], 'skipped': ['fingerprint', 'scenario_bag'],
    }, {
        'step': 'promote', 'status': 'skip',
        'detail': 'cannot promote without fingerprint from on_close',
        'changed': [], 'skipped': ['promote_gate'],
    }, {
        'step': 'commit', 'status': 'wait',
        'detail': 'WAITING backfill/on_close — needs: ' + ' · '.join(need[:4]),
        'changed': [], 'skipped': list(need),
        'mutations': [], 'map_lines': [], 'untouched': [],
        'mutation_lines': [], 'untouched_lines': [],
    }, {
        'step': 'ready', 'status': 'skip',
        'detail': 'LIVE promote idle until real commit + proof win',
        'changed': [], 'skipped': ['ready_for_live'],
    }]


def apply_targeted_fix(store, diagnosis: dict, *, commit=False) -> dict:
    """Apply proposed knobs only when commit=True (after promote_gate)."""
    proposed = diagnosis.get('proposed_fix') if isinstance(diagnosis, dict) else None
    if not isinstance(proposed, dict):
        return {'applied': False, 'reason': 'no_proposal'}
    # Ensure routes exist for CMD (older proposals may lack them).
    if not proposed.get('routes') and proposed.get('mistakes'):
        rebuilt = propose_fix(proposed.get('mistakes'))
        if isinstance(rebuilt, dict):
            proposed = {**proposed, 'routes': rebuilt.get('routes') or []}
    from . import self_params as sp
    before_params = ohlc_scenarios.load_params(store)
    before_audit = dict(store.get(STORE_KEY) or {}) if store is not None else {}
    before_self = sp.load(store) if store is not None else dict(sp.DEFAULTS)
    preview = describe_fix_mutations(
        store, proposed, applied=False,
        before_params=before_params, before_audit=before_audit,
        before_self=before_self)
    if not commit:
        if store is not None:
            store.set(PENDING_FIX_KEY, {
                'at': _now_iso(),
                'proposed': proposed,
                'fingerprint': (diagnosis.get('context') or {}).get('fingerprint'),
                'mistakes': diagnosis.get('mistakes'),
                'committed': False,
                'changed': [],
                'why': proposed.get('why'),
                'mutations': preview.get('mutations'),
                'untouched': preview.get('untouched'),
                'map_lines': preview.get('map_lines'),
            })
        return {
            'applied': False, 'pending': True, 'proposed': proposed,
            'reason': 'pending_gate',
            'mutations': preview.get('mutations'),
            'untouched': preview.get('untouched'),
            'map_lines': preview.get('map_lines'),
            'before_params': before_params,
        }

    params = dict(before_params)
    self_p = dict(before_self)
    deltas = proposed.get('deltas') or {}
    flags = proposed.get('flags') or {}
    changed = []
    ohlc_changed = False
    self_changed = False
    for k, d in deltas.items():
        if k in _OHLC_DELTA_KEYS:
            params[k] = _clip01(float(params.get(k) or 0) + float(d))
            changed.append(k)
            ohlc_changed = True
        elif k in _SELF_DELTA_KEYS:
            self_p[k] = sp._clip(k, float(self_p.get(k) or 0) + float(d))
            changed.append(k)
            self_changed = True
        # news / intermarket weights live in audit bag (not ohlc_scenarios).
    for k, v in flags.items():
        if k in _OHLC_FLAG_KEYS:
            params[k] = int(v)
            changed.append(k)
            ohlc_changed = True
    if ohlc_changed:
        ohlc_scenarios.save_params(store, params)
    if self_changed and store is not None:
        sp.save(store, self_p, reason='shadow_mistake_outcome_hurt')

    bag = _scenario_bag(store)
    fp = str((diagnosis.get('context') or {}).get('fingerprint') or '')
    if fp and fp in bag:
        row = dict(bag[fp])
        row['fix_committed'] = True
        row['fix_at'] = _now_iso()
        # New fix must re-earn proof wins — never inherit stale READY.
        row['wins_after_fix'] = 0
        row['ready_for_live'] = False
        row['fix'] = {
            'deltas': deltas, 'flags': flags, 'changed': changed,
            'mutations': preview.get('mutations'),
        }
        bag[fp] = row
        store.set(SCENARIOS_KEY, bag)
        ready_q = [
            r for r in (store.get(READY_KEY) or [])
            if isinstance(r, dict) and r.get('fingerprint') != fp]
        store.set(READY_KEY, ready_q)

    # Persist soft news/intermarket respect flags on audit store.
    audit = dict(before_audit)
    if flags.get('respect_news_window'):
        audit['respect_news_window'] = 1
        w = _clip01(float(audit.get('news_block_weight') or 0)
                    + float(deltas.get('news_block_weight') or 0))
        audit['news_block_weight'] = w
        if 'news_block_weight' not in changed:
            changed.append('news_block_weight')
        if 'respect_news_window' not in changed:
            changed.append('respect_news_window')
    if flags.get('respect_intermarket_veto'):
        audit['respect_intermarket_veto'] = 1
        w = _clip01(float(audit.get('intermarket_veto_weight') or 0)
                    + float(deltas.get('intermarket_veto_weight') or 0))
        audit['intermarket_veto_weight'] = w
        if 'intermarket_veto_weight' not in changed:
            changed.append('intermarket_veto_weight')
        if 'respect_intermarket_veto' not in changed:
            changed.append('respect_intermarket_veto')
    written = describe_fix_mutations(
        store, proposed, applied=True,
        before_params=before_params, after_params=params,
        before_audit=before_audit, after_audit=audit,
        before_self=before_self, after_self=self_p)
    if store is not None:
        audit['last_commit_at'] = _now_iso()
        store.set(STORE_KEY, audit)
        store.set(PENDING_FIX_KEY, {
            'at': _now_iso(),
            'proposed': proposed,
            'fingerprint': fp,
            'mistakes': diagnosis.get('mistakes'),
            'committed': True,
            'changed': changed,
            'mutations': written.get('mutations'),
            'untouched': written.get('untouched'),
            'map_lines': written.get('map_lines'),
            'why': proposed.get('why'),
        })
        store.event('shadow_mistake_audit', 'Targeted fix committed', {
            'fingerprint': fp, 'changed': changed,
            'mistakes': diagnosis.get('mistakes'),
            'mutations': [
                {'path': m.get('path'), 'before': m.get('before'),
                 'after': m.get('after')}
                for m in (written.get('mutations') or [])],
        })
    return {
        'applied': True, 'changed': changed, 'params': params,
        'proposed': proposed,
        'mutations': written.get('mutations'),
        'untouched': written.get('untouched'),
        'map_lines': written.get('map_lines'),
        'before_params': before_params,
        'after_params': params,
    }


def _mark_ready_entry(store, *, fingerprint: str, row: dict,
                      source=None, wins_after=None) -> dict:
    """Push fingerprint onto READY queue and stamp scenario bag."""
    ready = list(store.get(READY_KEY) or [])
    entry = {
        'fingerprint': fingerprint,
        'scenario': row.get('scenario'),
        'direction': row.get('direction'),
        'zone_side': row.get('zone_side'),
        'wins_after_fix': int(
            wins_after if wins_after is not None
            else (row.get('wins_after_fix') or 0)),
        'ready_at': _now_iso(),
        'source': source,
    }
    ready = [r for r in ready if isinstance(r, dict)
             and r.get('fingerprint') != fingerprint]
    ready.insert(0, entry)
    store.set(READY_KEY, ready[:20])
    bag = _scenario_bag(store)
    stamped = dict(bag.get(fingerprint) or row or {})
    stamped['ready_for_live'] = True
    stamped['fingerprint'] = fingerprint
    bag[fingerprint] = stamped
    store.set(SCENARIOS_KEY, bag)
    store.event('shadow_mistake_audit', 'Scenario ready for LIVE', entry)
    return entry


def _update_ready(store, diagnosis: dict) -> dict | None:
    """Mark fingerprint ready_for_live only after ≥ READY_N proof wins (V04)."""
    if store is None or not isinstance(diagnosis, dict):
        return None
    ctx = diagnosis.get('context') or {}
    fp = str(ctx.get('fingerprint') or '')
    if not fp:
        return None
    bag = _scenario_bag(store)
    row = bag.get(fp) if isinstance(bag.get(fp), dict) else None
    if not row or not row.get('fix_committed'):
        return None
    if row.get('ready_for_live'):
        return {'fingerprint': fp, 'already': True}
    wins_after = int(row.get('wins_after_fix') or 0)
    if wins_after < int(READY_N):
        return {'fingerprint': fp, 'wins_after_fix': wins_after, 'need': READY_N}
    return _mark_ready_entry(
        store, fingerprint=fp, row=row,
        source=diagnosis.get('source'), wins_after=wins_after)


def on_close(store, *, pnl, spread=0.0, candles=None, scenario_meta=None,
             ohlc_meta=None, news=None, intermarket=None, direction=None,
             mid=None, entry=None, exit=None, exit_reason=None,
             source='shadow', deal_id=None,
             unit=None, currency=None, paper_pnl=None, price_pnl=None,
             closed_at=None,
             apply_ohlc_penalties=True) -> dict:
    """Full loop: diagnose → memory → promote_gate → optional commit → ready."""
    diagnosis = diagnose(
        pnl=pnl, spread=spread, candles=candles,
        scenario_meta=scenario_meta, ohlc_meta=ohlc_meta,
        news=news, intermarket=intermarket, direction=direction,
        mid=mid, entry=entry, store=store, source=source)
    if not diagnosis.get('audited'):
        return diagnosis

    # LIVE / Funds cash is account currency; shadow may pass explicit unit.
    src_l = str(source or '').lower()
    unit_s = str(unit or '').strip() or None
    if unit_s not in ('price', 'account_currency', None):
        unit_s = None
    if unit_s is None and (src_l.startswith('live') or src_l in (
            'broker', 'funds', 'capital', 'live_backfill', 'live_funds_mirror')):
        unit_s = 'account_currency'
    ccy_s = (str(currency).strip().upper() if currency else None) or None
    paper_n = _finite(paper_pnl)
    price_n = _finite(price_pnl)
    if unit_s == 'account_currency' and paper_n is None:
        paper_n = _finite(pnl)
    if unit_s == 'price' and price_n is None:
        price_n = _finite(pnl)

    prior = None
    deal_s = str(deal_id) if deal_id is not None else ''
    if store is not None and deal_s:
        for h in (store.get(HIST_KEY) or []):
            if isinstance(h, dict) and str(h.get('deal_id') or '') == deal_s:
                prior = h
                break

    mem = scenario_memory_save(
        store, diagnosis, deal_id=deal_id, prior=prior)
    gate = {'ready': False}
    commit_result = {'applied': False, 'pending': False}
    if diagnosis.get('mistakes') and diagnosis.get('proposed_fix'):
        fp = (diagnosis.get('context') or {}).get('fingerprint')
        gate = promote_gate(store, fp, diagnosis['mistakes'])
        # Apply targeted fix so the next same-situation OPEN tests the change.
        # LIVE Capital £ and SHADOW paper both commit — no pending-only stall.
        do_commit = bool(apply_ohlc_penalties and gate.get('ready'))
        commit_result = apply_targeted_fix(
            store, diagnosis, commit=do_commit)
        if isinstance(commit_result, dict) and do_commit:
            commit_result = dict(commit_result, live_commit=True)

    ready = None
    # V04: ready_for_live only after a proof win — never on loss commit alone.
    if diagnosis.get('useful'):
        ready = _update_ready(store, diagnosis)

    fix_steps = build_fix_cycle_steps(
        diagnosis, mem, gate, commit_result, ready,
        apply_ohlc_penalties=apply_ohlc_penalties, store=store)
    ready_now = _ready_stamped_now(ready)

    audited_at = _now_iso()
    # Trade close time for 24h windows — never confuse with audit runtime.
    closed_stamp = closed_at
    if not closed_stamp and isinstance(prior, dict):
        closed_stamp = prior.get('closed_at') or prior.get('at')
    if not closed_stamp:
        closed_stamp = audited_at
    out = {
        **diagnosis,
        'memory': mem,
        'promote_gate': gate,
        'fix': commit_result,
        'fix_steps': fix_steps,
        'ready_for_live': ready if ready_now else None,
        'ready_stamped_this_close': ready_now,
        'at': audited_at,
        'audited_at': audited_at,
        'closed_at': closed_stamp,
        'deal_id': deal_s or None,
        'unit': unit_s,
        'currency': ccy_s,
        'paper_pnl': paper_n,
        'price_pnl': price_n,
    }
    if store is not None:
        hist = list(store.get(HIST_KEY) or [])
        entry_row = {
            'at': audited_at,  # legacy alias = audited_at
            'audited_at': audited_at,
            'closed_at': closed_stamp,
            'source': source,
            'pnl': diagnosis.get('pnl'),
            'direction': direction,
            'entry': entry,
            'exit': exit,
            'exit_reason': exit_reason,
            'mistakes': diagnosis.get('mistakes'),
            'notes': diagnosis.get('notes'),
            'fingerprint': (diagnosis.get('context') or {}).get('fingerprint'),
            'scenario': (diagnosis.get('context') or {}).get('stamped_scenario')
                        or (diagnosis.get('context') or {}).get('live_scenario'),
            'committed': bool(commit_result.get('applied')),
            'promote_count': gate.get('count'),
            'promote_need': gate.get('need'),
            'deal_id': deal_s or None,
            'unit': unit_s,
            'currency': ccy_s,
            'paper_pnl': paper_n,
            'price_pnl': price_n,
            'bumped_wins_after_fix': bool(
                (mem or {}).get('bumped_wins_after_fix')),
            'fix_steps': fix_steps,
            'fix_mutations': list(commit_result.get('mutations') or []),
            'fix_untouched': list(commit_result.get('untouched') or [])[:16],
            'fix_map_lines': list(commit_result.get('map_lines') or []),
            'ready_stamped_this_close': ready_now,
            'ready_for_live': ready_now,
        }
        # Durable narrative for Desk CMD log (loss → error → fix → outcome).
        if isinstance(commit_result, dict):
            entry_row['fix_applied'] = bool(commit_result.get('applied'))
            entry_row['fix_changed'] = list(commit_result.get('changed') or [])
            prop = commit_result.get('proposed') or diagnosis.get('proposed_fix')
            if isinstance(prop, dict):
                entry_row['fix_why'] = prop.get('why')
                entry_row['fix_deltas'] = prop.get('deltas')
                entry_row['fix_flags'] = prop.get('flags')
                entry_row['fix_routes'] = prop.get('routes')
        # Re-audit after Funds £ correction must REPLACE the prior row for this
        # deal — otherwise hist doubles and scenario repeat counters inflate.
        if deal_s:
            hist = [h for h in hist
                    if not (isinstance(h, dict) and str(h.get('deal_id') or '') == deal_s)]
        hist.append(entry_row)
        store.set(HIST_KEY, hist[-200:])  # keep more than a dense 24h book
        store.set(LAST_KEY, {
            'at': out['at'],
            'source': source,
            'pnl': diagnosis.get('pnl'),
            'mistakes': diagnosis.get('mistakes'),
            'notes': diagnosis.get('notes'),
            'reason': diagnosis.get('reason'),
            'fingerprint': (diagnosis.get('context') or {}).get('fingerprint'),
            'scenario': (diagnosis.get('context') or {}).get('stamped_scenario')
                        or (diagnosis.get('context') or {}).get('live_scenario'),
            'zone_side': (diagnosis.get('context') or {}).get('zone_side'),
            'news_status': (diagnosis.get('context') or {}).get('news_status'),
            'intermarket_status': (diagnosis.get('context') or {}).get(
                'intermarket_status'),
            'proposed_fix': diagnosis.get('proposed_fix'),
            'fix_committed': bool(commit_result.get('applied')),
            'fix_steps': fix_steps,
            'fix_mutations': list(commit_result.get('mutations') or []),
            'fix_untouched': list(commit_result.get('untouched') or [])[:16],
            'fix_map_lines': list(commit_result.get('map_lines') or []),
            'promote_count': gate.get('count'),
            'promote_need': gate.get('need'),
            'ready_for_live': ready_now,
            'ready_stamped_this_close': ready_now,
            'deal_id': deal_s or None,
            'unit': unit_s,
            'currency': ccy_s,
            'paper_pnl': paper_n,
            'price_pnl': price_n,
            'closed_at': closed_stamp,
            'audited_at': audited_at,
        })
        if diagnosis.get('mistakes'):
            store.event('shadow_mistake_audit', 'Mistake diagnosed', {
                'source': source,
                'mistakes': diagnosis.get('mistakes'),
                'notes': diagnosis.get('notes'),
                'fingerprint': (diagnosis.get('context') or {}).get('fingerprint'),
                'promote': gate.get('count'),
                'need': gate.get('need'),
                'committed': bool(commit_result.get('applied')),
                'deal_id': deal_s or None,
                'fix_steps': [
                    {'step': s.get('step'), 'status': s.get('status')}
                    for s in fix_steps],
            })
    return out


def flush_full_cycle(store) -> dict:
    """Commit pending fixes that already meet PROMOTE_N; ready only after READY_N.

    Does not bypass V04 — never marks ready_for_live without proof wins.
    """
    if store is None:
        return {'flushed': False, 'reason': 'no_store'}
    bag = _scenario_bag(store)
    committed = 0
    readied = 0
    for fp, row in list(bag.items()):
        if not isinstance(row, dict) or not fp:
            continue
        counts = row.get('mistake_counts') if isinstance(
            row.get('mistake_counts'), dict) else {}
        # Prefer the hottest mistake class for this fingerprint.
        mist_key = None
        mist_n = 0
        for k, n in counts.items():
            try:
                nn = int(n)
            except (TypeError, ValueError):
                continue
            if nn > mist_n:
                mist_n = nn
                mist_key = k
        if mist_key is None and int(row.get('mistake_hits') or 0) <= 0:
            continue
        mistakes = [m for m in str(mist_key or '').split('+') if m]
        if not mistakes and row.get('last_mistakes'):
            mistakes = list(row.get('last_mistakes') or [])
        if not mistakes:
            continue
        gate = promote_gate(store, fp, mistakes)
        if gate.get('ready') and not row.get('fix_committed'):
            proposed = propose_fix(mistakes)
            if not isinstance(proposed, dict):
                continue
            diagnosis = {
                'mistakes': mistakes,
                'proposed_fix': proposed,
                'context': {
                    'fingerprint': fp,
                    'scenario': row.get('scenario'),
                    'direction': row.get('direction'),
                    'zone_side': row.get('zone_side'),
                },
                'source': 'flush_full_cycle',
            }
            out = apply_targeted_fix(store, diagnosis, commit=True)
            if out.get('applied'):
                committed += 1
                bag = _scenario_bag(store)
                row = bag.get(fp) if isinstance(bag.get(fp), dict) else row
        bag = _scenario_bag(store)
        row = bag.get(fp) if isinstance(bag.get(fp), dict) else None
        if (isinstance(row, dict) and row.get('fix_committed')
                and not row.get('ready_for_live')
                and int(row.get('wins_after_fix') or 0) >= int(READY_N)):
            _mark_ready_entry(
                store, fingerprint=fp, row=row, source='flush_full_cycle')
            readied += 1
    # Also commit a bare PENDING_FIX if gate already satisfied.
    pending = store.get(PENDING_FIX_KEY) if isinstance(
        store.get(PENDING_FIX_KEY), dict) else {}
    if pending and not pending.get('committed') and pending.get('proposed'):
        fp = str(pending.get('fingerprint') or '')
        mistakes = list(pending.get('mistakes') or [])
        if fp and mistakes:
            gate = promote_gate(store, fp, mistakes)
            if gate.get('ready') or gate.get('count', 0) >= PROMOTE_N:
                diagnosis = {
                    'mistakes': mistakes,
                    'proposed_fix': pending.get('proposed'),
                    'context': {'fingerprint': fp},
                    'source': 'flush_pending',
                }
                out = apply_targeted_fix(store, diagnosis, commit=True)
                if out.get('applied'):
                    committed += 1
                    bag = _scenario_bag(store)
                    row = bag.get(fp) if isinstance(bag.get(fp), dict) else {
                        'fingerprint': fp}
                    if (not row.get('ready_for_live')
                            and int(row.get('wins_after_fix') or 0) >= int(READY_N)):
                        _mark_ready_entry(
                            store, fingerprint=fp, row=row,
                            source='flush_pending')
                        readied += 1
    if committed or readied:
        store.event('shadow_mistake_audit', 'Full cycle flushed', {
            'committed': committed, 'readied': readied,
            'promote_n': PROMOTE_N, 'ready_n': READY_N})
    return {'flushed': True, 'committed': committed, 'readied': readied}


def _format_fix_step_lines(when: str, step: dict) -> list:
    """CMD lines with concrete paths: MAP / WRITE before→after / UNTOUCHED."""
    if not isinstance(step, dict):
        return []
    name = str(step.get('step') or 'step').upper()
    status = str(step.get('status') or '?').lower()
    detail = str(step.get('detail') or '')[:160]
    level = {
        'ok': 'fix',
        'wait': 'wait',
        'skip': 'note',
    }.get(status, 'info')
    tag = {
        'ok': 'DONE',
        'wait': 'WAIT',
        'skip': 'SKIP',
    }.get(status, status.upper())
    lines = [{
        'level': level,
        'text': f'{when}  STEP   {name} · {tag} · {detail}',
        'step': step.get('step'),
        'status': status,
    }]
    # Mistake → knob path map (full route).
    for ml in (step.get('map_lines') or [])[:8]:
        lines.append({
            'level': 'fix',
            'text': f'{when}  MAP    {ml}',
            'step': step.get('step'),
            'status': 'ok',
        })
    # Concrete before → after writes / proposals.
    for ml in (step.get('mutation_lines') or [])[:8]:
        prefix = 'WRITE ' if status == 'ok' and name == 'COMMIT' else 'VALUE '
        lines.append({
            'level': 'fix' if 'WRITTEN' in str(ml) else (
                'wait' if 'PROPOSED' in str(ml) else 'info'),
            'text': f'{when}  {prefix}{ml}',
            'step': step.get('step'),
            'status': status,
        })
    # What was deliberately not touched.
    for ul in (step.get('untouched_lines') or [])[:8]:
        lines.append({
            'level': 'note',
            'text': f'{when}  KEEP   {ul}',
            'step': step.get('step'),
            'status': 'skip',
        })
    skipped = [str(x) for x in (step.get('skipped') or []) if x][:6]
    if skipped and status in ('skip', 'wait') and name not in ('PROPOSE', 'COMMIT'):
        lines.append({
            'level': 'note',
            'text': f'{when}  SKIP   {name}: ' + ', '.join(skipped),
            'step': step.get('step'),
            'status': 'skip',
        })
    return lines


def build_learning_log(*, recent_audits=None, pending_fix=None,
                       scenarios=None, limit=48, store=None) -> list:
    """Human-readable CMD lines: CLOSE → ERROR → FIX → OUTCOME.

    Newest first so Desk operators see the latest minus and what SHADOW did —
    including concrete knob paths and before→after values.
    """
    rows = [r for r in (recent_audits or []) if isinstance(r, dict)]
    bag = scenarios if isinstance(scenarios, dict) else {}
    pending = pending_fix if isinstance(pending_fix, dict) else {}
    lines = []
    lines.append({
        'level': 'head',
        'text': 'SHADOW LEARN LOG — close → error → map knobs → write before→after',
    })
    if not rows and not pending.get('fingerprint'):
        lines.append({
            'level': 'wait',
            'text': 'waiting for Capital / paper closes — nothing audited yet',
        })
        return lines

    shown = 0
    for a in rows:
        if shown >= max(1, int(limit)):
            break
        when = str(a.get('closed_at') or a.get('at') or '')
        when = when.replace('T', ' ').replace('+00:00', '').replace('Z', '')
        when = (when[11:19] if len(when) >= 19 else when[:16]) or '——:——'
        try:
            pnl = float(a.get('pnl'))
        except (TypeError, ValueError):
            pnl = None
        direction = str(a.get('direction') or '?').upper()
        arrow = '▲BUY' if direction == 'BUY' else (
            '▼SELL' if direction == 'SELL' else direction)
        src = str(a.get('source') or '?')
        scen = str(a.get('scenario') or a.get('fingerprint') or '—')[:28]
        deal = str(a.get('deal_id') or '')[:14]
        pnl_txt = '£—' if pnl is None else f"£{pnl:+.2f}"
        lines.append({
            'level': 'close',
            'text': (f'{when}  CLOSE  {arrow}  {pnl_txt}  [{src}]  '
                     f'{scen}' + (f'  deal={deal}' if deal else '')),
            'pnl': pnl,
        })
        mistakes = [m for m in (a.get('mistakes') or []) if m]
        notes = [n for n in (a.get('notes') or []) if n]
        src_l = str(a.get('source') or '').lower()
        snap_ok = bool(a.get('open_snapshot')) or any(
            str(n) == 'open_snapshot_used' for n in notes)
        journalish = (
            not snap_ok and (
                'journal' in src_l
                or any('journal' in str(n).lower() for n in notes)
                or str(a.get('scenario') or '') == 'LIVE_JOURNAL'))
        if journalish and (not mistakes or mistakes == ['outcome_hurt']):
            if pnl is not None and pnl < -0.02:
                lines.append({
                    'level': 'wait',
                    'text': (f'{when}  WAIT   Capital £ loss — full audit pending '
                             f'(OHLC+structure+news+peers+neural)'),
                })
            elif pnl is not None and pnl > 0.02:
                lines.append({
                    'level': 'ok',
                    'text': f'{when}  OK     Capital £ win — audit pending',
                })
            else:
                lines.append({
                    'level': 'info',
                    'text': f'{when}  INFO   Capital £ flat — audit pending',
                })
        elif snap_ok and mistakes:
            lines.append({
                'level': 'error',
                'text': (f'{when}  ERROR  ' + ' · '.join(str(m) for m in mistakes[:6])
                         + '  [open snapshot]'),
            })
        elif snap_ok and pnl is not None and pnl > 0.02:
            lines.append({
                'level': 'ok',
                'text': f'{when}  OK     open-snapshot audit — useful win',
            })
        elif snap_ok and pnl is not None and pnl < -0.02:
            lines.append({
                'level': 'error',
                'text': f'{when}  ERROR  open-snapshot audit — outcome_hurt',
            })
        elif mistakes:
            lines.append({
                'level': 'error',
                'text': f'{when}  ERROR  ' + ' · '.join(str(m) for m in mistakes[:6]),
            })
        elif pnl is not None and pnl < -0.02:
            lines.append({
                'level': 'error',
                'text': f'{when}  ERROR  outcome_hurt (no tagged class yet)',
            })
        elif pnl is not None and pnl > 0.02:
            lines.append({
                'level': 'ok',
                'text': f'{when}  OK     useful win — no mistake class',
            })
        else:
            lines.append({
                'level': 'info',
                'text': f'{when}  INFO   scratch / flat — light teach only',
            })
        for note in notes[:3]:
            lines.append({
                'level': 'note',
                'text': f'{when}  NOTE   {str(note)[:110]}',
            })
        changed = list(a.get('fix_changed') or [])
        why = a.get('fix_why')
        committed = bool(a.get('committed') or a.get('fix_applied'))
        fp = str(a.get('fingerprint') or '')
        bag_row = bag.get(fp) if fp and isinstance(bag.get(fp), dict) else {}
        if not changed and isinstance(bag_row.get('fix'), dict):
            changed = list((bag_row.get('fix') or {}).get('changed') or [])
        fix_steps = [s for s in (a.get('fix_steps') or []) if isinstance(s, dict)]
        if not fix_steps and journalish and (
                mistakes or (pnl is not None and pnl < -0.02)):
            fix_steps = journal_seed_fix_steps(a, store=store)
        # Prefer durable mutation lines stored on the audit row.
        if (not any(s.get('mutation_lines') for s in fix_steps)
                and (a.get('fix_mutations') or a.get('fix_map_lines'))):
            mut_lines = [
                m.get('line') for m in (a.get('fix_mutations') or [])
                if isinstance(m, dict) and m.get('line')]
            unt_lines = [
                u.get('line') for u in (a.get('fix_untouched') or [])
                if isinstance(u, dict) and u.get('line')]
            for s in fix_steps:
                if s.get('step') in ('propose', 'commit'):
                    s['mutation_lines'] = mut_lines
                    s['untouched_lines'] = unt_lines[:12]
                    s['map_lines'] = list(a.get('fix_map_lines') or [])
                    s['mutations'] = list(a.get('fix_mutations') or [])
        if committed or changed:
            ch = ', '.join(str(c) for c in changed[:8]) if changed else 'flags/knobs'
            lines.append({
                'level': 'fix',
                'text': f'{when}  FIX    COMMITTED · {ch}',
            })
            if why:
                lines.append({
                    'level': 'fix',
                    'text': f'{when}  FIX    why: {str(why)[:110]}',
                })
            for s in fix_steps:
                lines.extend(_format_fix_step_lines(when, s))
            lines.append({
                'level': 'out',
                'text': (f'{when}  OUTCOME next same situation retests with new knobs'
                         + (f' · fp={fp[:10]}' if fp else '')),
            })
        elif mistakes or (pnl is not None and pnl < -0.02):
            pc = a.get('promote_count')
            pn = a.get('promote_need')
            gate = ''
            if pc is not None and pn is not None:
                gate = f' · promote {pc}/{pn}'
            if journalish:
                lines.append({
                    'level': 'wait',
                    'text': (f'{when}  WAIT   Capital £ only — full path after '
                             f'on_close{gate}'),
                })
            else:
                lines.append({
                    'level': 'wait',
                    'text': (f'{when}  WAIT   full path below'
                             f'{gate} — MAP / VALUE / KEEP'),
                })
            if fix_steps:
                for s in fix_steps:
                    lines.extend(_format_fix_step_lines(when, s))
            elif not journalish:
                lines.append({
                    'level': 'note',
                    'text': (f'{when}  STEP   diagnose · ok · '
                             + (' · '.join(str(m) for m in mistakes[:4])
                                or 'outcome_hurt')),
                })
                lines.append({
                    'level': 'wait',
                    'text': (f'{when}  STEP   commit · wait · '
                             'no fix_steps stored — awaiting on_close'),
                })
        # READY only when THIS close stamped it — not sticky bag state on losses.
        if a.get('ready_stamped_this_close') or (
                isinstance(a.get('ready_for_live'), bool)
                and a.get('ready_for_live')
                and a.get('bumped_wins_after_fix')):
            lines.append({
                'level': 'ok',
                'text': f'{when}  READY  fingerprint marked ready_for_live',
            })
        elif bag_row.get('ready_for_live') and (
                pnl is not None and pnl > 0.02):
            lines.append({
                'level': 'ok',
                'text': f'{when}  READY  fingerprint already ready_for_live',
            })
        lines.append({'level': 'sep', 'text': '─' * 64})
        shown += 1

    if pending.get('fingerprint') or pending.get('mutations') or pending.get('map_lines'):
        st = 'COMMITTED' if pending.get('committed') else 'PENDING'
        ch = ', '.join(str(c) for c in (pending.get('changed') or [])[:8]) or '—'
        lines.append({
            'level': 'fix',
            'text': (f'LATEST FIX  {st}  fp={str(pending.get("fingerprint") or "—")[:10]}  '
                     f'changed={ch}'),
        })
        if pending.get('why'):
            lines.append({
                'level': 'fix',
                'text': f'LATEST FIX  why: {str(pending.get("why"))[:120]}',
            })
        miss = pending.get('mistakes') or []
        if miss:
            lines.append({
                'level': 'error',
                'text': 'LATEST FIX  mistakes: ' + ' · '.join(str(m) for m in miss[:6]),
            })
        for ml in (pending.get('map_lines') or [])[:8]:
            lines.append({'level': 'fix', 'text': f'LATEST FIX  MAP    {ml}'})
        for m in (pending.get('mutations') or [])[:8]:
            if isinstance(m, dict) and m.get('line'):
                lines.append({
                    'level': 'fix' if pending.get('committed') else 'wait',
                    'text': f'LATEST FIX  WRITE  {m["line"]}',
                })
        for u in (pending.get('untouched') or [])[:6]:
            if isinstance(u, dict) and u.get('line'):
                lines.append({
                    'level': 'note',
                    'text': f'LATEST FIX  KEEP   {u["line"]}',
                })
    return lines


def lab_payload(store) -> dict:
    """Compact payload for the SHADOW rail cards."""
    if store is None:
        return {'ok': False, 'reason': 'no_store'}
    # Unstick PENDING / empty READY from prior 3× / 2-win gates.
    try:
        flush_full_cycle(store)
    except Exception:
        pass
    last = store.get(LAST_KEY) if isinstance(store.get(LAST_KEY), dict) else {}
    pending = (store.get(PENDING_FIX_KEY)
               if isinstance(store.get(PENDING_FIX_KEY), dict) else {})
    bag = _scenario_bag(store)
    ready = [r for r in (store.get(READY_KEY) or []) if isinstance(r, dict)]
    hist = [h for h in (store.get(HIST_KEY) or []) if isinstance(h, dict)]
    top = sorted(
        bag.values(),
        key=lambda r: int((r or {}).get('mistake_hits') or 0),
        reverse=True)[:6]
    top_rows = []
    for r in top:
        if not isinstance(r, dict):
            continue
        top_rows.append({
            'fingerprint': r.get('fingerprint'),
            'scenario': r.get('scenario'),
            'direction': r.get('direction'),
            'seen': r.get('seen'),
            'mistake_hits': r.get('mistake_hits'),
            'wins': r.get('wins'),
            'losses': r.get('losses'),
            'fix_committed': bool(r.get('fix_committed')),
            'ready_for_live': bool(r.get('ready_for_live')),
            'last_mistakes': r.get('last_mistakes') or [],
        })
    audit = store.get(STORE_KEY) if isinstance(store.get(STORE_KEY), dict) else {}
    # Prefer last hurt for operator display — a +0.00 scratch must not hide
    # the 6 Capital minuses the cycle already (or should have) diagnosed.
    last_hurt = None
    for h in reversed(hist):
        if not isinstance(h, dict):
            continue
        ms = h.get('mistakes') or []
        try:
            pnl_h = float(h.get('pnl'))
        except (TypeError, ValueError):
            pnl_h = None
        if ms or (pnl_h is not None and pnl_h < -0.02):
            last_hurt = h
            break
    display_last = last
    if isinstance(last, dict) and not (last.get('mistakes') or []):
        try:
            last_pnl = float(last.get('pnl'))
        except (TypeError, ValueError):
            last_pnl = None
        if (last_pnl is None or abs(last_pnl) < 0.02) and last_hurt:
            display_last = {
                **last_hurt,
                'display_note': 'showing_last_hurt_not_scratch',
            }
    # Full 24h analysis — audit hist first; if empty (boot / bag wipe) use
    # Capital broker journal so Desk never shows 0/0 while CLIENT CLOSES has £.
    window_24h = _hist_window_stats(hist, hours=24)
    journal_24h = _journal_window_stats(store, hours=24)
    if int(window_24h.get('n') or 0) == 0 and int(journal_24h.get('n') or 0) > 0:
        window_24h = {**journal_24h, 'from_journal': True}
    elif int(journal_24h.get('n') or 0) > int(window_24h.get('n') or 0):
        # Journal has more of today's book than audit hist yet — show journal truth.
        window_24h = {
            **journal_24h,
            'from_journal': True,
            'audit_n': int(window_24h.get('n') or 0),
        }
    else:
        window_24h = {**window_24h, 'from_journal': False,
                      'journal_n': int(journal_24h.get('n') or 0)}
    # Seed recent list from journal when audit hist was wiped but Capital £ exists.
    # Also merge journal closes missing from hist so SHADOW shows ALL prior closes,
    # not only the last few audited fingerprints.
    recent_audits = list(reversed(hist[-16:]))
    if journal_24h.get('recent'):
        # Prefer deal_id alone — audited_at ≠ closed was duplicating live_backfill
        # + live_journal blocks for the same Capital trade.
        seen_deals = set()
        seen_fallback = set()
        for a in recent_audits:
            if not isinstance(a, dict):
                continue
            did = str(a.get('deal_id') or '')
            if did:
                seen_deals.add(did)
            else:
                try:
                    ap = round(float(a.get('pnl') or 0), 4)
                except (TypeError, ValueError):
                    ap = None
                seen_fallback.add((
                    ap, str(a.get('closed_at') or a.get('at') or '')[:16]))
        merged = list(recent_audits)
        for j in journal_24h['recent']:
            if not isinstance(j, dict):
                continue
            did = str(j.get('deal_id') or '')
            if did and did in seen_deals:
                continue
            try:
                jp = round(float(j.get('pnl') or 0), 4)
            except (TypeError, ValueError):
                continue
            if not did:
                fb = (jp, str(j.get('at') or '')[:16])
                if fb in seen_fallback:
                    continue
                seen_fallback.add(fb)
            else:
                seen_deals.add(did)
            # Attach honest cycle steps so CMD never says only "fix pending".
            if not j.get('fix_steps'):
                j = dict(j)
                j['fix_steps'] = journal_seed_fix_steps(j, store=store)
                j['notes'] = list(j.get('notes') or []) + ['journal_ish_used']
            merged.append(j)
        # Newest first by trade close time (not audit runtime).
        def _at_key(r):
            return str((r or {}).get('closed_at')
                       or (r or {}).get('closed')
                       or (r or {}).get('at') or '')
        merged.sort(key=_at_key, reverse=True)
        recent_audits = merged[:24]
    if (not display_last or not display_last.get('pnl')) and recent_audits:
        seed = recent_audits[0]
        display_last = {
            **seed,
            'display_note': 'showing_journal_24h_seed',
        }
    hurt_n = sum(
        1 for h in hist
        if isinstance(h, dict) and (
            (h.get('mistakes') or [])
            or (isinstance(h.get('pnl'), (int, float)) and float(h['pnl']) < -0.02)))
    if hurt_n == 0:
        hurt_n = int(window_24h.get('hurt_n') or window_24h.get('losses') or 0)
    live_ext = store.get('live_external_context') if isinstance(
        store.get('live_external_context'), dict) else {}
    live_news = live_ext.get('news') if isinstance(live_ext.get('news'), dict) else {}
    live_im = live_ext.get('intermarket') if isinstance(
        live_ext.get('intermarket'), dict) else {}
    # Fallback: assess calendar from the client data dir when engine has not
    # written live_external_context yet (Desk open before first snapshot).
    if not live_news.get('status') and getattr(store, 'path', None) is not None:
        try:
            from .external_risk import assess_news
            from .instrument_profile import instrument_profile
            cal = store.path.parent / 'economic_calendar.json'
            mandate = store.get('mandate') if isinstance(store.get('mandate'), dict) else {}
            epic = str(mandate.get('epic') or '')
            currencies = instrument_profile(epic).get('currencies') or []
            live_news = assess_news(
                epic, cal, instrument_currencies=currencies) or {}
        except Exception:
            live_news = {'status': 'NEWS_NA',
                         'reason': 'Calendar optional — closes still analyse'}
    peer_n = 0
    ready_peers = 0
    if isinstance(live_im.get('peers'), list):
        peer_n = len(live_im['peers'])
        ready_peers = sum(1 for p in live_im['peers']
                          if isinstance(p, dict) and p.get('ready'))
    # Honest lab labels: do not advertise WAITING as a gate for the cycle.
    news_lab_status = str(live_news.get('status') or 'NEWS_NA')
    if news_lab_status in ('UNCONFIGURED', 'WAITING', 'CALENDAR_UNAVAILABLE'):
        news_lab_status = 'NEWS_NA'
    peers_lab_status = str(live_im.get('status') or 'PEERS_NA')
    if peers_lab_status in ('WAITING', 'UNCONFIGURED'):
        peers_lab_status = 'PEERS_NA'
    return {
        'ok': True,
        'loop': 'diagnose ALL Capital closes → memory → fix → promote → LIVE',
        'promote_n': PROMOTE_N,
        'ready_n': READY_N,
        'scenarios': len(bag),
        'ready_count': len(ready),
        # Prefer real audit hist; fall back to journal merge length so Desk does
        # not show Audits=0 while Capital 24h already lists closes.
        'hist_n': len(hist) if hist else len(recent_audits),
        'hurt_n': hurt_n,
        'window_24h': window_24h,
        'live_news': {
            'status': news_lab_status,
            'reason': live_news.get('reason') or (
                'context only — not a close gate' if news_lab_status == 'NEWS_NA'
                else None),
            'entry_allowed': live_news.get('entry_allowed'),
            'event_at': live_news.get('event_at'),
        },
        'live_peers': {
            'status': peers_lab_status,
            'reason': live_im.get('reason') or (
                'context only — not a close gate' if peers_lab_status == 'PEERS_NA'
                else None),
            'peer_n': peer_n,
            'ready_peers': ready_peers,
            'entry_allowed': live_im.get('entry_allowed'),
        },
        'last': display_last,
        'last_raw': last,
        'pending_fix': {
            'committed': bool(pending.get('committed')),
            'fingerprint': pending.get('fingerprint'),
            'mistakes': pending.get('mistakes'),
            'changed': pending.get('changed'),
            'at': pending.get('at'),
            'why': ((pending.get('proposed') or {}).get('why')
                    if isinstance(pending.get('proposed'), dict)
                    else pending.get('why')),
            'steps': (display_last.get('fix_steps')
                      if isinstance(display_last, dict)
                      and isinstance(display_last.get('fix_steps'), list)
                      else None),
            'mutations': (pending.get('mutations')
                          or (display_last.get('fix_mutations')
                              if isinstance(display_last, dict) else None)),
            'untouched': (pending.get('untouched')
                          or (display_last.get('fix_untouched')
                              if isinstance(display_last, dict) else None)),
            'map_lines': (pending.get('map_lines')
                          or (display_last.get('fix_map_lines')
                              if isinstance(display_last, dict) else None)),
        },
        'top_scenarios': top_rows,
        'ready': ready[:6],
        'recent_audits': recent_audits,
        'learning_log': build_learning_log(
            recent_audits=recent_audits,
            pending_fix={
                'committed': bool(pending.get('committed')),
                'fingerprint': pending.get('fingerprint'),
                'mistakes': pending.get('mistakes'),
                'changed': pending.get('changed'),
                'why': ((pending.get('proposed') or {}).get('why')
                        if isinstance(pending.get('proposed'), dict)
                        else pending.get('why')),
                'mutations': pending.get('mutations'),
                'untouched': pending.get('untouched'),
                'map_lines': pending.get('map_lines'),
            },
            scenarios=bag,
            limit=12,
            store=store,
        ),
        'respect_news_window': bool(audit.get('respect_news_window')),
        'respect_intermarket_veto': bool(audit.get('respect_intermarket_veto')),
    }


def _hist_window_stats(hist, *, hours=24) -> dict:
    """Aggregate audit hist over a time window — wins and losses together.

    Filter by ``closed_at`` (trade close), not ``audited_at`` / legacy ``at``.
    Late Funds re-audits must not keep a 25h-old close inside “last 24h”.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, int(hours)))
    wins = losses = scratch = 0
    net = 0.0
    n = 0
    useful_n = hurt_n = 0
    for h in hist or []:
        if not isinstance(h, dict):
            continue
        stamp = h.get('closed_at') or h.get('at')
        if stamp:
            try:
                dt = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                pass
        pnl = _finite(h.get('pnl'))
        if pnl is None:
            continue
        n += 1
        net += float(pnl)
        ms = h.get('mistakes') or []
        if pnl > 0.02:
            wins += 1
            useful_n += 1
        elif pnl < -0.02 or ms:
            losses += 1
            hurt_n += 1
        else:
            scratch += 1
    return {
        'hours': int(hours),
        'n': n,
        'wins': wins,
        'losses': losses,
        'scratch': scratch,
        'net': round(net, 4),
        'useful_n': useful_n,
        'hurt_n': hurt_n,
        'reads': 'wins+losses',
        'filter': 'closed_at',
    }


def _journal_window_stats(store, *, hours=24) -> dict:
    """24h Capital broker closes from SQLite — survives audit-hist bag wipes."""
    from datetime import timedelta
    empty = {
        'hours': int(hours), 'n': 0, 'wins': 0, 'losses': 0, 'scratch': 0,
        'net': 0.0, 'useful_n': 0, 'hurt_n': 0, 'reads': 'wins+losses',
        'recent': [],
    }
    if store is None or not hasattr(store, 'db'):
        return empty
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, int(hours)))
    try:
        # Prefer broker £; also keep upl_estimate so Desk is not blank while
        # Capital Funds mirror is still upgrading rows.
        rows = store.db.execute(
            "SELECT deal_id,pnl,direction,entry,exit,closed,pnl_source FROM trades "
            "WHERE closed IS NOT NULL AND pnl IS NOT NULL "
            "ORDER BY closed DESC LIMIT 400").fetchall()
    except Exception:
        return empty
    wins = losses = scratch = 0
    net = 0.0
    n = 0
    recent = []
    broker_n = 0
    for deal_id, pnl, direction, entry, exit_px, closed, src in rows:
        pnl_n = _finite(pnl)
        if pnl_n is None or abs(float(pnl_n)) < 1e-12:
            continue
        src_s = str(src or '').strip().lower()
        if src_s and src_s not in (
                'broker', 'upl_estimate', 'funds', 'capital',
                'pending_broker_pnl', 'pending_close_sync', 'pending_upl_teach'):
            # Ignore synthetic / non-cash teaching rows.
            if src_s not in ('', 'none'):
                continue
        if closed:
            try:
                raw = str(closed).replace('Z', '+00:00').replace(' ', 'T', 1)
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                # Unparseable stamp → still count (better than false 0 today).
                pass
        n += 1
        if src_s == 'broker':
            broker_n += 1
        net += float(pnl_n)
        if pnl_n > 0.02:
            wins += 1
        elif pnl_n < -0.02:
            losses += 1
        else:
            scratch += 1
        if len(recent) < 16:
            recent.append({
                'at': closed,
                'source': 'live_journal',
                'pnl': float(pnl_n),
                'direction': direction,
                'entry': entry,
                'exit': exit_px,
                'pnl_source': src_s or 'broker',
                # No invented mistake class — on_close owns diagnose/MAP.
                'mistakes': [],
                'notes': ['journal_24h_seed', 'awaiting_full_audit'],
                'scenario': 'LIVE_JOURNAL',
                'deal_id': deal_id,
            })
    return {
        'hours': int(hours),
        'n': n,
        'wins': wins,
        'losses': losses,
        'scratch': scratch,
        'net': round(net, 4),
        'useful_n': wins,
        'hurt_n': losses,
        'broker_n': broker_n,
        'reads': 'wins+losses',
        'recent': recent,
    }
