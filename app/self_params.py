"""Self-tunable knobs the robot owns — not frozen magic in source.

Seed DEFAULTS are only the *first* values. Everything here is persisted in Store
and nudged from outcomes (approve rate, OOS net, shadow compare, broker £).

Honest immutable (NOT in this bag — physics / API / layout):
  - Capital has no volume/DOM
  - Feature vector layout [BASE|DYNAMIC|EXTERNAL]
  - BUY/SELL/HOLD three-class head
  - Broker tick / min-stop / margin rules
"""
from __future__ import annotations

import math

STORE_KEY = 'self_params_v1'

# Seeds only — overwritten by Store once the robot has lived.
DEFAULTS = {
    # Discovery
    'bars_between_search': 30,
    'max_proposals_kept': 80,
    'search_limit': 24,
    'max_approved_features': 32,
    'gen_horizons': [3, 5, 8, 13, 21, 34],
    # Validation
    'val_min_bars': 120,
    'val_min_folds': 3,
    'val_max_corr': 0.92,
    'val_min_fold_agree': 0.6,
    'val_min_net_pnl': 1e-6,
    'val_horizon_bars': 8,
    'val_min_spread_coverage': 0.7,
    'val_oos_frac': 0.7,
    # Model / shadow
    'model_min_oos': 24,
    'model_max_dd_ratio': 1.15,
    'model_stability_slack': 0.05,
    'shadow_min_broker_trades': 12,
    'shadow_rollback_window': 10,
    'shadow_rollback_delta': 0.5,
    # Learning
    'learn_lr': 0.15,
    'learn_win_repeats': 2,
    'learn_loss_repeats': 5,
    'profit_history_rows': 400,
    # Exit intent (logged only — LIVE still SL/TP/trail)
    'exit_need_streak': 2,
    # Neural growth (engine)
    'grow_error_threshold': 0.55,
    'prune_utility_threshold': -0.01,
    'prune_min_age': 80,
}

# Soft rails so a bad evolve step cannot explode dim / divide-by-zero.
# Robot may move freely inside these; widening rails is a code change (rare).
BOUNDS = {
    'bars_between_search': (5, 120),
    'max_proposals_kept': (20, 200),
    'search_limit': (4, 64),
    'max_approved_features': (4, 64),
    'val_min_bars': (80, 400),
    'val_min_folds': (2, 8),
    'val_max_corr': (0.7, 0.99),
    'val_min_fold_agree': (0.4, 0.9),
    'val_min_net_pnl': (0.0, 1.0),
    'val_horizon_bars': (2, 30),
    'val_min_spread_coverage': (0.4, 0.95),
    'val_oos_frac': (0.5, 0.85),
    'model_min_oos': (8, 80),
    'model_max_dd_ratio': (1.0, 2.0),
    'model_stability_slack': (0.0, 0.2),
    'shadow_min_broker_trades': (6, 60),
    'shadow_rollback_window': (5, 40),
    'shadow_rollback_delta': (0.05, 5.0),
    'learn_lr': (0.01, 0.5),
    'learn_win_repeats': (1, 8),
    'learn_loss_repeats': (1, 12),
    'profit_history_rows': (50, 2000),
    'exit_need_streak': (1, 8),
    'grow_error_threshold': (0.2, 2.0),
    'prune_utility_threshold': (-0.2, 0.0),
    'prune_min_age': (20, 500),
}

IMMUTABLE_NOTE = (
    'Capital: no volume/DOM. Layout: BASE|DYNAMIC|EXTERNAL. '
    'Actions: BUY/SELL/HOLD. Broker tick/min-stop/margin are exchange rules.'
)


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def _clip(key, value):
    if key=='gen_horizons':
        if not isinstance(value,(list,tuple)) or not value:
            return list(DEFAULTS['gen_horizons'])
        out=[]
        for h in value:
            try:
                ih=int(h)
            except (TypeError,ValueError):
                continue
            if 2<=ih<=120 and ih not in out:out.append(ih)
        return sorted(out) or list(DEFAULTS['gen_horizons'])
    bounds=BOUNDS.get(key)
    n=_finite(value)
    if n is None:return DEFAULTS[key]
    if key in ('bars_between_search','max_proposals_kept','search_limit',
               'max_approved_features','val_min_bars','val_min_folds',
               'val_horizon_bars','model_min_oos','shadow_min_broker_trades',
               'shadow_rollback_window','learn_win_repeats','learn_loss_repeats',
               'profit_history_rows','exit_need_streak','prune_min_age'):
        n=int(round(n))
    if bounds:
        lo,hi=bounds
        n=max(lo,min(hi,n))
    return n


