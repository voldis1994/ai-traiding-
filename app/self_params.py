"""Self-tunable knobs the robot owns — not frozen magic in source.

Seed DEFAULTS are only the *first* values. Everything here is persisted in Store
and nudged from outcomes (approve rate, OOS net, shadow compare, broker £).

Nudges from single events are *queued* (commit=False), then activated as a
*trial* so the robot actually trades under the new knobs. Once a trial starts,
those knobs are *frozen* for the whole evaluation window — further nudges
queue for a later draft but must not mutate the version under test. Commit
only when broker £ earned *during the trial* beats the equal pre-trial
window — never from profit earned while the draft was still idle, and never
for a param set that kept changing mid-trial.

Honest immutable (NOT in this bag — physics / API / layout):
  - Capital has no volume/DOM (public liquidity is EXTERNAL fusion only)
  - Feature vector layout [BASE|DYNAMIC|EXTERNAL]
  - BUY/SELL/HOLD three-class head
  - Broker tick / min-stop / margin rules
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

STORE_KEY = 'self_params_v1'
PENDING_KEY = 'self_params_pending_v1'

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
    'val_min_spread_coverage': 0.5,
    'val_oos_frac': 0.7,
    # Model / shadow — lower floors so promote/reject can actually resolve
    'model_min_oos': 12,
    'model_max_dd_ratio': 1.15,
    'model_stability_slack': 0.08,
    'shadow_min_broker_trades': 8,
    'shadow_rollback_window': 10,
    'shadow_rollback_delta': 0.5,
    # Learning
    'learn_lr': 0.15,
    'learn_win_repeats': 2,
    'learn_loss_repeats': 2,
    'profit_history_rows': 400,
    # Exit intent (logged only — LIVE still SL/TP/trail)
    'exit_need_streak': 2,
    # Neural growth (engine)
    'grow_error_threshold': 0.55,
    'prune_utility_threshold': -0.01,
    'prune_min_age': 80,
    # Shadow/LIVE execution parity (critical #4) — never ideal zero friction.
    'exec_slip_bps': 2.0,
    'exec_fill_delay_bars': 1,
    # Long-horizon gate before committing param nudges
    'self_params_horizon_trades': 20,
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
    'exec_slip_bps': (0.5, 50.0),
    'exec_fill_delay_bars': (1, 5),
    'self_params_horizon_trades': (8, 80),
}

IMMUTABLE_NOTE = (
    'Capital: no volume/DOM (per-instrument Yahoo/Coinbase/OKX liquidity is EXTERNAL). '
    'Layout: BASE|DYNAMIC|EXTERNAL. Actions: BUY/SELL/HOLD. '
    'Broker tick/min-stop/margin are exchange rules.'
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
               'profit_history_rows','exit_need_streak','prune_min_age',
               'exec_fill_delay_bars','self_params_horizon_trades'):
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
    pending=store.get(PENDING_KEY) if store is not None else None
    return {'params':load(store),'bounds':{k:list(v) for k,v in BOUNDS.items()},
            'immutable':IMMUTABLE_NOTE,
            'pending_events':len((pending or {}).get('events') or []) if isinstance(pending,dict) else 0}


def _broker_trade_count(store):
    """How many broker-£ closed trades are in the profit ledger right now."""
    if store is None:return 0
    hist=store.get('profit_learning_history_v1') or []
    if not isinstance(hist,list):return 0
    return sum(1 for r in hist
               if isinstance(r,dict) and r.get('actual_pnl') is not None
               and r.get('mode')!='skipped_no_broker_pnl')


def _pending(store):
    raw=store.get(PENDING_KEY) if store is not None else None
    if not isinstance(raw,dict):
        return {'events':[],'draft':None,'baseline_net':None,'marked_at_trades':0,
                'trial_active':False,'trial_started_at_trades':0,'baseline_params':None,
                'trial_params':None,'trial_id':None}
    return {
        'events':list(raw.get('events') or []),
        'draft':raw.get('draft'),
        'baseline_net':raw.get('baseline_net'),
        'marked_at_trades':int(raw.get('marked_at_trades') or 0),
        'trial_active':bool(raw.get('trial_active')),
        'trial_started_at_trades':int(raw.get('trial_started_at_trades') or 0),
        'baseline_params':raw.get('baseline_params'),
        # Frozen param set under evaluation — never mutate while trial_active.
        'trial_params':raw.get('trial_params'),
        'trial_id':raw.get('trial_id'),
    }


def _save_pending(store, pending):
    if store is None:return
    store.set(PENDING_KEY,{
        'events':list(pending.get('events') or [])[-80:],
        'draft':pending.get('draft'),
        'baseline_net':pending.get('baseline_net'),
        'marked_at_trades':int(pending.get('marked_at_trades') or 0),
        'trial_active':bool(pending.get('trial_active')),
        'trial_started_at_trades':int(pending.get('trial_started_at_trades') or 0),
        'baseline_params':pending.get('baseline_params'),
        'trial_params':pending.get('trial_params'),
        'trial_id':pending.get('trial_id'),
    })


def _clear_trial_fields():
    return {'events':[],'draft':None,'baseline_net':None,'marked_at_trades':0,
            'trial_active':False,'trial_started_at_trades':0,'baseline_params':None,
            'trial_params':None,'trial_id':None}


def _apply_nudge(p, *, feature_approved=None, feature_rejected=None,
                 shadow_promote=None, broker_pnl=None):
    """Mutate a param dict from one evidence event. Returns note strings."""
    notes=[]
    if feature_approved:
        p['bars_between_search']=max(BOUNDS['bars_between_search'][0],
            int(p['bars_between_search']*0.9))
        p['max_approved_features']=min(BOUNDS['max_approved_features'][1],
            int(p['max_approved_features'])+1)
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
            # Scratch / penny closes (|£|≤0.05) must not ratchet HOLD repeats —
            # that was the permanent-HOLD trap after a streak of £0 exits.
            if pnl<0 and abs(pnl)>0.05:
                p['learn_lr']=min(BOUNDS['learn_lr'][1],float(p['learn_lr'])*1.05)
                # Never let loss repeats exceed win repeats (symmetric teacher).
                win_r=int(p.get('learn_win_repeats',DEFAULTS['learn_win_repeats']))
                cur=int(p['learn_loss_repeats'])
                p['learn_loss_repeats']=min(win_r,cur)
                notes.append('meaningful loss → calm LR only; loss repeats capped at win')
            elif pnl<0:
                notes.append('scratch loss — no HOLD-repeat ratchet')
            elif pnl>0:
                p['learn_lr']=max(BOUNDS['learn_lr'][0],float(p['learn_lr'])*0.98)
                win_r=int(p.get('learn_win_repeats',DEFAULTS['learn_win_repeats']))
                # Soft nudge win path so it stays ≥ loss path.
                p['learn_win_repeats']=min(BOUNDS['learn_win_repeats'][1],max(win_r,int(p['learn_loss_repeats'])))
                notes.append('win → calm LR; keep win repeats ≥ loss')
    return notes


def evolve_from_feedback(store, *, feature_approved=None, feature_rejected=None,
                         shadow_promote=None, broker_pnl=None, reason='',
                         commit=True):
    """Nudge knobs from outcomes.

    commit=False → queue draft, then *activate as trial* so subsequent broker £
    is earned under the new knobs (proof requires trial-period £, not past £).
    While a trial is active the frozen trial_params stay on the live bag —
    further nudges only update the aspirational draft, never the version under
    evaluation (audit #27).
    commit=True  → immediate save (tests / explicit force).
    """
    pending=_pending(store)
    # While a trial is frozen, draft from the frozen set (not a mutating chain).
    if pending.get('trial_active') and isinstance(pending.get('trial_params'),dict):
        base=dict(pending['trial_params'])
    elif isinstance(pending.get('draft'),dict):
        base=dict(pending['draft'])
    else:
        base=load(store)
    # Ensure all keys present when starting from a partial draft.
    p={k:_clip(k,base.get(k,DEFAULTS[k])) for k in DEFAULTS}
    notes=_apply_nudge(p,feature_approved=feature_approved,feature_rejected=feature_rejected,
                       shadow_promote=shadow_promote,broker_pnl=broker_pnl)
    msg=reason or ('; '.join(notes) if notes else 'no change')
    clean={k:_clip(k,p[k]) for k in DEFAULTS}
    if not commit:
        if notes:
            pending['events'].append({
                'at':datetime.now(timezone.utc).isoformat(),
                'reason':msg,
                'feature_approved':feature_approved,
                'feature_rejected':(feature_rejected.get('reason')
                    if isinstance(feature_rejected,dict) else feature_rejected),
                'shadow_promote':shadow_promote,
                'broker_pnl':broker_pnl,
                'trial_id':pending.get('trial_id'),
            })
            # First draft → activate trial under new params; mark trade index.
            if not pending.get('trial_active'):
                pending['baseline_params']=load(store)
                pending['trial_started_at_trades']=_broker_trade_count(store)
                pending['trial_active']=True
                pending['trial_params']=dict(clean)
                pending['trial_id']=(
                    f"t{_broker_trade_count(store)}_"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
                pending['draft']=dict(clean)
                save(store,clean,reason=f"trial activation {pending['trial_id']}")
                if store is not None:
                    store.event('self_params','Self-params trial activated',
                        {'reason':msg,'trial_id':pending['trial_id'],
                         'trial_started_at_trades':pending['trial_started_at_trades'],
                         'frozen':True})
            else:
                # Trial frozen — queue aspirational draft only; do NOT mutate live.
                pending['draft']=clean
                if store is not None:
                    store.event('self_params','Self-params mid-trial draft queued (live frozen)',
                        {'reason':msg,'trial_id':pending.get('trial_id')})
            _save_pending(store,pending)
            # Callers that inspect return value during a trial see the frozen set.
            if pending.get('trial_active') and isinstance(pending.get('trial_params'),dict):
                return {k:_clip(k,pending['trial_params'].get(k,DEFAULTS[k])) for k in DEFAULTS}
        return clean
    save(store,clean,reason=msg)
    if store is not None and notes:
        store.event('self_params','Self-params adapted from feedback',
            {'reason':msg,'params':{k:clean[k] for k in (
                'bars_between_search','val_horizon_bars','learn_lr',
                'max_approved_features','exit_need_streak') if k in clean},
             'commit':True})
    return clean


def maybe_commit_long_horizon(store, broker_pnls):
    """Commit *frozen* trial params only if £ during the trial beats pre-trial.

    Profit from before trial_started_at_trades does not count — that was earned
    under the old knobs and cannot prove the draft is better. Mid-trial draft
    mutations never become the committed set (audit #27).
    """
    if store is None:return {'committed':False,'reason':'no store'}
    pending=_pending(store)
    if not pending.get('draft') and not pending.get('events') and not pending.get('trial_params'):
        return {'committed':False,'reason':'no pending draft'}
    if not pending.get('trial_active'):
        return {'committed':False,'reason':'draft not yet on trial'}
    frozen=pending.get('trial_params')
    if not isinstance(frozen,dict):
        # Legacy pending without trial_params — treat live bag as the frozen set.
        frozen=load(store)
    cfg={k:_clip(k,frozen.get(k,DEFAULTS[k])) for k in DEFAULTS}
    need=int(cfg['self_params_horizon_trades'])
    pnls=[float(x) for x in (broker_pnls or [])
          if x is not None and math.isfinite(float(x))]
    start=int(pending.get('trial_started_at_trades') or 0)
    trial_id=pending.get('trial_id')
    # Trades after trial activation only.
    trial=pnls[start:]
    prior=pnls[max(0,start-need):start]
    if len(trial)<need:
        return {'committed':False,'reason':f'need ≥{need} broker £ under trial',
                'n_trial':len(trial),'pending':len(pending['events']),
                'trial_started_at_trades':start,'trial_id':trial_id}
    trial_window=trial[:need]
    trial_net=float(sum(trial_window))
    trial_mean=trial_net/max(1,len(trial_window))
    n_events=len(pending['events'])
    # Cold start / short prior used to stall forever (need≥N prior never arrived).
    # Compare means when prior is shorter; with no prior, require trial_net>0.
    if not prior:
        prior_net=0.0
        beat=trial_net>0
        mode='cold_start_trial_net'
    elif len(prior)<need:
        prior_net=float(sum(prior))
        prior_mean=prior_net/len(prior)
        beat=trial_mean>0 and trial_mean>prior_mean
        mode='short_prior_mean'
    else:
        prior_net=float(sum(prior[-need:]))
        beat=trial_net>0 and trial_net>prior_net
        mode='equal_window_net'
    if not beat:
        baseline=pending.get('baseline_params') or DEFAULTS
        save(store,{k:_clip(k,baseline.get(k,DEFAULTS[k])) for k in DEFAULTS},
             reason='trial rejected — restored baseline')
        cleared=_clear_trial_fields()
        cleared['baseline_net']=prior_net
        cleared['marked_at_trades']=len(pnls)
        _save_pending(store,cleared)
        store.event('self_params','Self-params trial rejected; baseline restored',
            {'events':n_events,'trial_net':trial_net,'prior_net':prior_net,
             'horizon':need,'mode':mode,'trial_id':trial_id})
        return {'committed':False,'rejected':True,
                'trial_net':trial_net,'prior_net':prior_net,'mode':mode,
                'trial_id':trial_id,
                'reason':'trial £ not better than available baseline'}
    # Commit the frozen trial version — never a mid-trial draft that drifted.
    save(store,cfg,reason=f'long-horizon trial £ proof {trial_id or ""}'.strip())
    cleared=_clear_trial_fields()
    cleared['baseline_net']=trial_net
    cleared['marked_at_trades']=len(pnls)
    _save_pending(store,cleared)
    store.event('self_params','Self-params committed after trial £ proof',
        {'events_applied':n_events,'trial_net':trial_net,'prior_net':prior_net,
         'horizon':need,'trial_started_at_trades':start,'mode':mode,
         'trial_id':trial_id,'frozen':True})
    from .learning_generation import bump as bump_generation
    bump_generation(store,'self_params_trial_committed')
    return {'committed':True,'trial_net':trial_net,'prior_net':prior_net,
            'events_applied':n_events,'mode':mode,'trial_id':trial_id}
