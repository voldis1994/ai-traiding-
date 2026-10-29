"""Visible learning generation — advances when the *brain* actually changes.

`code_generation` alone only counted shadow Python policy file swaps. LIVE entry
uses the neural vote, so that counter could sit still while weights/features/
brackets/self_params moved. UI "Paaudze" must track real self-development.

Separate counters keep training / architecture / param / verified-promote
advances visible without inflating Paaudze on identical weight re-copies.
"""
STORE_KEY = 'learning_generation'
COUNTERS_KEY = 'learning_generation_counters_v1'
LAST_WEIGHT_CHECKSUM_KEY = 'learning_generation_last_weight_checksum'

# reason substring → counter bucket
_REASON_KIND = (
    ('shadow_weights_promoted', 'verified_candidate_promotes'),
    ('verified_candidate_promote', 'verified_candidate_promotes'),
    ('feature_registry', 'architecture_changes'),
    ('neuron', 'architecture_changes'),
    ('expand', 'architecture_changes'),
    ('architecture', 'architecture_changes'),
    ('bracket_shape', 'param_changes'),
    ('self_params', 'param_changes'),
    ('param', 'param_changes'),
    ('training', 'training_steps'),
    ('closed_trade', 'training_steps'),
    ('learn', 'training_steps'),
    ('m1_horizon', 'training_steps'),
)


def current(store):
    if store is None:
        return 0
    try:
        return int(store.get(STORE_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def counters(store):
    """Per-kind Paaudze tallies — never conflated into one opaque bump."""
    empty = {
        'training_steps': 0,
        'architecture_changes': 0,
        'param_changes': 0,
        'verified_candidate_promotes': 0,
    }
    if store is None:
        return empty
    raw = store.get(COUNTERS_KEY) or {}
    if not isinstance(raw, dict):
        return empty
    out = dict(empty)
    for k in empty:
        try:
            out[k] = int(raw.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _kind_for_reason(reason, kind=None):
    if kind in ('training_steps', 'architecture_changes',
                'param_changes', 'verified_candidate_promotes'):
        return kind
    text = str(reason or '').lower()
    for needle, bucket in _REASON_KIND:
        if needle in text:
            return bucket
    return 'training_steps'


def bump(store, reason, *, kind=None, weight_checksum=None):
    """+1 when a durable learning artifact advances.

    Identical weight re-copy (same checksum as last promote/bump) is a no-op so
    Paaudze does not inflate on repeated shadow→LIVE copies of the same brain.
    """
    if store is None:
        return current(store)
    bucket = _kind_for_reason(reason, kind)
    checksum = str(weight_checksum) if weight_checksum else None
    if checksum:
        last = store.get(LAST_WEIGHT_CHECKSUM_KEY)
        if last is not None and str(last) == checksum:
            store.event('learning', 'Learning generation skipped identical weight re-copy',
                        {'reason': str(reason or '')[:160], 'checksum': checksum[:32]})
            return current(store)
    gen = current(store) + 1
    store.set(STORE_KEY, gen)
    tallies = counters(store)
    tallies[bucket] = int(tallies.get(bucket) or 0) + 1
    store.set(COUNTERS_KEY, tallies)
    if checksum:
        store.set(LAST_WEIGHT_CHECKSUM_KEY, checksum)
    store.event('learning', 'Learning generation advanced',
                {'generation': gen, 'reason': str(reason or '')[:160],
                 'kind': bucket, 'counters': tallies})
    return gen


def display(store, policy_generation=0):
    """Paaudze = brain advances only — never inflate with policy-file gen.

    Policy Python swaps are research and do not change LIVE neural entry;
    showing max(brain, policy) made 500+ generations look like progress.
    `policy_generation` is kept for callers/UI that show it separately.
    """
    del policy_generation  # explicit: unused for Paaudze
    return current(store)
