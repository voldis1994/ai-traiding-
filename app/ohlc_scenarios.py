"""1m OHLC scenario layer + self-audit when analysis was wrong.

Scenarios (guide): TREND_UP/DOWN, RANGE, BREAKOUT_*, RETEST_*, CHOCH_*,
FAKEOUT, COMPRESSION. Entry policy prefers pullback/retest confirm; blocks
mid-range chase.

Self-audit on close: compare OPEN-time scenario + zone vs post-trade OHLC
path and £. Persist mistakes so next gates tighten (robot understands it
mis-read the zone / scenario).
"""
from __future__ import annotations

import math

from . import structure_memory

STORE_KEY = 'ohlc_scenarios_v1'
HIST_KEY = 'ohlc_scenarios_v1:hist'
MISTAKES_KEY = 'ohlc_scenarios_v1:mistakes'

DEFAULTS = {
    'enabled': 1,
    'block_mid_range': 1,
    'prefer_retest_over_blind_break': 1,
    'fakeout_block_conf': 0.55,
    'min_rr_hint': 2.0,
    # Learned penalties (0..1) — raised after wrong analysis.
    'penalty_trend': 0.0,
    'penalty_range': 0.0,
    'penalty_breakout': 0.0,
    'penalty_retest': 0.0,
    'penalty_zone': 0.0,
}


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clip01(v):
    n = _finite(v)
    if n is None:
        return 0.0
    return max(0.0, min(1.0, float(n)))


def load_params(store=None) -> dict:
    out = dict(DEFAULTS)
    if store is None:
        return out
    bag = store.get(STORE_KEY)
    if isinstance(bag, dict):
        for k in out:
            if bag.get(k) is not None:
                if k in ('enabled', 'block_mid_range', 'prefer_retest_over_blind_break'):
                    out[k] = int(bag[k])
                else:
                    out[k] = float(bag[k])
    for k in ('penalty_trend', 'penalty_range', 'penalty_breakout',
              'penalty_retest', 'penalty_zone', 'fakeout_block_conf'):
        out[k] = _clip01(out[k])
    out['enabled'] = int(out.get('enabled') or 0)
    out['block_mid_range'] = int(out.get('block_mid_range') or 0)
    out['prefer_retest_over_blind_break'] = int(
        out.get('prefer_retest_over_blind_break') or 0)
    return out


def save_params(store, params: dict):
    if store is None or not isinstance(params, dict):
        return
    clean = load_params(None)
    for k in clean:
        if params.get(k) is not None:
            clean[k] = params[k]
    for k in ('penalty_trend', 'penalty_range', 'penalty_breakout',
              'penalty_retest', 'penalty_zone', 'fakeout_block_conf'):
        clean[k] = _clip01(clean[k])
    clean['enabled'] = int(clean.get('enabled') or 0)
    clean['block_mid_range'] = int(clean.get('block_mid_range') or 0)
    clean['prefer_retest_over_blind_break'] = int(
        clean.get('prefer_retest_over_blind_break') or 0)
    store.set(STORE_KEY, clean)


def _atr(path, period=14):
    if len(path) < 3:
        return None
    trs = []
    prev = path[0]['close']
    for b in path[1:]:
        tr = max(b['high'] - b['low'], abs(b['high'] - prev), abs(b['low'] - prev))
        trs.append(tr)
        prev = b['close']
    n = min(max(2, int(period)), len(trs))
    return sum(trs[-n:]) / float(n)


def _range_box(path, lookback=24):
    """Horizontal support/resistance from recent swing + path extremes."""
    window = path[-max(8, int(lookback)):]
    if len(window) < 8:
        return None
    bos = structure_memory.bos_choch(window, left=2, right=2) or {}
    hi = _finite(bos.get('range_hi'))
    lo = _finite(bos.get('range_lo'))
    if hi is None or lo is None or not (hi > lo):
        hi = max(b['high'] for b in window)
        lo = min(b['low'] for b in window)
    if not (hi > lo):
        return None
    atr = _atr(window) or (hi - lo)
    # Contained closes → RANGE character.
    closes = [b['close'] for b in window]
    inside = sum(1 for c in closes if lo <= c <= hi)
    frac = inside / float(len(closes))
    span = hi - lo
    compression = span <= 1.35 * atr if atr else False
    return {
        'lo': float(lo), 'hi': float(hi), 'span': float(span),
        'atr': float(atr) if atr else None,
        'inside_frac': float(frac),
        'compression': bool(compression and frac >= 0.7),
    }