def load(store) -> dict:
    raw=store.get(STORE_KEY) if store is not None else None
    out=dict(DEFAULTS)
    if isinstance(raw,dict):
        for k,v in raw.items():
            if k in DEFAULTS:
                out[k]=_clip(k,v)
    return out


def save(store, params: dict, *, reason=''):
    if store is None:return
    clean={k:_clip(k,params.get(k,DEFAULTS[k])) for k in DEFAULTS}
    store.set(STORE_KEY,clean)
    if reason:
        store.set('self_params_reason',reason)


def get(store, key, default=None):
    params=load(store)
    if key in params:return params[key]
    return default if default is not None else DEFAULTS.get(key)


def snapshot(store) -> dict:
    return {'params':load(store),'bounds':{k:list(v) for k,v in BOUNDS.items()},
            'immutable':IMMUTABLE_NOTE}


def evolve_from_feedback(store, *, feature_approved=None, feature_rejected=None,
                         shadow_promote=None, broker_pnl=None, reason=''):
    """Nudge knobs from outcomes — the robot changes itself when needed."""
    p=load(store)
    notes=[]
    if feature_approved:
        # Approvals working → allow slightly faster search / more slots.
        p['bars_between_search']=max(BOUNDS['bars_between_search'][0],
            int(p['bars_between_search']*0.9))
        p['max_approved_features']=min(BOUNDS['max_approved_features'][1],
            int(p['max_approved_features'])+1)
        # Prefer horizons that appear in approved ids.
        fid=str(feature_approved)
        for h in list(p['gen_horizons']):
            if f'_{h}' in fid or fid.endswith(f'_{h}'):
                notes.append(f'keep horizon {h}')
        notes.append('faster discovery after approve')
    if feature_rejected:
        why=str(feature_rejected.get('reason') or '') if isinstance(feature_rejected,dict) else str(feature_rejected)
        if 'spread' in why or 'coverage' in why:
            p['val_min_spread_coverage']=max(BOUNDS['val_min_spread_coverage'][0],
                float(p['val_min_spread_coverage'])-0.02)
            notes.append('eased spread coverage after reject')
        elif 'collinear' in why or 'duplicate' in why:
            p['val_max_corr']=max(BOUNDS['val_max_corr'][0],
                float(p['val_max_corr'])-0.01)
            notes.append('stricter uniqueness via lower corr cap move')
        elif 'net PnL' in why or 'unstable' in why:
            p['val_horizon_bars']=min(BOUNDS['val_horizon_bars'][1],
                int(p['val_horizon_bars'])+1)
            p['bars_between_search']=min(BOUNDS['bars_between_search'][1],
                int(p['bars_between_search'])+2)
            notes.append('longer horizon / slower search after weak PnL reject')
        else:
            p['search_limit']=max(BOUNDS['search_limit'][0],int(p['search_limit'])-1)
            notes.append('narrow search after reject')
    if shadow_promote is True:
        p['shadow_min_broker_trades']=max(BOUNDS['shadow_min_broker_trades'][0],
            int(p['shadow_min_broker_trades'])-1)
        notes.append('shadow earned trust — slightly lower sample floor')
    elif shadow_promote is False:
        p['model_max_dd_ratio']=max(BOUNDS['model_max_dd_ratio'][0],
            float(p['model_max_dd_ratio'])-0.02)
        notes.append('shadow lost — tighter DD tolerance')
    if broker_pnl is not None:
        pnl=_finite(broker_pnl)
        if pnl is not None:
            if pnl<0:
                p['learn_lr']=min(BOUNDS['learn_lr'][1],float(p['learn_lr'])*1.05)
                p['learn_loss_repeats']=min(BOUNDS['learn_loss_repeats'][1],
                    int(p['learn_loss_repeats'])+1)
                notes.append('loss → slightly stronger learn step')
            elif pnl>0:
                p['learn_lr']=max(BOUNDS['learn_lr'][0],float(p['learn_lr'])*0.98)
                notes.append('win → calm LR')
    msg=reason or ('; '.join(notes) if notes else 'no change')
    save(store,p,reason=msg)
    if store is not None and notes:
        store.event('self_params','Self-params adapted from feedback',
            {'reason':msg,'params':{k:p[k] for k in (
                'bars_between_search','val_horizon_bars','learn_lr',
                'max_approved_features','exit_need_streak') if k in p}})
    return p
