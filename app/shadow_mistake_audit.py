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

PROMOTE_N = 3   # same fingerprint+mistake repeats before settings change
READY_N = 2     # wins after committed fix → ready_for_live


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


def fingerprint(*, scenario=None, zone_side=None, news_status=None,
                intermarket_status=None, direction=None, trigger=None) -> str:
    """Stable short id for a market-situation class (not a trade id)."""
    parts = [
        str(scenario or 'UNK'),
        str(zone_side or 'mid'),
        str(news_status or 'news?'),
        str(intermarket_status or 'im?'),
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
    trigger = meta_o.get('trigger')
    zside = _zone_side(zone, mid=mid)
    news_status = str(news.get('status') or 'UNCONFIGURED')
    im_status = str(intermarket.get('status') or 'WAITING')
    # Assess peer fight against the trade direction when peers are ready.
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

    if stamped.startswith('TREND') and live.startswith('CHOCH') and hurt:
        mistakes.append('scenario_fail')
        notes.append(f'trend_stamped_but_{live}')
    if stamped.startswith('BREAKOUT') and live == 'FAKEOUT' and hurt:
        mistakes.append('fakeout_missed')
        notes.append('breakout_was_fakeout')
    if stamped == 'RANGE' and ctx.get('trigger') == 'break' and hurt:
        mistakes.append('mid_range_mistake')
        notes.append('blind_break_in_range')
    if stamped.startswith('RETEST') and hurt and not useful:
        if 'zone_fail' not in mistakes:
            mistakes.append('retest_fail')
            notes.append('retest_did_not_hold')

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
        mistakes.append('outcome_hurt')
        notes.append('loss_without_clear_structure_miss')

    proposed = propose_fix(mistakes)
    return {
        'audited': True, 'mistakes': mistakes, 'notes': notes,
        'reason': 'mistakes' if mistakes else 'ok',
        'hurt': hurt, 'useful': useful, 'pnl': pnl_n,
        'source': source, 'context': ctx, 'proposed_fix': proposed,
    }


def propose_fix(mistakes) -> dict | None:
    """Map mistake classes → only the knobs that address them (not £ chase)."""
    if not mistakes:
        return None
    deltas = {}
    flags = {}
    for m in mistakes:
        if m == 'zone_fail':
            deltas['penalty_zone'] = deltas.get('penalty_zone', 0) + 0.08
        elif m == 'scenario_fail':
            deltas['penalty_trend'] = deltas.get('penalty_trend', 0) + 0.06
        elif m == 'fakeout_missed':
            deltas['penalty_breakout'] = deltas.get('penalty_breakout', 0) + 0.08
            deltas['fakeout_block_conf'] = deltas.get('fakeout_block_conf', 0) - 0.02
            flags['prefer_retest_over_blind_break'] = 1
        elif m == 'mid_range_mistake':
            deltas['penalty_range'] = deltas.get('penalty_range', 0) + 0.08
            flags['block_mid_range'] = 1
            flags['prefer_retest_over_blind_break'] = 1
        elif m == 'retest_fail':
            deltas['penalty_retest'] = deltas.get('penalty_retest', 0) + 0.06
        elif m == 'outcome_hurt':
            deltas['penalty_zone'] = deltas.get('penalty_zone', 0) + 0.03
        elif m == 'news_risk_ignored':
            flags['respect_news_window'] = 1
            deltas['news_block_weight'] = deltas.get('news_block_weight', 0) + 0.1
        elif m == 'intermarket_fight':
            flags['respect_intermarket_veto'] = 1
            deltas['intermarket_veto_weight'] = (
                deltas.get('intermarket_veto_weight', 0) + 0.1)
    if not deltas and not flags:
        return None
    return {
        'deltas': deltas,
        'flags': flags,
        'mistakes': list(mistakes),
        'why': 'targeted_to_mistake_class_not_pnl',
    }


def _scenario_bag(store) -> dict:
    raw = store.get(SCENARIOS_KEY) if store is not None else None
    return dict(raw) if isinstance(raw, dict) else {}


def scenario_memory_save(store, diagnosis: dict) -> dict:
    """Persist fingerprint counts + last mistake for promote / ready gates."""
    if store is None or not isinstance(diagnosis, dict) or not diagnosis.get('audited'):
        return {'saved': False}
    ctx = diagnosis.get('context') if isinstance(diagnosis.get('context'), dict) else {}
    fp = str(ctx.get('fingerprint') or '')
    if not fp:
        return {'saved': False, 'reason': 'no_fingerprint'}
    bag = _scenario_bag(store)
    row = dict(bag.get(fp) or {})
    row['fingerprint'] = fp
    row['scenario'] = ctx.get('stamped_scenario') or ctx.get('live_scenario')
    row['zone_side'] = ctx.get('zone_side')
    row['news_status'] = ctx.get('news_status')
    row['intermarket_status'] = ctx.get('intermarket_status')
    row['direction'] = ctx.get('direction')
    row['trigger'] = ctx.get('trigger')
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
    if diagnosis.get('useful'):
        row['wins'] = int(row.get('wins') or 0) + 1
        if row.get('fix_committed'):
            row['wins_after_fix'] = int(row.get('wins_after_fix') or 0) + 1
    if diagnosis.get('hurt'):
        row['losses'] = int(row.get('losses') or 0) + 1
    row['last_pnl'] = diagnosis.get('pnl')
    row['last_source'] = diagnosis.get('source')
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
    return {'saved': True, 'fingerprint': fp, 'seen': row['seen'], 'row': row}


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


def apply_targeted_fix(store, diagnosis: dict, *, commit=False) -> dict:
    """Apply proposed knobs only when commit=True (after promote_gate)."""
    proposed = diagnosis.get('proposed_fix') if isinstance(diagnosis, dict) else None
    if not isinstance(proposed, dict):
        return {'applied': False, 'reason': 'no_proposal'}
    if not commit:
        if store is not None:
            store.set(PENDING_FIX_KEY, {
                'at': _now_iso(),
                'proposed': proposed,
                'fingerprint': (diagnosis.get('context') or {}).get('fingerprint'),
                'mistakes': diagnosis.get('mistakes'),
                'committed': False,
            })
        return {'applied': False, 'pending': True, 'proposed': proposed}

    params = ohlc_scenarios.load_params(store)
    deltas = proposed.get('deltas') or {}
    flags = proposed.get('flags') or {}
    changed = []
    for k, d in deltas.items():
        if k in ('penalty_zone', 'penalty_trend', 'penalty_range',
                 'penalty_breakout', 'penalty_retest', 'fakeout_block_conf'):
            params[k] = _clip01(float(params.get(k) or 0) + float(d))
            changed.append(k)
        # news / intermarket weights live in audit bag (not ohlc_scenarios).
    for k, v in flags.items():
        if k in ('block_mid_range', 'prefer_retest_over_blind_break'):
            params[k] = int(v)
            changed.append(k)
    if changed:
        ohlc_scenarios.save_params(store, params)

    bag = _scenario_bag(store)
    fp = str((diagnosis.get('context') or {}).get('fingerprint') or '')
    if fp and fp in bag:
        row = dict(bag[fp])
        row['fix_committed'] = True
        row['fix_at'] = _now_iso()
        row['fix'] = {'deltas': deltas, 'flags': flags, 'changed': changed}
        bag[fp] = row
        store.set(SCENARIOS_KEY, bag)

    # Persist soft news/intermarket respect flags on audit store.
    audit = dict(store.get(STORE_KEY) or {}) if store is not None else {}
    if flags.get('respect_news_window'):
        audit['respect_news_window'] = 1
        w = _clip01(float(audit.get('news_block_weight') or 0)
                    + float(deltas.get('news_block_weight') or 0))
        audit['news_block_weight'] = w
    if flags.get('respect_intermarket_veto'):
        audit['respect_intermarket_veto'] = 1
        w = _clip01(float(audit.get('intermarket_veto_weight') or 0)
                    + float(deltas.get('intermarket_veto_weight') or 0))
        audit['intermarket_veto_weight'] = w
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
        })
        store.event('shadow_mistake_audit', 'Targeted fix committed', {
            'fingerprint': fp, 'changed': changed,
            'mistakes': diagnosis.get('mistakes'),
        })
    return {'applied': True, 'changed': changed, 'params': params, 'proposed': proposed}