def classify(candles, *, mid=None, zones=None, tick=0.01, store=None) -> dict:
    """Classify live 1m scenario from completed OHLC (+ optional entry zones)."""
    path = structure_memory.normalise(candles, limit=80)
    cfg = load_params(store)
    out = {
        'ready': False,
        'scenario': 'UNKNOWN',
        'scenarios': [],
        'bias': None,
        'event': None,
        'range': None,
        'retest': None,
        'fakeout': False,
        'compression': False,
        'entry_hint': None,
        'block_mid_range': bool(cfg.get('block_mid_range')),
        'confidence': 0.0,
        'params': {
            'penalty_trend': cfg.get('penalty_trend'),
            'penalty_range': cfg.get('penalty_range'),
            'penalty_breakout': cfg.get('penalty_breakout'),
            'penalty_retest': cfg.get('penalty_retest'),
            'penalty_zone': cfg.get('penalty_zone'),
        },
        'enabled': bool(cfg.get('enabled')),
    }
    if len(path) < 16:
        out['reason'] = 'too_few_bars'
        return out
    struct = structure_memory.label_swing_structure(path, left=2, right=2) or {}
    bos = structure_memory.bos_choch(path, left=2, right=2) or {}
    box = _range_box(path)
    out['bias'] = struct.get('bias') or bos.get('bias')
    out['event'] = bos.get('event')
    out['range'] = box
    out['retest'] = bos.get('retest')
    out['fakeout'] = bool(bos.get('false_break'))
    out['compression'] = bool(box and box.get('compression'))
    out['structure'] = {
        'last_high_tag': struct.get('last_high_tag'),
        'last_low_tag': struct.get('last_low_tag'),
        'hh_count': struct.get('hh_count'),
        'hl_count': struct.get('hl_count'),
        'lh_count': struct.get('lh_count'),
        'll_count': struct.get('ll_count'),
    }
    tags = []
    bias = str(out.get('bias') or 'RANGE').upper()
    event = str(out.get('event') or '').upper()
    retest = str(out.get('retest') or '').upper() if out.get('retest') else None

    if out['fakeout']:
        tags.append('FAKEOUT')
    if out['compression']:
        tags.append('COMPRESSION')

    if event in ('CHOCH_UP', 'CHOCH_DOWN'):
        tags.append(event)
    elif event in ('BOS_UP', 'BOS_DOWN'):
        tags.append('BREAKOUT_UP' if event == 'BOS_UP' else 'BREAKOUT_DOWN')

    if retest == 'UP':
        tags.append('RETEST_UP')
    elif retest == 'DOWN':
        tags.append('RETEST_DOWN')

    if bias == 'UP' and 'BREAKOUT_UP' not in tags and 'CHOCH_UP' not in tags:
        tags.append('TREND_UP')
    elif bias == 'DOWN' and 'BREAKOUT_DOWN' not in tags and 'CHOCH_DOWN' not in tags:
        tags.append('TREND_DOWN')
    elif bias == 'RANGE' or (box and box.get('inside_frac', 0) >= 0.75
                             and 'BREAKOUT_UP' not in tags
                             and 'BREAKOUT_DOWN' not in tags):
        if 'RANGE' not in tags:
            tags.append('RANGE')

    # Primary scenario = highest-priority tag.
    priority = (
        'FAKEOUT', 'CHOCH_UP', 'CHOCH_DOWN',
        'RETEST_UP', 'RETEST_DOWN',
        'BREAKOUT_UP', 'BREAKOUT_DOWN',
        'COMPRESSION', 'TREND_UP', 'TREND_DOWN', 'RANGE',
    )
    primary = 'RANGE'
    for p in priority:
        if p in tags:
            primary = p
            break
    out['scenario'] = primary
    out['scenarios'] = tags
    out['ready'] = True

    # Confidence: structure clarity minus learned penalties.
    conf = 0.55
    if primary.startswith('TREND'):
        conf = 0.70 - float(cfg.get('penalty_trend') or 0)
    elif primary == 'RANGE':
        conf = 0.60 - float(cfg.get('penalty_range') or 0)
    elif primary.startswith('BREAKOUT'):
        conf = 0.65 - float(cfg.get('penalty_breakout') or 0)
    elif primary.startswith('RETEST'):
        conf = 0.75 - float(cfg.get('penalty_retest') or 0)
    elif primary.startswith('CHOCH'):
        conf = 0.68 - float(cfg.get('penalty_trend') or 0)
    elif primary == 'FAKEOUT':
        conf = 0.72
    elif primary == 'COMPRESSION':
        conf = 0.50
    if zones and isinstance(zones, dict) and zones.get('ok'):
        conf = min(1.0, conf + 0.05) - float(cfg.get('penalty_zone') or 0) * 0.5
    out['confidence'] = _clip01(conf)

    # Entry hint for the neural side.
    if primary in ('TREND_UP', 'RETEST_UP', 'CHOCH_UP', 'BREAKOUT_UP'):
        out['entry_hint'] = 'BUY'
    elif primary in ('TREND_DOWN', 'RETEST_DOWN', 'CHOCH_DOWN', 'BREAKOUT_DOWN'):
        out['entry_hint'] = 'SELL'
    elif primary == 'RANGE':
        out['entry_hint'] = 'EDGE'  # only zone edges
    elif primary == 'FAKEOUT':
        # Fade the fake — opposite of pierce direction when known.
        if retest == 'UP':
            out['entry_hint'] = 'SELL'
        elif retest == 'DOWN':
            out['entry_hint'] = 'BUY'
        else:
            out['entry_hint'] = 'WAIT'
    else:
        out['entry_hint'] = 'WAIT'
    out['reason'] = primary.lower()
    return out


