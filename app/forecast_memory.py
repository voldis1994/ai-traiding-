"""Persist full M1 forecast inputs + later multi-horizon outcomes.

The live ``signals`` table stores scores but not the 32-D feature vector.
This ledger is the external accumulation path for forecast quality work —
recording only; it never gates OPEN/CLOSE or updates LIVE weights.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from .forecast_targets import (
    HORIZONS, bar_stamp, candle_bar_time, completed_candles, path_from_candles,
    targets_at_horizons,
)

STORE_FLAG = 'forecast_memory_enabled'
TABLE = 'forecast_memory'
# resolved: 0=pending, 1=ok, 2=abandoned (gap / past window — leave the queue)
RESOLVED_PENDING = 0
RESOLVED_OK = 1
RESOLVED_ABANDONED = 2


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
            in_position INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0,
            outcomes TEXT
        )''')
        # Migrate older DBs that predate in_position.
        cols={r[1] for r in self.store.db.execute(f'PRAGMA table_info({TABLE})')}
        if 'in_position' not in cols:
            self.store.db.execute(
                f'ALTER TABLE {TABLE} ADD COLUMN in_position INTEGER DEFAULT 0')
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
               bar_time=None, signal_id=None, in_position=False):
        """Insert one unresolved forecast row (flat or in-trade). No-op if disabled."""
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
        stamp=bar_stamp(bar_time) if bar_time is not None else None
        time_s=stamp.isoformat() if stamp is not None else (bar_time or _now())
        # Dedup same epic+minute (flat and manage ticks can share a bar).
        existing=self.store.db.execute(
            f'SELECT id FROM {TABLE} WHERE epic=? AND time=? LIMIT 1',
            (str(epic or ''),str(time_s))).fetchone()
        if existing:
            return int(existing[0])
        cur=self.store.db.execute(
            f'''INSERT INTO {TABLE}
                (time,epic,signal_id,features,scores,mid,spread,tick_size,
                 in_position,resolved,outcomes)
                VALUES(?,?,?,?,?,?,?,?,?,?,NULL)''',
            (str(time_s),str(epic or ''),signal_id,
             json.dumps(feats),json.dumps(score_map),mid_f,sp,tick,
             1 if in_position else 0,RESOLVED_PENDING))
        self.store.db.commit()
        return cur.lastrowid

    def unresolved(self, *, epic=None, limit=500, newest_first=False):
        q=f'SELECT id,time,epic,mid,spread FROM {TABLE} WHERE resolved=?'
        args=[RESOLVED_PENDING]
        if epic:
            q+=' AND epic=?';args.append(str(epic))
        q+=(' ORDER BY id DESC LIMIT ?' if newest_first
            else ' ORDER BY id ASC LIMIT ?')
        args.append(int(limit))
        cols=('id','time','epic','mid','spread')
        return [dict(zip(cols,r)) for r in self.store.db.execute(q,args)]

    def _pending_scan(self, *, epic=None, scan=400):
        """Newest + oldest pending — old gaps must not monopolize the queue."""
        recent=self.unresolved(epic=epic,limit=scan,newest_first=True)
        older=self.unresolved(epic=epic,limit=scan,newest_first=False)
        seen=set();out=[]
        for row in recent+older:
            rid=row['id']
            if rid in seen:
                continue
            seen.add(rid);out.append(row)
        return out

    def resolve_with_candles(self, candles, *, epic=None, horizons=HORIZONS,
                             now_utc=None, abandon_grace_minutes=2, scan=400):
        """Link pending M1 forecasts to actual 1/3/5/15 outcomes.

        - Uses only **completed** M1 candles (forming minute excluded).
        - Scans newest and oldest pending so a stuck early gap cannot block
          evaluation of fresh rows.
        - Abandons rows whose outcome window is already past the latest candle
          but still unresolvable (contiguous path missing).
        """
        need=tuple(int(h) for h in horizons)
        max_h=max(need) if need else 15
        bars=completed_candles(candles,now_utc=now_utc)
        if not bars:
            return {'resolved':0,'abandoned':0,'scanned':0}
        latest=None
        for c in bars:
            st=bar_stamp(candle_bar_time(c))
            if st is None:
                continue
            if latest is None or st>latest:
                latest=st
        grace=int(abandon_grace_minutes if abandon_grace_minutes is not None else 2)
        n_ok=0;n_ab=0
        pending=self._pending_scan(epic=epic,scan=scan)
        for row in pending:
            mids,spreads,_match=path_from_candles(
                bars,after_time=row['time'],max_h=max_h,require_contiguous=True)
            if len(mids)>=max_h:
                outcomes=targets_at_horizons(
                    row['mid'],mids,spread=row.get('spread') or 0.,
                    future_spreads=spreads,horizons=need)
                if any(h not in outcomes for h in need):
                    continue
                if abs(float(outcomes[need[0]]['exit_mid'])-float(mids[need[0]-1]))>1e-9:
                    continue
                payload={str(h):outcomes[h] for h in need}
                self.store.db.execute(
                    f'UPDATE {TABLE} SET resolved=?, outcomes=? WHERE id=?',
                    (RESOLVED_OK,json.dumps(payload),row['id']))
                n_ok+=1
                continue
            # Stuck? History already past forecast + horizon + grace → abandon.
            row_st=bar_stamp(row['time'])
            if row_st is None or latest is None:
                continue
            deadline=row_st+timedelta(minutes=max_h+grace)
            if latest>=deadline:
                self.store.db.execute(
                    f'UPDATE {TABLE} SET resolved=?, outcomes=? WHERE id=?',
                    (RESOLVED_ABANDONED,
                     json.dumps({'reason':'gap_or_incomplete_path','max_h':max_h}),
                     row['id']))
                n_ab+=1
        if n_ok or n_ab:
            self.store.db.commit()
        return {'resolved':n_ok,'abandoned':n_ab,'scanned':len(pending)}

    def rows(self, *, epic=None, currency=None, resolved_only=True, limit=5000):
        """currency unused — kept for API symmetry with cash rows; epic filters."""
        _=currency
        q=(f'SELECT id,time,epic,signal_id,features,scores,mid,spread,tick_size,'
           f'in_position,resolved,outcomes FROM {TABLE}')
        clauses=[];args=[]
        if resolved_only:
            clauses.append('resolved=?');args.append(RESOLVED_OK)
        if epic:
            clauses.append('epic=?');args.append(str(epic))
        if clauses:
            q+=' WHERE '+' AND '.join(clauses)
        q+=' ORDER BY time ASC, id ASC LIMIT ?';args.append(int(limit))
        out=[]
        for r in self.store.db.execute(q,args):
            (rid,t,ep,sid,feat,sc,mid,sp,tick,inp,res,outc)=r
            try:
                features=json.loads(feat) if feat else None
                scores=json.loads(sc) if sc else None
                outcomes=json.loads(outc) if outc else None
            except (TypeError,ValueError,json.JSONDecodeError):
                continue
            if outcomes and int(res)==RESOLVED_OK:
                try:
                    outcomes={int(k):v for k,v in outcomes.items()
                              if str(k).lstrip('-').isdigit()}
                except Exception:
                    outcomes=None
            out.append({
                'id':rid,'time':t,'epic':ep,'signal_id':sid,
                'features':features,'scores':scores,'mid':mid,
                'spread':sp,'tick_size':tick,
                'in_position':bool(inp),
                'resolved':int(res)==RESOLVED_OK,
                'abandoned':int(res)==RESOLVED_ABANDONED,
                'status':int(res),
                'outcomes':outcomes,
            })
        return out

    def summary(self, *, epic=None):
        rows=self.rows(epic=epic,resolved_only=False,limit=100000)
        resolved=sum(1 for r in rows if r.get('resolved'))
        abandoned=sum(1 for r in rows if r.get('abandoned'))
        in_pos=sum(1 for r in rows if r.get('in_position'))
        return {
            'total':len(rows),
            'resolved':resolved,
            'abandoned':abandoned,
            'unresolved':len(rows)-resolved-abandoned,
            'in_position_rows':in_pos,
            'epic':epic,
            'horizons':list(HORIZONS),
        }


def maybe_record(store, **kwargs):
    """Best-effort recorder for AdaptiveBrain.tick / poll hooks."""
    try:
        return ForecastMemory(store).record(**kwargs)
    except Exception:
        return None


def maybe_resolve(store, candles, *, epic=None, now_utc=None):
    """Resolve from completed candles; prefer Store HISTORICAL when dense enough."""
    try:
        mem=ForecastMemory(store)
        use=list(candles or [])
        if epic and hasattr(store,'candles'):
            try:
                hist=store.candles(str(epic),n=500,historical_only=True) or []
                # Prefer HISTORICAL if it covers the contiguous horizon window.
                if len(hist)>=max(HORIZONS)+2:
                    use=hist
            except Exception:
                pass
        out=mem.resolve_with_candles(use,epic=epic,now_utc=now_utc)
        return int(out.get('resolved') or 0) if isinstance(out,dict) else int(out or 0)
    except Exception:
        return 0
