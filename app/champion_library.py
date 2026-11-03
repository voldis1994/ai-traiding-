"""Multi-champion library — profitable versions keyed by market context.

Not one forever-champion. Several proven snapshots live side-by-side; LIVE
loads a version only when the current task/market fingerprint matches the
context that version earned money in (trend × regime × news × epic).

Save gate = broker £ in that context (rolling window), not M1 theater.
Activate gate = exact (or soft) context match while flat — never mid-trade.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'champion_library_v1'
ACTIVE_KEY = 'champion_library_active_v1'
TRADE_LOG_KEY = 'champion_context_trades_v1'

# Soft rails — library stays finite; contexts stay coarse enough to match.
MAX_CHAMPIONS = 24
MAX_TRADE_LOG = 400
DEFAULT_MIN_TRADES = 6
DEFAULT_MIN_NET = 0.15  # £ — above scratch noise
DEFAULT_MIN_WIN_RATE = 0.5


def _now():
    return datetime.now(timezone.utc).isoformat()


def news_bucket(news) -> str:
    """Coarse news task: CLEAR vs window vs blocked/unavailable."""
    if not isinstance(news, dict):
        return 'UNKNOWN'
    status = str(news.get('status') or '').upper()
    if status in ('CLEAR',):
        return 'CLEAR'
    if status in ('NEWS_WINDOW',):
        return 'WINDOW'
    if not news.get('entry_allowed', True):
        return 'BLOCK'
    return status or 'UNKNOWN'


def context_from(*, epic=None, trend=None, regime=None, news=None) -> dict:
    """Fingerprint of the trading task — what the version must match later."""
    ep = str(epic or '').strip() or 'UNKNOWN'
    tr = str(trend or 'MIXED').upper()
    if tr not in ('UP', 'DOWN', 'MIXED'):
        tr = 'MIXED'
    rg = str(regime or 'ALIGNED').upper()
    if rg not in ('ALIGNED', 'SHIFT'):
        rg = 'ALIGNED'
    nb = news_bucket(news)
    return {
        'epic': ep,
        'trend': tr,
        'regime': rg,
        'news': nb,
        'key': f'{ep}|{tr}|{rg}|{nb}',
    }


def context_key(ctx) -> str:
    if isinstance(ctx, dict) and ctx.get('key'):
        return str(ctx['key'])
    return context_from(
        epic=(ctx or {}).get('epic') if isinstance(ctx, dict) else None,
        trend=(ctx or {}).get('trend') if isinstance(ctx, dict) else None,
        regime=(ctx or {}).get('regime') if isinstance(ctx, dict) else None,
        news=(ctx or {}).get('news') if isinstance(ctx, dict) else None,
    )['key']


def soft_key(key: str) -> str:
    """Drop news bucket — match on epic|trend|regime when exact miss."""
    parts = str(key or '').split('|')
    if len(parts) < 3:
        return str(key or '')
    return '|'.join(parts[:3])


def load(store) -> dict:
    raw = store.get(STORE_KEY) if store is not None else None
    if not isinstance(raw, dict):
        return {'champions': {}, 'updated': None}
    champs = raw.get('champions')
    if not isinstance(champs, dict):
        champs = {}
    clean = {}
    for k, v in champs.items():
        if isinstance(v, dict) and v.get('weights_path'):
            clean[str(k)] = dict(v)
    return {'champions': clean, 'updated': raw.get('updated')}


def save(store, library: dict) -> dict:
    champs = library.get('champions') if isinstance(library, dict) else {}
    if not isinstance(champs, dict):
        champs = {}
    # Cap by weakest net when oversize.
    items = sorted(
        ((k, v) for k, v in champs.items() if isinstance(v, dict)),
        key=lambda kv: float(kv[1].get('net_pnl') or 0),
        reverse=True,
    )
    trimmed = {k: dict(v) for k, v in items[:MAX_CHAMPIONS]}
    out = {'champions': trimmed, 'updated': _now()}
    store.set(STORE_KEY, out)
    return out


def active(store) -> dict | None:
    raw = store.get(ACTIVE_KEY) if store is not None else None
    return dict(raw) if isinstance(raw, dict) else None


def set_active(store, *, key, model_version=None, reason='') -> dict:
    row = {
        'key': str(key),
        'model_version': model_version,
        'activated_at': _now(),
        'reason': str(reason or '')[:160],
    }
    store.set(ACTIVE_KEY, row)
    return row


def append_trade(store, *, context, pnl, deal_id=None, model_version=None,
                 direction=None) -> list:
    """Append one broker-£ close tagged with the OPEN-time context."""
    ctx = context if isinstance(context, dict) else context_from()
    key = context_key(ctx)
    try:
        cash = float(pnl)
    except (TypeError, ValueError):
        return list(store.get(TRADE_LOG_KEY) or [])
    if not math.isfinite(cash):
        return list(store.get(TRADE_LOG_KEY) or [])
    log = list(store.get(TRADE_LOG_KEY) or [])
    log.append({
        'at': _now(),
        'key': key,
        'soft': soft_key(key),
        'epic': ctx.get('epic'),
        'trend': ctx.get('trend'),
        'regime': ctx.get('regime'),
        'news': ctx.get('news'),
        'pnl': cash,
        'deal_id': deal_id,
        'model_version': model_version,
        'direction': direction,
    })
    store.set(TRADE_LOG_KEY, log[-MAX_TRADE_LOG:])
    return log[-MAX_TRADE_LOG:]


def _stats_for_key(log, key: str, *, soft=False) -> dict:
    if soft:
        sk = soft_key(key)
        rows = [r for r in log if soft_key(r.get('key') or '') == sk]
    else:
        rows = [r for r in log if r.get('key') == key]
    pnls = [float(r['pnl']) for r in rows if r.get('pnl') is not None]
    n = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x < 0)
    net = float(sum(pnls)) if pnls else 0.0
    return {
        'n': n, 'wins': wins, 'losses': losses, 'net': net,
        'win_rate': (wins / n) if n else 0.0,
        'rows': rows,
    }


def should_save_champion(stats, *, min_trades=DEFAULT_MIN_TRADES,
                         min_net=DEFAULT_MIN_NET,
                         min_win_rate=DEFAULT_MIN_WIN_RATE) -> bool:
    if int(stats.get('n') or 0) < int(min_trades):
        return False
    if float(stats.get('net') or 0) < float(min_net):
        return False
    if float(stats.get('win_rate') or 0) + 1e-12 < float(min_win_rate):
        return False
    return True


def consider_save(store, engine, *, context, min_trades=DEFAULT_MIN_TRADES,
                  min_net=DEFAULT_MIN_NET,
                  min_win_rate=DEFAULT_MIN_WIN_RATE) -> dict | None:
    """If this context's broker-£ window is proven, snapshot weights into library."""
    ctx = context if isinstance(context, dict) else context_from()
    key = context_key(ctx)
    log = list(store.get(TRADE_LOG_KEY) or [])
    stats = _stats_for_key(log, key, soft=False)
    if not should_save_champion(stats, min_trades=min_trades, min_net=min_net,
                                min_win_rate=min_win_rate):
        return None
    brain = getattr(engine, 'brain', None)
    if brain is None or not hasattr(brain, 'backup_weights'):
        return None
    lib = load(store)
    existing = lib['champions'].get(key)
    if existing and float(existing.get('net_pnl') or 0) >= float(stats['net']) - 1e-9:
        # Already have equal/better proof for this exact context.
        return None
    try:
        path = brain.backup_weights(tag=f'champ_{key.replace("|", "_")[:40]}')
    except Exception:
        return None
    if not path:
        return None
    from .bracket_learn import load_multipliers
    from . import learning_generation
    specs = []
    reg = getattr(engine, 'feature_registry', None)
    if reg is not None:
        specs = [dict(x) for x in (reg.approved or [])]
    entry = {
        'key': key,
        'context': {
            'epic': ctx.get('epic'), 'trend': ctx.get('trend'),
            'regime': ctx.get('regime'), 'news': ctx.get('news'),
        },
        'weights_path': str(path),
        'model_version': int(getattr(brain, 'model_version', 0) or 0),
        'feature_schema': getattr(brain, 'feature_schema', None),
        'ohlc_dim': getattr(brain, 'ohlc_dim', None),
        'fusion_dim': getattr(brain, 'fusion_dim', None),
        'approved_specs': specs,
        'brackets': dict(load_multipliers(store) or {}),
        'net_pnl': float(stats['net']),
        'n_trades': int(stats['n']),
        'wins': int(stats['wins']),
        'losses': int(stats['losses']),
        'win_rate': float(stats['win_rate']),
        'learning_generation': learning_generation.current(store),
        'saved_at': _now(),
        'note': 'broker_£_context_proven',
    }
    lib['champions'][key] = entry
    save(store, lib)
    store.event('champion', 'Saved profitable version for market context', {
        'key': key, 'net_pnl': entry['net_pnl'], 'n_trades': entry['n_trades'],
        'model_version': entry['model_version'], 'weights_path': entry['weights_path'],
    })
    return entry