def mid_range_blocked(action, scenario: dict, price, *, tick=0.01) -> tuple:
    """Guide mistake: do not enter in the middle of a range.

    Only blocks when price is *inside* the box away from edges. Outside the
    box (breakout chase / far from range) is not a mid-range entry.
    """
    if not isinstance(scenario, dict) or not scenario.get('block_mid_range'):
        return False, None
    if str(scenario.get('scenario') or '') not in ('RANGE', 'COMPRESSION'):
        return False, None
    box = scenario.get('range') or {}
    lo = _finite(box.get('lo'))
    hi = _finite(box.get('hi'))
    p = _finite(price)
    if lo is None or hi is None or p is None or not (hi > lo):
        return False, None
    try:
        tick_f = max(float(tick or 0.01), 1e-9)
    except (TypeError, ValueError):
        tick_f = 0.01
    span = hi - lo
    edge = max(tick_f * 4, 0.18 * span)
    # Outside the range entirely → not a mid-range mistake (break/retest path).
    if p < lo - tick_f or p > hi + tick_f:
        return False, None
    # Allowed near edges only.
    near_lo = p <= lo + edge
    near_hi = p >= hi - edge
    if near_lo or near_hi:
        return False, None
    return True, f'wait_mid_range (scenario={scenario.get("scenario")})'


