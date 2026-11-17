"""Persist full M1 forecast inputs + later multi-horizon outcomes.

The live ``signals`` table stores scores but not the 32-D feature vector.
This ledger is the external accumulation path for forecast quality work —
recording only; it never gates OPEN/CLOSE.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from .forecast_targets import HORIZONS, path_from_candles, targets_at_horizons

STORE_FLAG = 'forecast_memory_enabled'
TABLE = 'forecast_memory'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _finite_list(xs):
    out=[]
    for v in xs or []:
        try:
            f=float(v)
        except (TypeError,ValueError):
            return None
        if not math.isfinite(f):
            return None
        out.append(f)
    return out


class ForecastMemory:
    def __init__(self, store):
        self.store=store
        self._ensure()

    def _ensure(self):
        self.store.db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            time TEXT,
            epic TEXT,
            signal_id INTEGER,
            features TEXT,
            scores TEXT,
            mid REAL,
            spread REAL,
            tick_size REAL,
            resolved INTEGER DEFAULT 0,
            outcomes TEXT
        )''')
        self.store.db.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_epic_time ON {TABLE}(epic,time)')
        self.store.db.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_resolved ON {TABLE}(resolved)')
        self.store.db.commit()

    def enabled(self):
        try:
            from .self_params import get as sp_get
            return int(sp_get(self.store,STORE_FLAG,1))==1
        except Exception:
            return True

    def record(self, *, epic, features, scores, mid, spread=0., tick_size=0.01,
               bar_time=None, signal_id=None):
        """Insert one unresolved forecast row. No-op when disabled or invalid."""
        if not self.enabled():
            return None
        feats=_finite_list(features)
        if not feats or not isinstance(scores,dict):
            return None
        try:
            mid_f=float(mid);sp=float(spread or 0.);tick=float(tick_size or 0.01)
        except (TypeError,ValueError):
            return None
        if not (math.isfinite(mid_f) and mid_f>0):
            return None
        score_map={}
        for k in ('BUY','SELL','HOLD'):
            if k not in scores:continue
            try:
                v=float(scores[k])
            except (TypeError,ValueError):
                continue
            if math.isfinite(v):
                score_map[k]=v
        if len(score_map)<2:
            return None
        cur=self.store.db.execute(
            f'''INSERT INTO {TABLE}
                (time,epic,signal_id,features,scores,mid,spread,tick_size,resolved,outcomes)
                VALUES(?,?,?,?,?,?,?,?,0,NULL)''',
            (bar_time or _now(),str(epic or ''),signal_id,
             json.dumps(feats),json.dumps(score_map),mid_f,sp,tick))
        self.store.db.commit()
        return cur.lastrowid

    def unresolved(self, *, epic=None, limit=500):
        q=f'SELECT id,time,epic,mid,spread FROM {TABLE} WHERE resolved=0'
        args=[]
        if epic:
            q+=' AND epic=?';args.append(str(epic))
        q+=' ORDER BY id ASC LIMIT ?';args.append(int(limit))
        cols=('id','time','epic','mid','spread')
        return [dict(zip(cols,r)) for r in self.store.db.execute(q,args)]

    def resolve_with_candles(self, candles, *, epic=None, horizons=HORIZONS):
        """Fill outcomes for pending rows once enough future M1 bars exist."""
        if not candles:
            return 0
        n=0
        for row in self.unresolved(epic=epic,limit=2000):
            mids,spreads=path_from_candles(
                candles,after_time=row['time'],max_h=max(horizons))
            if len(mids)<max(horizons):
                continue
            outcomes=targets_at_horizons(
                row['mid'],mids,spread=row.get('spread') or 0.,
                future_spreads=spreads,horizons=horizons)
            if len(outcomes)<len(horizons):
                continue
            # JSON keys as strings for portability
            payload={str(h):outcomes[h] for h in outcomes}
            self.store.db.execute(
                f'UPDATE {TABLE} SET resolved=1, outcomes=? WHERE id=?',
                (json.dumps(payload),row['id']))
            n+=1
        if n:
            self.store.db.commit()
        return n

    def rows(self, *, epic=None, currency=None, resolved_only=True, limit=5000):
        """currency unused — kept for API symmetry with cash rows; epic filters."""
        _=currency
        q=f'SELECT id,time,epic,signal_id,features,scores,mid,spread,tick_size,resolved,outcomes FROM {TABLE}'
        clauses=[];args=[]
        if resolved_only:
            clauses.append('resolved=1')
        if epic:
            clauses.append('epic=?');args.append(str(epic))
        if clauses:
            q+=' WHERE '+' AND '.join(clauses)
        q+=' ORDER BY id ASC LIMIT ?';args.append(int(limit))
        out=[]
        for r in self.store.db.execute(q,args):
            (rid,t,ep,sid,feat,sc,mid,sp,tick,res,outc)=r
            try:
                features=json.loads(feat) if feat else None
                scores=json.loads(sc) if sc else None
                outcomes=json.loads(outc) if outc else None
            except (TypeError,ValueError,json.JSONDecodeError):
                continue
            if outcomes:
                outcomes={int(k):v for k,v in outcomes.items()}
            out.append({
                'id':rid,'time':t,'epic':ep,'signal_id':sid,
                'features':features,'scores':scores,'mid':mid,
                'spread':sp,'tick_size':tick,'resolved':bool(res),
                'outcomes':outcomes,
            })
        return out

    def summary(self, *, epic=None):
        rows=self.rows(epic=epic,resolved_only=False,limit=100000)
        resolved=sum(1 for r in rows if r.get('resolved'))
        return {
            'total':len(rows),
            'resolved':resolved,
            'unresolved':len(rows)-resolved,
            'epic':epic,
            'horizons':list(HORIZONS),
        }


def maybe_record(store, **kwargs):
    """Best-effort recorder for AdaptiveBrain.tick / poll hooks."""
    try:
        return ForecastMemory(store).record(**kwargs)
    except Exception:
        return None


def maybe_resolve(store, candles, *, epic=None):
    try:
        return ForecastMemory(store).resolve_with_candles(candles,epic=epic)
    except Exception:
        return 0
