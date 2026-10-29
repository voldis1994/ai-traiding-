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
    """Paaudze = brain advances only — never inflate with policy-file gen.

    Policy Python swaps are research and do not change LIVE neural entry;
    showing max(brain, policy) made 500+ generations look like progress.
    `policy_generation` is kept for callers/UI that show it separately.
    """
    del policy_generation  # explicit: unused for Paaudze
    return current(store)