def scenario_allows_trigger(action, scenario: dict, trigger: str, *, store=None) -> dict:
    """Whether break|zone trigger fits the live scenario (+ learned penalties)."""
    cfg = load_params(store)
    action = (action or '').upper()
    trigger = str(trigger or '').lower()
    out = {'ok': True, 'reason': 'scenario_ok', 'scenario': None}
    if not isinstance(scenario, dict) or not scenario.get('ready'):
        out['reason'] = 'scenario_unready'
        return out
    if not scenario.get('enabled', True):
        out['reason'] = 'scenario_disabled'
        return out
    name = str(scenario.get('scenario') or 'UNKNOWN')
    out['scenario'] = name
    hint = str(scenario.get('entry_hint') or '')
    conf = float(scenario.get('confidence') or 0)

    if name == 'FAKEOUT':
        # Blind break into a fakeout is banned; zone fade may pass.
        thr = float(cfg.get('fakeout_block_conf') or 0.55)
        if trigger == 'break' and conf >= thr:
            out['ok'] = False
            out['reason'] = 'block_fakeout_break'
            return out

    if name == 'RANGE' and trigger == 'break':
        # Prefer wait for edge/zone unless strong BOS already in tags.
        tags = scenario.get('scenarios') or []
        if 'BREAKOUT_UP' not in tags and 'BREAKOUT_DOWN' not in tags:
            prefer = int(cfg.get('prefer_retest_over_blind_break') or 1)
            if prefer:
                out['ok'] = False
                out['reason'] = 'wait_range_edge_or_retest'
                return out

    if hint == 'BUY' and action == 'SELL' and conf >= 0.65:
        out['ok'] = False
        out['reason'] = f'scenario_conflict ({name} prefers BUY)'
        return out
    if hint == 'SELL' and action == 'BUY' and conf >= 0.65:
        out['ok'] = False
        out['reason'] = f'scenario_conflict ({name} prefers SELL)'
        return out
    if hint == 'WAIT' and trigger == 'break':
        out['ok'] = False
        out['reason'] = f'wait_scenario ({name})'
        return out
    return out


def open_meta_from_scenario(scenario: dict) -> dict:
    if not isinstance(scenario, dict):
        return {}
    return {
        'scenario': scenario.get('scenario'),
        'scenarios': list(scenario.get('scenarios') or []),
        'bias': scenario.get('bias'),
        'event': scenario.get('event'),
        'retest': scenario.get('retest'),
        'fakeout': bool(scenario.get('fakeout')),
        'compression': bool(scenario.get('compression')),
        'entry_hint': scenario.get('entry_hint'),
        'confidence': scenario.get('confidence'),
        'range': scenario.get('range'),
        'structure': scenario.get('structure'),
    }


