"""Per-instrument store bags — prevent GOLD↔SILVER (etc.) state mixing.

Neural weight files are already epic-scoped. These helpers park / restore the
*shared* learning bags (P&L history, shadow, SL/TP params, feature registry,
discovery, model validation stack) when the operator switches markets.

Live keys stay unscoped for backward-compatible readers; each instrument's
snapshot lives under ``instr:<epic>:<key>``. The active epic is recorded in
``instrument_bag_epic`` so a restart mid-mandate reloads the right bag.
"""
from __future__ import annotations

ACTIVE_EPIC_KEY = 'instrument_bag_epic'

# Shared keys that must not bleed across instruments.
BAG_KEYS = (
    'profit_learning_history_v1',
    'shadow_paper_journal_v1',
    'shadow_paper_state_v1',
    'shadow_neural_state_v1',
    'shadow_self_dev_last',
    'shadow_last_promote_v1',
    'feature_registry_v1',
    'feature_discovery_state_v1',
    'feature_discovery_rejected_v1',
    'feature_validation_log_v1',
    'feature_validation_attempts_v1',
    'model_validation_log_v1',
    'model_version_stack_v1',
    'bracket_kappa',
    'bracket_sl_scale',
    'bracket_mult_kappa',
    'bracket_mult_sl',
    'bracket_learn_reason',
    'bracket_outcome_history',
)


def bag_key(base: str, epic: str) -> str:
    return f'instr:{epic}:{base}'


def park_instrument_bag(store, epic) -> int:
    """Copy current live bag into the epic-scoped snapshot. Returns keys parked."""
    if store is None or not epic:return 0
    n=0
    for key in BAG_KEYS:
        store.set(bag_key(key,epic),store.get(key))
        n+=1
    store.set(ACTIVE_EPIC_KEY,epic)
    return n


def _clear_live_bag(store):
    for key in BAG_KEYS:
        store.set(key,None)


def load_instrument_bag(store, epic) -> int:
    """Load epic snapshot into live keys (or clear live for a fresh instrument)."""
    if store is None:return 0
    if not epic:
        _clear_live_bag(store)
        store.set(ACTIVE_EPIC_KEY,None)
        return 0
    n=0
    for key in BAG_KEYS:
        parked=store.get(bag_key(key,epic))
        # Missing snapshot → fresh empty bag for this instrument.
        store.set(key,parked)
        n+=1
    store.set(ACTIVE_EPIC_KEY,epic)
    return n


def switch_instrument_bag(store, previous_epic, new_epic) -> dict:
    """Park previous live bag, then load (or reset) the new instrument's bag."""
    if store is None:
        return {'parked':0,'loaded':0,'previous':previous_epic,'current':new_epic}
    if previous_epic and previous_epic==new_epic:
        store.set(ACTIVE_EPIC_KEY,new_epic)
        return {'parked':0,'loaded':0,'previous':previous_epic,'current':new_epic,
                'unchanged':True}
    parked=park_instrument_bag(store,previous_epic) if previous_epic else 0
    loaded=load_instrument_bag(store,new_epic)
    if store is not None and hasattr(store,'event'):
        store.event('instrument','Instrument learning bag switched',
            {'previous_epic':previous_epic,'new_epic':new_epic,
             'parked_keys':parked,'loaded_keys':loaded,
             'bag_keys':list(BAG_KEYS)})
    return {'parked':parked,'loaded':loaded,
            'previous':previous_epic,'current':new_epic}


def label_backup_meta(meta: dict, epic) -> dict:
    """Ensure model backup metadata carries an explicit instrument label."""
    out=dict(meta or {})
    if epic:
        out['epic']=epic
        out['instrument']=epic
    return out
