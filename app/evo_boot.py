"""Kick physical evolution — seed + harvest + soft ceilings so evo is not idle.

Without this, fresh Stores wait for: hot soft-ceilings, empty strategy cards,
and a 20-trade trial horizon. Call once per engine / runtime start.
"""
from __future__ import annotations

STORE_FLAG = 'evo_boot_v1'
# Temporary PR #181 OPEN baseline — force into Store even after prior boots.
# Stale self_params_v1 alone kept min_rr=1.5 + flow fight HOLD after DEFAULTS change.
PR181_OPEN_BASELINE = {
    'bracket_min_rr': 1.0,
    'flow_consensus_block_fights': 0,
    'reentry_confirm_enabled': 0,
}


def _force_pr181_open_baseline(store, p, *, out=None):
    """Overwrite OPEN-blocking knobs back to PR #181 seeds."""
    touched = False
    forced = []
    for k, want in PR181_OPEN_BASELINE.items():
        try:
            cur = p.get(k)
            if isinstance(want, float):
                same = abs(float(cur) - float(want)) < 1e-12
            else:
                same = int(cur) == int(want)
        except (TypeError, ValueError):
            same = False
        if not same:
            p[k] = want
            forced.append(k)
            touched = True
    if touched:
        from .self_params import save
        save(store, p, reason='evo_boot_pr181_open_baseline')
        if isinstance(out, dict):
            out['pr181_open'] = 'forced:' + ','.join(forced)
    elif isinstance(out, dict):
        out['pr181_open'] = 'ok'
    return touched


def ensure_physical_evo(store, *, epic=None, force=False) -> dict:
    """Idempotent boot so the robot actually starts evolving on day one."""
    if store is None:
        return {'ok': False, 'reason': 'no_store'}
    prev = store.get(STORE_FLAG) or {}
    if isinstance(prev, dict) and prev.get('done') and not force:
        # Still try samples (idempotent) + harvest so visi_dati upgrades land.
        out = {'ok': True, 'reason': 'already_booted'}
        try:
            from .self_params import load, save, STORE_KEY
            raw = store.get(STORE_KEY) if store is not None else None
            raw_on = (isinstance(raw, dict)
                      and int(raw.get('trail_live_enabled') or 0) != 0)
            p = load(store)
            p['trail_live_enabled'] = 0
            if raw_on:
                save(store, p, reason='evo_boot_trail_hard_off')
                out['trail'] = 'forced_off'
            else:
                out['trail'] = 'off'
            # Re-load after trail save so PR181 force sees latest bag.
            p = load(store)
            _force_pr181_open_baseline(store, p, out=out)
        except Exception as exc:
            out['trail'] = type(exc).__name__
            out['pr181_open'] = type(exc).__name__
        try:
            from . import sample_strategies, strategy_gen
            out['samples'] = sample_strategies.build_strategies_from_samples(
                store, backup_root=None, use_store_trades=True, force=False)
            out['harvest'] = strategy_gen.harvest(store, epic=epic)
        except Exception as exc:
            out['harvest'] = {'ok': False, 'reason': type(exc).__name__}
        return out

    out = {
        'ok': True,
        'soft_ceilings': None,
        'audit_seed': None,
        'harvest': None,
        'params': None,
    }
    try:
        from .self_params import migrate_soft_ceilings, load, save, DEFAULTS
        migrated = migrate_soft_ceilings(store)
        out['soft_ceilings'] = bool(migrated)
        # Ensure audit-seeded knobs are present even on old Store bags.
        p = load(store)
        touched = False
        for k in (
            'self_params_horizon_trades', 'learn_loss_repeats',
            'trail_live_enabled', 'trail_arm_mfe_frac', 'structure_edge_nudge',
            'strategy_gen_enabled', 'reentry_same_dir_atr_frac',
            'flow_consensus_nudge', 'flow_consensus_block_fights',
            'flow_consensus_block_min_agree',
            'flow_pillar_impulse', 'flow_pillar_vol_z',
            'flow_pillar_imbalance', 'flow_pillar_impact',
        ):
            if k not in (store.get('self_params_v1') or {}) and k in DEFAULTS:
                p[k] = DEFAULTS[k]
                touched = True
        # Force horizon down if an old Store still holds 20+ (evo freeze).
        try:
            hz = int(p.get('self_params_horizon_trades') or 8)
        except (TypeError, ValueError):
            hz = 8
        if hz > 12:
            p['self_params_horizon_trades'] = 8
            touched = True
        # Kill trail on every boot — stale Stores may still hold trail_live_enabled=1.
        if int(p.get('trail_live_enabled') or 0) != 0:
            p['trail_live_enabled'] = 0
            touched = True
        # PR #181 OPEN baseline — wipe stale min_rr / flow fight / reentry HOLD.
        for k, want in PR181_OPEN_BASELINE.items():
            try:
                cur = p.get(k)
                if isinstance(want, float):
                    same = abs(float(cur) - float(want)) < 1e-12
                else:
                    same = int(cur) == int(want)
            except (TypeError, ValueError):
                same = False
            if not same:
                p[k] = want
                touched = True
                out['pr181_open'] = 'forced'
        if touched:
            save(store, p, reason='evo_boot_audit_defaults')
            out['params'] = 'seeded'
        else:
            out['params'] = 'ok'
            out.setdefault('pr181_open', 'ok')
    except Exception as exc:
        out['params'] = type(exc).__name__

    try:
        from . import strategy_gen
        from . import sample_strategies
        # Samples first (backups + store trades) → library → cards.
        # No programming — market closes are the strategy material.
        # Always try /tmp/visi-dati (or VISI_DATI) so uploaded market samples
        # become strategy cards on day-one boot — not only thin local store.
        out['samples'] = sample_strategies.build_strategies_from_samples(
            store, backup_root=None, use_store_trades=True, force=False)
        out['audit_seed'] = strategy_gen.seed_audit_cards(store)
        out['harvest'] = strategy_gen.harvest(store, epic=epic)
        if (out['audit_seed'] or {}).get('added') or (
                out.get('samples') or {}).get('ok'):
            try:
                from .learning_generation import bump
                bump(store, 'sample_strategies_evo_boot',
                     kind='architecture_changes')
            except Exception:
                pass
    except Exception as exc:
        out['audit_seed'] = {'ok': False, 'reason': type(exc).__name__}
        out['samples'] = {'ok': False, 'reason': type(exc).__name__}

    store.set(STORE_FLAG, {
        'done': True,
        'at': __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc).isoformat(),
        'epic': epic,
        'summary': {
            'active_cards': len((out.get('harvest') or {}).get('activated') or []),
            'seed_added': len((out.get('audit_seed') or {}).get('added') or []),
        },
    })
    try:
        store.event('learning', 'Physical evo boot — seeds + harvest live', {
            'epic': epic,
            'soft_ceilings': out.get('soft_ceilings'),
            'seed_added': (out.get('audit_seed') or {}).get('added'),
            'activated': (out.get('harvest') or {}).get('activated'),
            'params': out.get('params'),
        })
    except Exception:
        pass
    return out