def find_match(library, context, *, allow_soft=True) -> dict | None:
    """Pick champion whose context matches the current market task."""
    ctx = context if isinstance(context, dict) else context_from()
    key = context_key(ctx)
    champs = (library or {}).get('champions') or {}
    if key in champs:
        return dict(champs[key], match='exact')
    if not allow_soft:
        return None
    sk = soft_key(key)
    soft_hits = [
        v for k, v in champs.items()
        if soft_key(k) == sk and isinstance(v, dict)
    ]
    if not soft_hits:
        return None
    best = max(soft_hits, key=lambda v: float(v.get('net_pnl') or 0))
    return dict(best, match='soft')


def maybe_activate(store, engine, *, context, allow_soft=True) -> dict | None:
    """While flat: load matching champion weights if not already active for this key."""
    if getattr(engine, 'positions', None):
        return None  # never swap mid-trade
    ctx = context if isinstance(context, dict) else context_from()
    key = context_key(ctx)
    lib = load(store)
    hit = find_match(lib, ctx, allow_soft=allow_soft)
    if not hit:
        return None
    cur = active(store) or {}
    hit_key = str(hit.get('key') or '')
    if cur.get('key') == hit_key and str(cur.get('model_version')) == str(hit.get('model_version')):
        return {'skipped': True, 'reason': 'already_active', 'key': hit_key}
    restore = getattr(engine, 'restore_neural_weights', None)
    if restore is None:
        return None
    ok = bool(restore(hit))
    if not ok:
        store.event('champion', 'Champion activate failed (restore closed)', {
            'key': hit_key, 'match': hit.get('match'),
        })
        return {'activated': False, 'key': hit_key, 'reason': 'restore_failed'}
    # Soft-restore bracket multipliers that earned the £ — do not wipe other params.
    try:
        from .bracket_learn import save_multipliers
        br = hit.get('brackets') if isinstance(hit.get('brackets'), dict) else None
        if br and br.get('kappa') is not None and br.get('sl_scale') is not None:
            save_multipliers(
                store, float(br['kappa']), float(br['sl_scale']),
                reason=f'champion:{hit_key}')
    except Exception:
        pass
    set_active(store, key=hit_key, model_version=hit.get('model_version'),
               reason=f"match={hit.get('match')} context={key}")
    store.event('champion', 'Activated profitable version for matching context', {
        'key': hit_key, 'request_key': key, 'match': hit.get('match'),
        'model_version': hit.get('model_version'),
        'net_pnl': hit.get('net_pnl'), 'n_trades': hit.get('n_trades'),
    })
    store.set('neural_adapt_reason',
              f"Champion {hit_key} loaded (match={hit.get('match')}, "
              f"net£={float(hit.get('net_pnl') or 0):.2f})")
    return {
        'activated': True, 'key': hit_key, 'match': hit.get('match'),
        'model_version': hit.get('model_version'),
        'net_pnl': hit.get('net_pnl'),
    }


def snapshot(store) -> dict:
    lib = load(store)
    act = active(store)
    log = list(store.get(TRADE_LOG_KEY) or [])
    return {
        'champions': len(lib['champions']),
        'keys': sorted(lib['champions'].keys()),
        'active': act,
        'context_trades': len(log),
        'updated': lib.get('updated'),
        'entries': [
            {
                'key': v.get('key'),
                'net_pnl': v.get('net_pnl'),
                'n_trades': v.get('n_trades'),
                'win_rate': v.get('win_rate'),
                'model_version': v.get('model_version'),
                'saved_at': v.get('saved_at'),
                'context': v.get('context'),
            }
            for v in sorted(
                lib['champions'].values(),
                key=lambda x: float(x.get('net_pnl') or 0),
                reverse=True,
            )
        ],
    }
