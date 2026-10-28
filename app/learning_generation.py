"""Visible learning generation — advances when the *brain* actually changes.

`code_generation` alone only counted shadow Python policy file swaps. LIVE entry
uses the neural vote, so that counter could sit still while weights/features/
brackets/self_params moved. UI "Paaudze" must track real self-development.
"""
STORE_KEY = 'learning_generation'


def current(store):
    if store is None:
        return 0
    try:
        return int(store.get(STORE_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def bump(store, reason):
    """+1 when a durable learning artifact advances."""
    if store is None:
        return 0
    gen = current(store) + 1
    store.set(STORE_KEY, gen)
    store.event('learning', 'Learning generation advanced',
                {'generation': gen, 'reason': str(reason or '')[:160]})
    return gen


def display(store, policy_generation=0):
    """Number shown as Paaudze: max(brain advances, policy file gen)."""
    try:
        policy = int(policy_generation or 0)
    except (TypeError, ValueError):
        policy = 0
    return max(current(store), policy)