def _update_ready(store, diagnosis: dict) -> dict | None:
    """Mark fingerprint ready_for_live after enough wins post-fix."""
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
    wins_after = int(row.get('wins_after_fix') or 0)
    if wins_after < READY_N:
        return {'fingerprint': fp, 'wins_after_fix': wins_after, 'need': READY_N}
    ready = list(store.get(READY_KEY) or [])
    entry = {
        'fingerprint': fp,
        'scenario': row.get('scenario'),
        'direction': row.get('direction'),
        'zone_side': row.get('zone_side'),
        'wins_after_fix': wins_after,
        'ready_at': _now_iso(),
        'source': diagnosis.get('source'),
    }
    ready = [r for r in ready if isinstance(r, dict)
             and r.get('fingerprint') != fp]
    ready.insert(0, entry)
    store.set(READY_KEY, ready[:20])
    row = dict(row)
    row['ready_for_live'] = True
    bag[fp] = row
    store.set(SCENARIOS_KEY, bag)
    store.event('shadow_mistake_audit', 'Scenario ready for LIVE', entry)
    return entry


def on_close(store, *, pnl, spread=0.0, candles=None, scenario_meta=None,
             ohlc_meta=None, news=None, intermarket=None, direction=None,
             mid=None, entry=None, exit=None, exit_reason=None,
             source='shadow',
             apply_ohlc_penalties=True) -> dict:
    """Full loop: diagnose → memory → promote_gate → optional commit → ready."""
    diagnosis = diagnose(
        pnl=pnl, spread=spread, candles=candles,
        scenario_meta=scenario_meta, ohlc_meta=ohlc_meta,
        news=news, intermarket=intermarket, direction=direction,
        mid=mid, entry=entry, store=store, source=source)
    if not diagnosis.get('audited'):
        return diagnosis

    mem = scenario_memory_save(store, diagnosis)
    gate = {'ready': False}
    commit_result = {'applied': False, 'pending': False}
    if diagnosis.get('mistakes') and diagnosis.get('proposed_fix'):
        fp = (diagnosis.get('context') or {}).get('fingerprint')
        gate = promote_gate(store, fp, diagnosis['mistakes'])
        if apply_ohlc_penalties and gate.get('ready'):
            commit_result = apply_targeted_fix(store, diagnosis, commit=True)
        else:
            commit_result = apply_targeted_fix(store, diagnosis, commit=False)

    ready = None
    if diagnosis.get('useful'):
        ready = _update_ready(store, diagnosis)

    out = {
        **diagnosis,
        'memory': mem,
        'promote_gate': gate,
        'fix': commit_result,
        'ready_for_live': ready,
        'at': _now_iso(),
    }
    if store is not None:
        hist = list(store.get(HIST_KEY) or [])
        hist.append({
            'at': out['at'],
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
        })
        store.set(HIST_KEY, hist[-120:])  # ~24h of closes for analysis
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
            'promote_count': gate.get('count'),
            'promote_need': gate.get('need'),
            'ready_for_live': bool(ready),
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
            })
    return out


