"""One unified adaptive system — samples → combinations → LIVE decisions.

This is the single entry point for the market-learning loop built across
audit → teach → strategy_gen → sample ingest → combo adapt:

  BOOT   samples (store + backups) → library → strategy cards
  OPEN   neural scores soft-tilted by: structure EV → GOLD approach →
         strategy cards/combos → M1 multi-source flow consensus
         (Order Flow + Liquidity + Impact from public feeds)
  CLOSE  bank +£/−£ into situation + combo ledger → flip/retire cards →
         next OPEN already changed

No programming of new strategies: profits and losses are the samples;
combinations (structure×side) decide; LIVE vote follows evidence.
"""
from __future__ import annotations

STORE_STATUS_KEY = 'adaptive_system_status_v1'


def boot(store, *, epic=None, force=False) -> dict:
    """Start the full system: soft ceilings, samples, cards."""
    from . import evo_boot
    out = evo_boot.ensure_physical_evo(store, epic=epic, force=force)
    status = {
        'booted': True,
        'epic': epic,
        'evo': {
            'reason': out.get('reason'),
            'params': out.get('params'),
            'samples_ok': bool((out.get('samples') or {}).get('ok')),
            'active_cards': len(
                ((out.get('harvest') or {}).get('activated') or [])),
            'seed_added': len((out.get('audit_seed') or {}).get('added') or []),
        },
    }
    if store is not None:
        try:
            store.set(STORE_STATUS_KEY, status)
            store.event('learning', 'Adaptive system boot complete', status)
        except Exception:
            pass
    return {'ok': True, **status, 'raw': out}


def nudge_open(store, scores, *, epic=None, structure=None, trend=None,
               candles=None, edge_advice=None, liquidity_path=None,
               bar_time=None, proposed=None) -> tuple:
    """Unified OPEN soft-nudge: structure EV → GOLD → combos → M1 flow consensus.

    Returns (scores, meta). Never hard-vetoes — only tilts the simplex.
    """
    meta = {
        'system': 'adaptive_system',
        'structure': structure,
        'stages': {},
    }
    if not isinstance(scores, dict):
        return scores, {**meta, 'nudged': False, 'reason': 'no_scores'}
    out = dict(scores)
    nudged_any = False

    # 1) Library structure×side EV (historic samples).
    try:
        from . import structure_edge
        advice = edge_advice
        if not isinstance(advice, dict):
            st = structure or 'CHOP'
            advice = structure_edge.compare_frame(
                store, epic=epic, structure=st) if store else {'advice': None}
            meta['structure'] = st
        out, m = structure_edge.soft_nudge_scores(
            out, advice, store=store, live_trend=trend)
        meta['stages']['structure_edge'] = m
        meta['edge_advice'] = {
            'advice': (advice or {}).get('advice'),
            'confidence': (advice or {}).get('confidence'),
            'toxic_opposite': (advice or {}).get('toxic_opposite'),
            'structure': (advice or {}).get('structure') or structure,
        }
        if m.get('nudged'):
            nudged_any = True
        if not structure:
            structure = (advice or {}).get('structure') or structure
    except Exception as exc:
        meta['stages']['structure_edge'] = {
            'nudged': False, 'reason': type(exc).__name__}

    # 2) GOLD BOS/CHoCH preferred approach (soft).
    try:
        from . import gold_strategies
        if gold_strategies.applies_to_epic(epic):
            try:
                prop = max(('BUY', 'SELL', 'HOLD'),
                           key=lambda a: float(out.get(a, 0) or 0))
            except Exception:
                prop = 'HOLD'
            gate = gold_strategies.evaluate(
                epic=epic,
                action=prop if prop in ('BUY', 'SELL') else 'BUY',
                candles=candles,
                structure=structure,
                store=store)
            out, m = gold_strategies.soft_nudge_scores(out, gate, store=store)
            meta['stages']['gold'] = m
            meta['gold_gate'] = {
                k: gate.get(k) for k in (
                    'structure', 'preferred', 'soft_reject', 'reason', 'allowed')
            }
            if m.get('nudged'):
                nudged_any = True
            if gate.get('structure'):
                structure = gate.get('structure')
        else:
            meta['stages']['gold'] = {'nudged': False, 'reason': 'not_gold'}
    except Exception as exc:
        meta['stages']['gold'] = {'nudged': False, 'reason': type(exc).__name__}

    # 3) Strategy cards + win/loss combo ledger (live adapt).
    try:
        from . import strategy_gen
        live_struct = structure or 'CHOP'
        out, m = strategy_gen.soft_nudge_scores(
            out, structure=live_struct, epic=epic, store=store)
        meta['stages']['strategy_gen'] = m
        meta['structure'] = live_struct
        if m.get('nudged'):
            nudged_any = True
    except Exception as exc:
        meta['stages']['strategy_gen'] = {
            'nudged': False, 'reason': type(exc).__name__}

    # 4) M1 multi-source Order Flow + Liquidity + Impact consensus (soft).
    try:
        from . import flow_consensus
        try:
            prop = (proposed or '').upper()
            if prop not in ('BUY', 'SELL'):
                prop = max(('BUY', 'SELL', 'HOLD'),
                           key=lambda a: float(out.get(a, 0) or 0))
        except Exception:
            prop = 'HOLD'
        if liquidity_path is not None and bar_time is not None:
            consensus = flow_consensus.evaluate_from_path(
                liquidity_path, bar_time, epic=epic,
                proposed=prop if prop in ('BUY', 'SELL') else None,
                structure=structure, trend=trend)
        else:
            consensus = {
                'ok': False, 'reason': 'liquidity_path_or_bar_missing',
                'pillars': [], 'providers': flow_consensus.provider_catalog(),
            }
        out, m = flow_consensus.soft_nudge_scores(
            out, consensus, store=store)
        meta['stages']['flow_consensus'] = m
        meta['flow_consensus'] = flow_consensus.desk_payload(consensus)
        if m.get('nudged'):
            nudged_any = True
    except Exception as exc:
        meta['stages']['flow_consensus'] = {
            'nudged': False, 'reason': type(exc).__name__}

    meta['nudged'] = nudged_any
    meta['scores'] = {k: out.get(k) for k in ('BUY', 'SELL', 'HOLD')}
    return out, meta