def audit_close(store, *, scenario_meta=None, ohlc_meta=None,
                candles=None, broker_pnl=None, spread=0.0,
                entry=None, exit_px=None, action=None) -> dict:
    """Did OPEN-time scenario/zone analysis match post-trade reality?

    Marks mistakes:
      • zone_fail — price sliced through the entry zone (no hold)
      • scenario_fail — regime flipped against the stamped scenario
      • mid_range_mistake — entered mid-box in RANGE
      • fakeout_missed — false break not recognized
    Then raises the matching penalty so the robot gets stricter.
    """
    meta_s = scenario_meta if isinstance(scenario_meta, dict) else {}
    meta_o = ohlc_meta if isinstance(ohlc_meta, dict) else {}
    if not meta_s and not meta_o:
        return {'audited': False, 'reason': 'no_meta'}
    pnl = _finite(broker_pnl)
    if pnl is None:
        return {'audited': False, 'reason': 'no_pnl'}
    cost = abs(_finite(spread) or 0.0)
    hurt = pnl < -max(0.02, 0.25 * cost)
    useful = pnl > max(0.02, 0.25 * cost)
    action = (action or meta_o.get('action') or '').upper()
    path = structure_memory.normalise(candles, limit=80) if candles else []
    now = classify(path, store=store) if path else {'ready': False}
    mistakes = []
    notes = []

    stamped = str(meta_s.get('scenario') or '')
    live = str(now.get('scenario') or '') if now.get('ready') else ''
    # Zone fail: BUY entry zone but post path made LL through zone / big loss.
    zone = meta_o.get('zone') if isinstance(meta_o.get('zone'), dict) else None
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
    if stamped == 'RANGE' and meta_o.get('trigger') == 'break' and hurt:
        mistakes.append('mid_range_mistake')
        notes.append('blind_break_in_range')
    if stamped.startswith('RETEST') and hurt and useful is False:
        # Soft: retest didn't hold.
        if 'zone_fail' not in mistakes:
            mistakes.append('retest_fail')
            notes.append('retest_did_not_hold')

    if not mistakes and not hurt and not useful:
        return {'audited': True, 'mistakes': [], 'reason': 'scratch', 'adapted': False}
    if not mistakes and useful:
        # Correct analysis — ease penalties slightly.
        params = load_params(store)
        for k in ('penalty_trend', 'penalty_range', 'penalty_breakout',
                  'penalty_retest', 'penalty_zone'):
            params[k] = _clip01(float(params[k]) - 0.01)
        save_params(store, params)
        return {
            'audited': True, 'mistakes': [], 'reason': 'analysis_ok',
            'adapted': True, 'notes': ['ease_penalties_after_win'],
            'params': params, 'live_scenario': live,
        }
    if not mistakes and hurt:
        # Loss without clear structural miss — mild zone penalty.
        mistakes.append('outcome_hurt')
        notes.append('loss_without_clear_structure_miss')

    params = load_params(store)
    adapted = False
    for m in mistakes:
        if m == 'zone_fail':
            params['penalty_zone'] = _clip01(float(params['penalty_zone']) + 0.08)
            adapted = True
        elif m == 'scenario_fail':
            params['penalty_trend'] = _clip01(float(params['penalty_trend']) + 0.06)
            adapted = True
        elif m == 'fakeout_missed':
            params['penalty_breakout'] = _clip01(float(params['penalty_breakout']) + 0.08)
            params['fakeout_block_conf'] = _clip01(
                float(params['fakeout_block_conf']) - 0.02)
            params['prefer_retest_over_blind_break'] = 1
            adapted = True
        elif m == 'mid_range_mistake':
            params['penalty_range'] = _clip01(float(params['penalty_range']) + 0.08)
            params['block_mid_range'] = 1
            params['prefer_retest_over_blind_break'] = 1
            adapted = True
        elif m == 'retest_fail':
            params['penalty_retest'] = _clip01(float(params['penalty_retest']) + 0.06)
            adapted = True
        elif m == 'outcome_hurt':
            params['penalty_zone'] = _clip01(float(params['penalty_zone']) + 0.03)
            adapted = True
    if adapted:
        save_params(store, params)
    row = {
        'pnl': pnl, 'hurt': hurt, 'useful': useful,
        'stamped': stamped, 'live': live,
        'mistakes': mistakes, 'notes': notes,
        'action': action, 'trigger': meta_o.get('trigger'),
        'params': {
            'penalty_zone': params['penalty_zone'],
            'penalty_trend': params['penalty_trend'],
            'penalty_range': params['penalty_range'],
            'penalty_breakout': params['penalty_breakout'],
            'penalty_retest': params['penalty_retest'],
        },
    }
    if store is not None:
        hist = list(store.get(HIST_KEY) or [])
        hist.append(row)
        store.set(HIST_KEY, hist[-50:])
        if mistakes:
            miss = list(store.get(MISTAKES_KEY) or [])
            miss.append(row)
            store.set(MISTAKES_KEY, miss[-30:])
            store.event('ohlc_scenarios', 'Self-audit: scenario/zone mistake', {
                'mistakes': mistakes, 'notes': notes,
                'stamped': stamped, 'live': live, 'pnl': pnl,
            })
        else:
            store.event('ohlc_scenarios', 'Self-audit: analysis ok', {
                'stamped': stamped, 'live': live, 'pnl': pnl,
            })
    return {
        'audited': True, 'adapted': adapted, 'mistakes': mistakes,
        'notes': notes, 'params': params, 'live_scenario': live,
        'reason': 'mistakes' if mistakes else 'ok',
    }