def lab_payload(store) -> dict:
    """Compact payload for the SHADOW rail cards."""
    if store is None:
        return {'ok': False, 'reason': 'no_store'}
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
    recent_audits = list(reversed(hist[-16:]))
    if not recent_audits and journal_24h.get('recent'):
        recent_audits = list(journal_24h['recent'])
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
            cal = store.path.parent / 'economic_calendar.json'
            live_news = assess_news('', cal) or {}
        except Exception:
            live_news = {'status': 'UNCONFIGURED',
                         'reason': 'Calendar not readable yet'}
    peer_n = 0
    ready_peers = 0
    if isinstance(live_im.get('peers'), list):
        peer_n = len(live_im['peers'])
        ready_peers = sum(1 for p in live_im['peers']
                          if isinstance(p, dict) and p.get('ready'))
    return {
        'ok': True,
        'loop': 'diagnose → scenario memory → targeted fix → promote → LIVE',
        'promote_n': PROMOTE_N,
        'ready_n': READY_N,
        'scenarios': len(bag),
        'ready_count': len(ready),
        'hist_n': len(hist),
        'hurt_n': hurt_n,
        'window_24h': window_24h,
        'live_news': {
            'status': live_news.get('status') or '—',
            'reason': live_news.get('reason'),
            'entry_allowed': live_news.get('entry_allowed'),
            'event_at': live_news.get('event_at'),
        },
        'live_peers': {
            'status': live_im.get('status') or 'WAITING',
            'reason': live_im.get('reason'),
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
                    if isinstance(pending.get('proposed'), dict) else None),
        },
        'top_scenarios': top_rows,
        'ready': ready[:6],
        'recent_audits': recent_audits,
        'respect_news_window': bool(audit.get('respect_news_window')),
        'respect_intermarket_veto': bool(audit.get('respect_intermarket_veto')),
    }


def _hist_window_stats(hist, *, hours=24) -> dict:
    """Aggregate audit hist over a time window — wins and losses together."""
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
        at = h.get('at')
        if at:
            try:
                dt = datetime.fromisoformat(str(at).replace('Z', '+00:00'))
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
            "ORDER BY closed DESC LIMIT 160").fetchall()
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
        if src_s and src_s not in ('broker', 'upl_estimate', 'funds', 'capital'):
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
                'mistakes': ['outcome_hurt'] if pnl_n < -0.02 else [],
                'notes': ['journal_24h_seed'],
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