def learn_close(store, *, structure, direction, broker_pnl, epic=None,
                situation=None, deal_id=None) -> dict:
    """Unified CLOSE learn: bank ±£ into situation + combos; flip dead strategies."""
    out = {
        'ok': True,
        'system': 'adaptive_system',
        'situation': None,
        'strategy': None,
    }
    pnl = broker_pnl
    # Situation scene×action £.
    try:
        from . import situation_memory
        if situation and direction in ('BUY', 'SELL') and pnl is not None:
            out['situation'] = situation_memory.record_outcome(
                store, situation=situation, action=direction, pnl=pnl,
                epic=epic, deal_id=deal_id)
    except Exception as exc:
        out['situation'] = {'ok': False, 'reason': type(exc).__name__}

    # Combo ledger + card flip/retire + harvest.
    try:
        from . import strategy_gen
        out['strategy'] = strategy_gen.observe_close(
            store, structure=structure, direction=direction,
            broker_pnl=pnl, epic=epic)
    except Exception as exc:
        out['strategy'] = {'ok': False, 'reason': type(exc).__name__}

    if store is not None:
        try:
            prev = store.get(STORE_STATUS_KEY) or {}
            if not isinstance(prev, dict):
                prev = {}
            flips = ((out.get('strategy') or {}).get('flips') or [])
            prev['last_close'] = {
                'structure': structure, 'direction': direction,
                'pnl': pnl, 'flips': flips,
                'combo': ((out.get('strategy') or {}).get('combo') or {}).get(
                    'mean_pnl'),
            }
            store.set(STORE_STATUS_KEY, prev)
        except Exception:
            pass
    return out


def snapshot(store) -> dict:
    """Operator view of the unified system state."""
    from . import strategy_gen
    from . import sample_strategies
    bag = strategy_gen.load(store)
    status = store.get(STORE_STATUS_KEY) if store is not None else None
    combos = bag.get('combos') or {}
    paying = []
    bleeding = []
    for k, c in combos.items():
        if not isinstance(c, dict):
            continue
        mean = c.get('mean_pnl')
        n = int(c.get('samples') or 0)
        if mean is None or n < 2:
            continue
        row = {
            'key': k, 'mean_pnl': round(float(mean), 4),
            'samples': n, 'wins': int(c.get('wins') or 0),
            'losses': int(c.get('losses') or 0),
        }
        if mean > 0:
            paying.append(row)
        elif mean < 0:
            bleeding.append(row)
    paying.sort(key=lambda r: r['mean_pnl'], reverse=True)
    bleeding.sort(key=lambda r: r['mean_pnl'])
    active = []
    for cid in bag.get('active_ids') or []:
        c = (bag.get('cards') or {}).get(cid) or {}
        active.append({
            'id': cid,
            'prefer': c.get('prefer'),
            'avoid': c.get('avoid'),
            'structure': c.get('structure'),
            'live_n': int(c.get('live_n') or 0),
            'live_pnl': c.get('live_pnl'),
            'status': c.get('status'),
        })
    return {
        'system': 'adaptive_system',
        'library_samples': sample_strategies.library_sample_count(store),
        'combo_count': len(combos),
        'paying': paying[:12],
        'bleeding': bleeding[:12],
        'active_cards': active,
        'retired_n': len(bag.get('retired_ids') or []),
        'status': status if isinstance(status, dict) else {},
    }
