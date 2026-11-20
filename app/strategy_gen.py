"""Generate trading strategy cards from evidence — soft LIVE use, no .py invent.

Honest scope:
  • Harvest structure×side £ buckets (structure_shadow library) into rule cards
  • Activate cards that clear sample/mean floors (self_params seeds)
  • Soft-nudge LIVE scores when live OHLC structure matches a card
  • Track live broker £ per card → demote if the card bleeds

This is **not** LLM code-writing. It is the next evo after audit defaults:
the robot proposes / activates / retires approach rules from outcomes and
physically tilts the LIVE vote with them.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import structure_shadow as ss

STORE_KEY = 'strategy_gen_v1'
LAST_ADVICE_KEY = 'strategy_gen_last_advice_v1'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _cfg(store=None):
    from .self_params import load
    return load(store)


def _empty():
    return {
        'cards': {},
        'active_ids': [],
        'retired_ids': [],
        'updated_at': None,
        'harvest_count': 0,
    }


def load(store):
    if store is None:
        return _empty()
    raw = store.get(STORE_KEY) or {}
    if not isinstance(raw, dict):
        return _empty()
    out = _empty()
    cards = raw.get('cards') if isinstance(raw.get('cards'), dict) else {}
    out['cards'] = {str(k): dict(v) for k, v in cards.items() if isinstance(v, dict)}
    out['active_ids'] = [str(x) for x in (raw.get('active_ids') or [])]
    out['retired_ids'] = [str(x) for x in (raw.get('retired_ids') or [])]
    out['updated_at'] = raw.get('updated_at')
    try:
        out['harvest_count'] = int(raw.get('harvest_count') or 0)
    except (TypeError, ValueError):
        out['harvest_count'] = 0
    return out


def save(store, bag):
    if store is None:
        return bag
    clean = {
        'cards': bag.get('cards') or {},
        'active_ids': list(bag.get('active_ids') or []),
        'retired_ids': list(bag.get('retired_ids') or []),
        'updated_at': bag.get('updated_at') or _now(),
        'harvest_count': int(bag.get('harvest_count') or 0),
    }
    store.set(STORE_KEY, clean)
    return clean


def card_id(*, epic, structure, prefer):
    ep = str(epic or 'UNKNOWN').upper()
    st = str(structure or 'CHOP').upper()
    pf = str(prefer or 'HOLD').upper()
    return f'{ep}|{st}|{pf}'


def _parse_frame_key(key: str):
    """structure_shadow keys look like EPIC|STRUCTURE|DIRECTION."""
    parts = str(key or '').split('|')
    if len(parts) < 3:
        return None
    return {
        'epic': parts[0],
        'structure': parts[1],
        'direction': parts[2],
    }


def propose_from_library(store, *, epic=None) -> list:
    """Build candidate cards from structure library EV — one prefer per frame."""
    cfg = _cfg(store)
    need = max(4, int(cfg.get('strategy_gen_min_samples', 8)))
    min_mean = float(cfg.get('strategy_gen_min_mean', 0.08))
    lib = ss.load_library(store)
    buckets = lib.get('buckets') if isinstance(lib, dict) else {}
    if not isinstance(buckets, dict):
        return []
    # Group BUY/SELL stats by epic|structure.
    frames = {}
    for key, b in buckets.items():
        if not isinstance(b, dict):
            continue
        parsed = _parse_frame_key(key)
        if not parsed:
            continue
        if epic and str(parsed['epic']).upper() != str(epic).upper():
            # Soft match: GOLD vs CS.D.USCGC etc — compare via structure_shadow epic norm if any
            ep_u = str(parsed['epic']).upper()
            want = str(epic).upper()
            if want not in ep_u and ep_u not in want and 'GOLD' not in ep_u and 'XAU' not in ep_u:
                if 'GOLD' in want or 'XAU' in want:
                    if 'GOLD' not in ep_u and 'XAU' not in ep_u:
                        continue
                else:
                    continue
        n = int(b.get('samples') or 0)
        total = _finite(b.get('total_pnl')) or 0.0
        if n <= 0:
            continue
        mean = total / n
        fk = f"{parsed['epic']}|{parsed['structure']}"
        side = str(parsed['direction']).upper()
        if side not in ('BUY', 'SELL'):
            continue
        slot = frames.setdefault(fk, {
            'epic': parsed['epic'], 'structure': parsed['structure'],
            'BUY': None, 'SELL': None,
        })
        slot[side] = {
            'samples': n, 'mean_pnl': mean, 'total_pnl': total,
            'win_count': int(b.get('win_count') or 0),
            'loss_count': int(b.get('loss_count') or 0),
        }

    proposals = []
    for fk, slot in frames.items():
        buy, sell = slot.get('BUY'), slot.get('SELL')
        ranked = []
        for side, st in (('BUY', buy), ('SELL', sell)):
            if not st or st['samples'] < need:
                continue
            ranked.append((side, float(st['mean_pnl']), st))
        if not ranked:
            continue
        ranked.sort(key=lambda x: x[1], reverse=True)
        best_side, best_ev, best_st = ranked[0]
        other = ranked[1] if len(ranked) > 1 else None
        prefer = None
        avoid = None
        if best_ev >= min_mean:
            prefer = best_side
            if other is not None and other[1] < 0:
                avoid = other[0]
        elif other is not None and other[1] < 0 and best_ev <= 0:
            prefer = 'HOLD'
            avoid = best_side if best_ev <= other[1] else other[0]
        else:
            continue
        cid = card_id(epic=slot['epic'], structure=slot['structure'], prefer=prefer)
        proposals.append({
            'id': cid,
            'epic': slot['epic'],
            'structure': slot['structure'],
            'prefer': prefer,
            'avoid': avoid,
            'mean_pnl': best_ev if prefer in ('BUY', 'SELL') else 0.0,
            'samples': int(best_st['samples']),
            'evidence': {'buy': buy, 'sell': sell},
            'source': 'structure_library',
            'status': 'candidate',
        })
    proposals.sort(key=lambda c: (float(c.get('mean_pnl') or 0), int(c.get('samples') or 0)),
                   reverse=True)
    return proposals


# Distilled from GOLD audit — birth seeds so evo has cards before library fills.
AUDIT_SEED_CARDS = (
    {
        'epic': 'GOLD', 'structure': 'TREND_DOWN', 'prefer': 'SELL', 'avoid': 'BUY',
        'mean_pnl': 0.31, 'samples': 111, 'source': 'gold_audit_seed',
    },
    {
        'epic': 'GOLD', 'structure': 'BREAKOUT_DOWN', 'prefer': 'SELL', 'avoid': 'BUY',
        'mean_pnl': 0.25, 'samples': 40, 'source': 'gold_audit_seed',
    },
    {
        'epic': 'GOLD', 'structure': 'TREND_UP', 'prefer': 'BUY', 'avoid': 'SELL',
        'mean_pnl': 0.20, 'samples': 40, 'source': 'gold_audit_seed',
    },
    {
        'epic': 'GOLD', 'structure': 'BREAKOUT_UP', 'prefer': 'BUY', 'avoid': 'SELL',
        'mean_pnl': 0.18, 'samples': 30, 'source': 'gold_audit_seed',
    },
    {
        'epic': 'GOLD', 'structure': 'CHOP', 'prefer': 'HOLD', 'avoid': None,
        'mean_pnl': 0.0, 'samples': 50, 'source': 'gold_audit_seed',
    },
)


def seed_audit_cards(store) -> dict:
    """Install audit-distilled cards once so LIVE soft-nudge has something to evolve."""
    bag = load(store)
    cards = dict(bag.get('cards') or {})
    added = []
    for seed in AUDIT_SEED_CARDS:
        cid = card_id(epic=seed['epic'], structure=seed['structure'],
                      prefer=seed['prefer'])
        if cid in cards and cards[cid].get('status') == 'active':
            continue
        if cid in cards and cards[cid].get('source') not in (
                None, 'gold_audit_seed', 'structure_library'):
            continue
        prev = cards.get(cid) or {}
        card = {
            'id': cid,
            'epic': seed['epic'],
            'structure': seed['structure'],
            'prefer': seed['prefer'],
            'avoid': seed.get('avoid'),
            'mean_pnl': float(seed.get('mean_pnl') or 0),
            'samples': int(seed.get('samples') or 0),
            'source': seed.get('source') or 'gold_audit_seed',
            'status': 'active',
            'live_pnl': _finite(prev.get('live_pnl')) or 0.0,
            'live_n': int(prev.get('live_n') or 0),
            'evidence': prev.get('evidence') or {},
            'updated_at': _now(),
        }
        cards[cid] = card
        added.append(cid)
    if not added and bag.get('active_ids'):
        return {'ok': True, 'added': [], 'reason': 'already_seeded'}
    cfg = _cfg(store)
    max_active = max(4, int(cfg.get('strategy_gen_max_active', 12)))
    active = [cid for cid, c in cards.items() if c.get('status') == 'active']
    # Prefer higher mean_pnl when capping.
    active_sorted = sorted(
        active,
        key=lambda i: (float(cards[i].get('mean_pnl') or 0),
                       int(cards[i].get('samples') or 0)),
        reverse=True)[:max_active]
    bag = {
        'cards': cards,
        'active_ids': active_sorted,
        'retired_ids': [c for c, v in cards.items() if v.get('status') == 'retired'],
        'updated_at': _now(),
        'harvest_count': int(bag.get('harvest_count') or 0),
        'audit_seeded': True,
    }
    save(store, bag)
    return {'ok': True, 'added': added, 'active_ids': active_sorted}


def harvest(store, *, epic=None) -> dict:
    """Propose + activate cards that clear floors; persist in Store."""
    cfg = _cfg(store)
    if not int(cfg.get('strategy_gen_enabled', 1)):
        return {'ok': False, 'reason': 'disabled', 'activated': []}
    bag = load(store)
    props = propose_from_library(store, epic=epic)
    max_active = max(4, int(cfg.get('strategy_gen_max_active', 12)))
    cards = dict(bag.get('cards') or {})
    # Keep prior active seeds (audit) — empty library must not wipe them.
    for cid, prev in list(cards.items()):
        if prev.get('status') == 'retired':
            continue
        live_n = int(prev.get('live_n') or 0)
        live_pnl = _finite(prev.get('live_pnl')) or 0.0
        if live_n >= 6 and live_pnl < -abs(float(cfg.get('strategy_gen_min_mean', 0.08)) * 4):
            prev = dict(prev)
            prev['status'] = 'retired'
            prev['retire_reason'] = 'live_bleed'
            prev['updated_at'] = _now()
            cards[cid] = prev
    for p in props:
        cid = p['id']
        prev = cards.get(cid) or {}
        live_pnl = _finite(prev.get('live_pnl')) or 0.0
        live_n = int(prev.get('live_n') or 0)
        card = dict(prev)
        card.update(p)
        card['live_pnl'] = live_pnl
        card['live_n'] = live_n
        card['updated_at'] = _now()
        if live_n >= 6 and live_pnl < -abs(float(cfg.get('strategy_gen_min_mean', 0.08)) * 4):
            card['status'] = 'retired'
            card['retire_reason'] = 'live_bleed'
        else:
            card['status'] = 'active'
        cards[cid] = card

    activated = [cid for cid, c in cards.items() if c.get('status') == 'active']
    active_sorted = sorted(
        [cards[i] for i in activated if i in cards],
        key=lambda c: (float(c.get('mean_pnl') or 0), int(c.get('samples') or 0)),
        reverse=True)[:max_active]
    active_ids = [c['id'] for c in active_sorted]
    retired = [cid for cid, c in cards.items() if c.get('status') == 'retired']
    bag = {
        'cards': cards,
        'active_ids': active_ids,
        'retired_ids': retired,
        'updated_at': _now(),
        'harvest_count': int(bag.get('harvest_count') or 0) + 1,
        'audit_seeded': bool(bag.get('audit_seeded')),
    }
    save(store, bag)
    return {
        'ok': True,
        'proposed': len(props),
        'activated': active_ids,
        'retired': retired,
        'harvest_count': bag['harvest_count'],
    }


def active_for_structure(store, *, structure, epic=None) -> list:
    bag = load(store)
    st = str(structure or '').upper()
    if not st:
        return []
    out = []
    for cid in bag.get('active_ids') or []:
        c = (bag.get('cards') or {}).get(cid)
        if not isinstance(c, dict):
            continue
        if str(c.get('structure') or '').upper() != st:
            continue
        if epic:
            ep = str(c.get('epic') or '').upper()
            want = str(epic).upper()
            if want not in ep and ep not in want:
                if not (('GOLD' in want or 'XAU' in want) and ('GOLD' in ep or 'XAU' in ep)):
                    continue
        out.append(c)
    return out


def soft_nudge_scores(scores, *, structure, epic=None, store=None):
    """Tilt LIVE scores using active strategy cards for this OHLC frame."""
    if not isinstance(scores, dict):
        return scores, {'nudged': False, 'reason': 'no_scores'}
    cfg = _cfg(store)
    if not int(cfg.get('strategy_gen_enabled', 1)):
        return dict(scores), {'nudged': False, 'reason': 'disabled'}
    strength = float(cfg.get('strategy_gen_nudge', 0.14))
    if strength <= 0:
        return dict(scores), {'nudged': False, 'reason': 'zero_strength'}
    cards = active_for_structure(store, structure=structure, epic=epic)
    if not cards:
        # Lazy harvest once when empty so first LIVE day can still learn.
        harvest(store, epic=epic)
        cards = active_for_structure(store, structure=structure, epic=epic)
    if not cards:
        return dict(scores), {'nudged': False, 'reason': 'no_active_cards'}
    out = {k: float(scores.get(k) or 0) for k in ('BUY', 'SELL', 'HOLD')}
    applied = []
    for c in cards:
        prefer = str(c.get('prefer') or '').upper()
        avoid = str(c.get('avoid') or '').upper()
        # Scale by library mean (cap) so strong EV cards teach louder.
        mean = abs(_finite(c.get('mean_pnl')) or 0.0)
        scale = min(2.5, 1.0 + mean / 0.25)
        delta = strength * scale
        if prefer in ('BUY', 'SELL'):
            out[prefer] = out.get(prefer, 0) + delta
            if avoid in ('BUY', 'SELL'):
                out[avoid] = out.get(avoid, 0) - 0.85 * delta
            else:
                other = 'SELL' if prefer == 'BUY' else 'BUY'
                out[other] = out.get(other, 0) - 0.5 * delta
        elif prefer == 'HOLD':
            out['HOLD'] = out.get('HOLD', 0) + delta
            out['BUY'] = out.get('BUY', 0) - 0.4 * delta
            out['SELL'] = out.get('SELL', 0) - 0.4 * delta
        applied.append({'id': c.get('id'), 'prefer': prefer, 'delta': delta})
    for k in out:
        out[k] = max(0.0, float(out[k]))
    total = sum(out.values())
    if total <= 1e-12:
        return dict(scores), {'nudged': False, 'reason': 'degenerate'}
    out = {k: v / total for k, v in out.items()}
    meta = {
        'nudged': True,
        'cards': applied,
        'structure': structure,
        'renormalized': True,
        'sum': sum(out.values()),
    }
    if store is not None:
        try:
            store.set(LAST_ADVICE_KEY, dict(meta, at=_now()))
        except Exception:
            pass
    return out, meta


def observe_close(store, *, structure, direction, broker_pnl, epic=None):
    """Attribute live £ to matching active cards (promote/demote later harvest)."""
    pnl = _finite(broker_pnl)
    if pnl is None or store is None:
        return {'ok': False, 'reason': 'no_pnl'}
    st = str(structure or '').upper()
    if not st:
        return {'ok': False, 'reason': 'no_structure'}
    bag = load(store)
    touched = []
    for cid in list(bag.get('active_ids') or []):
        c = (bag.get('cards') or {}).get(cid)
        if not isinstance(c, dict):
            continue
        if str(c.get('structure') or '').upper() != st:
            continue
        prefer = str(c.get('prefer') or '').upper()
        # Credit/debit when the trade sided with (or against) the card.
        if prefer in ('BUY', 'SELL') and direction in ('BUY', 'SELL'):
            if direction == prefer:
                c['live_pnl'] = (_finite(c.get('live_pnl')) or 0.0) + pnl
            elif direction == (c.get('avoid') or ''):
                # Took the avoided side — card was right if we lost.
                c['live_pnl'] = (_finite(c.get('live_pnl')) or 0.0) - pnl
            else:
                continue
        elif prefer == 'HOLD':
            # Directional trade in a HOLD card frame — penalize if loss.
            if pnl < 0:
                c['live_pnl'] = (_finite(c.get('live_pnl')) or 0.0) + pnl
            else:
                continue
        else:
            continue
        c['live_n'] = int(c.get('live_n') or 0) + 1
        c['updated_at'] = _now()
        bag['cards'][cid] = c
        touched.append(cid)
    if touched:
        save(store, bag)
    return {'ok': True, 'touched': touched, 'pnl': pnl}
