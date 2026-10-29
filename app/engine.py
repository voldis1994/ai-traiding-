"""Automatic broker/AI/order/learning cycle and persistent event journal.

Non-idempotent operations are journaled BEFORE transmission. Ambiguous outcomes
are reconciled, never blindly repeated. No order until an initial mobile trade has
been detected in this authenticated session and broker confirmed its closure.
"""
import asyncio
import json
import math
import re
import sqlite3
import time
from collections import deque
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
import numpy as np
from .core import NeuralCore
from .autonomy import AutoEvolution
from .math_rules import stop_take, ohlc_range_distance
from . import bracket_learn
from .protection_ai import propose_broker_trail, trail_extreme_from_broker
from .storage import DATA
from .market_context import market_features, candle_bucket, candle_time, FEATURE_SCHEMA, FEATURE_DIM
from .market_fusion import FUSION_SCHEMA, FUSION_DIM, fusion_features, fusion_dim_for, EXTERNAL_NAMES
from .feature_registry import FeatureRegistry
from .feature_discovery import FeatureDiscovery
from .feature_validation import FeatureValidator
from .profit_learning import ProfitLearning
from .model_validation import ModelValidator
from . import learning_generation

DB=DATA/'robot.sqlite3'

def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def clock():
    """Parse engine `now()` into an aware UTC datetime (monkeypatch-friendly in tests)."""
    value=now()
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _safe_broker_error(exc):
    """Record only Capital.com errorCode, never session tokens or request headers."""
    response=getattr(exc,'response',None)
    if response is None:return str(exc)[:180] if not hasattr(exc,'request') else type(exc).__name__
    try:
        body=response.json()
        if isinstance(body,dict):
            return str(body.get('errorCode') or body.get('error') or body.get('message') or 'HTTP error')[:180]
    except (ValueError,TypeError):pass
    return 'HTTP error (no structured broker code)'

def _num(value,default=None):
    try:
        result=float(value)
        return result if math.isfinite(result) else default
    except (ValueError,TypeError):return default

class Store:
    def __init__(self,path=DB):
        self.path=Path(path)
        self.db=sqlite3.connect(str(path),check_same_thread=False)
        try:
            # Refuse trading against malformed state/order journals. Recovery is
            # always explicit; NEVER discard or recreate the old DB silently.
            check=self.db.execute('PRAGMA integrity_check').fetchall()
            if check!=[('ok',)]:
                raise sqlite3.DatabaseError('integrity_check failed: '+str(check[:3]))
            self.db.execute('PRAGMA journal_mode=WAL')
        except sqlite3.DatabaseError as exc:
            self.db.close()
            raise RuntimeError(
                'SQLite robot.sqlite3 is damaged. STOP live trading, retain '
                'robot.sqlite3 plus its -wal/-shm files, and run '
                'repair_database_windows.bat with the robot CLOSED. '
                'No training or order data has been deleted.'
            ) from exc
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,time TEXT,kind TEXT,message TEXT,details TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY,time TEXT,epic TEXT,action TEXT,scores TEXT,price REAL,model_step INTEGER,label TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (deal_id TEXT PRIMARY KEY,epic TEXT,size TEXT,direction TEXT,opened TEXT,closed TEXT,entry REAL,exit REAL,pnl REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY,time TEXT,kind TEXT,deal_id TEXT,reference TEXT,payload TEXT,status TEXT)')
        self.db.execute("CREATE TABLE IF NOT EXISTS candles (epic TEXT,at TEXT,open REAL,high REAL,low REAL,close REAL,source TEXT DEFAULT 'QUOTE',PRIMARY KEY(epic,at))")
        candle_columns={r[1] for r in self.db.execute('PRAGMA table_info(candles)')}
        if 'source' not in candle_columns:
            self.db.execute("ALTER TABLE candles ADD COLUMN source TEXT DEFAULT 'QUOTE'")
        # Per-bar spread for Walk-Forward validation (never reuse one live spread for history).
        if 'spread' not in candle_columns:
            self.db.execute('ALTER TABLE candles ADD COLUMN spread REAL')
        trade_columns={r[1] for r in self.db.execute('PRAGMA table_info(trades)')}
        # Cash P&L must carry account currency + source so GOLD USD quotes are
        # never mixed with GBP account profit as if they were the same unit.
        for name,kind in (('pnl_currency','TEXT'),('pnl_source','TEXT')):
            if name not in trade_columns:self.db.execute(f'ALTER TABLE trades ADD COLUMN {name} {kind}')
        # Migration from the earlier project's signals table.
        columns={r[1] for r in self.db.execute('PRAGMA table_info(signals)')}
        if 'label' not in columns:self.db.execute('ALTER TABLE signals ADD COLUMN label TEXT')
        # Legacy labels were produced using one-poll price moves. Keep them for
        # audit, but never mix them with the new multi-step supervised targets.
        for name,kind in (('spread','REAL'),('tick_size','REAL'),('label_version','INTEGER')):
            if name not in columns:self.db.execute(f'ALTER TABLE signals ADD COLUMN {name} {kind}')
        self.db.commit()
    def event(self,kind,message,details=None):
        self.db.execute('INSERT INTO events(time,kind,message,details) VALUES(?,?,?,?)',
            (now(),kind,message,json.dumps(details or {},default=str)));self.db.commit()
    def recent(self,n=100):
        cols=('time','kind','message','details')
        return [dict(zip(cols,r)) for r in self.db.execute('SELECT time,kind,message,details FROM events ORDER BY id DESC LIMIT ?', (n,))]
    def set(self,k,v):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(k,json.dumps(v,default=str)));self.db.commit()
    def get(self,k,default=None):
        r=self.db.execute('SELECT value FROM state WHERE key=?',(k,)).fetchone();return json.loads(r[0]) if r else default
    def signals(self,n=100):
        cols=('time','epic','action','scores','price','model_step','label')
        return [dict(zip(cols,r)) for r in self.db.execute('SELECT time,epic,action,scores,price,model_step,label FROM signals ORDER BY id DESC LIMIT ?', (n,))]
    def order(self,kind,deal_id,payload):
        cur=self.db.execute('INSERT INTO orders(time,kind,deal_id,reference,payload,status) VALUES(?,?,?,?,?,?)',
            (now(),kind,deal_id,None,json.dumps(payload,default=str),'PENDING'))
        self.db.commit();return cur.lastrowid
    def order_update(self,order_id,status,reference=None):
        self.db.execute('UPDATE orders SET status=?,reference=COALESCE(?,reference) WHERE id=?',
            (status,reference,order_id));self.db.commit()
    def unresolved(self):
        cols=('id','time','kind','deal_id','reference','payload','status')
        return [dict(zip(cols,r)) for r in self.db.execute("SELECT id,time,kind,deal_id,reference,payload,status FROM orders WHERE status IN ('PENDING','UNCERTAIN') ORDER BY id")]
    def upsert_trade(self,p):
        self.db.execute('INSERT OR IGNORE INTO trades(deal_id,epic,size,direction,opened,entry) VALUES(?,?,?,?,?,?)',
            (p['dealId'],p['epic'],str(p['size']),p['direction'],now(),_num(p.get('level'))))
        self.db.commit()
    def close_trade(self,deal_id,exit_price=None,pnl=None,*,currency=None,source=None,overwrite=False):
        """Persist exit and realized cash P&L.

        Never invent (exit-entry)*size as account money — GOLD is USD-quoted while
        Capital.com profit/UPL is in account currency (often GBP). Only store cash
        when the broker (or an explicit caller) supplies it, with currency+source.
        overwrite=True lets a later CLOSE confirm replace a mid guess.
        """
        exit_n=_num(exit_price);pnl_n=_num(pnl)
        currency=(currency or '').strip().upper() or None
        source=(source or '').strip() or None
        if overwrite:
            self.db.execute(
                'UPDATE trades SET closed=COALESCE(closed,?), '
                'exit=CASE WHEN ? IS NOT NULL THEN ? ELSE exit END, '
                'pnl=CASE WHEN ? IS NOT NULL THEN ? ELSE pnl END, '
                'pnl_currency=CASE WHEN ? IS NOT NULL THEN ? ELSE pnl_currency END, '
                'pnl_source=CASE WHEN ? IS NOT NULL THEN ? ELSE pnl_source END '
                'WHERE deal_id=?',
                (now(),exit_n,exit_n,pnl_n,pnl_n,currency,currency,source,source,deal_id))
        else:
            self.db.execute(
                'UPDATE trades SET closed=COALESCE(closed,?), '
                'exit=COALESCE(?,exit), '
                'pnl=CASE WHEN ? IS NOT NULL THEN ? ELSE pnl END, '
                'pnl_currency=CASE WHEN ? IS NOT NULL THEN ? ELSE pnl_currency END, '
                'pnl_source=CASE WHEN ? IS NOT NULL THEN ? ELSE pnl_source END '
                'WHERE deal_id=?',
                (now(),exit_n,pnl_n,pnl_n,currency,currency,source,source,deal_id))
        self.db.commit()
    def trades(self,n=150):
        cols=('deal_id','epic','size','direction','opened','closed','entry','exit','pnl',
              'pnl_currency','pnl_source')
        rows=[dict(zip(cols,r)) for r in self.db.execute(
            'SELECT deal_id,epic,size,direction,opened,closed,entry,exit,pnl,'
            'pnl_currency,pnl_source FROM trades '
            'ORDER BY opened DESC LIMIT ?',(n,))]
        for row in rows:
            if row.get('pnl') is not None:
                try:row['pnl']=round(float(row['pnl']),4)
                except (TypeError,ValueError):pass
        return rows
    def repair_broker_pnl_currency(self, account_currency):
        """Force broker-source cash rows onto account currency (fix GOLD→USD tags)."""
        acct=(account_currency or '').strip().upper()
        if not acct:return 0
        cur=self.db.execute(
            "UPDATE trades SET pnl_currency=? WHERE pnl_source='broker' "
            "AND pnl IS NOT NULL AND closed IS NOT NULL "
            "AND (pnl_currency IS NULL OR UPPER(pnl_currency)!=?)",
            (acct,acct))
        self.db.commit()
        return int(cur.rowcount or 0)
    def candle(self,epic,bar,source='QUOTE'):
        if source not in ('QUOTE','HISTORICAL'):raise ValueError('Invalid candle provenance')
        spread=_num(bar.get('spread'))
        self.db.execute(
            'INSERT OR REPLACE INTO candles(epic,at,open,high,low,close,source,spread) VALUES(?,?,?,?,?,?,?,?)',
            (epic,str(bar['at']),bar['open'],bar['high'],bar['low'],bar['close'],source,spread))
        self.db.commit()
    def candles(self,epic,n=120,historical_only=False):
        cols=('at','open','high','low','close','spread')
        where="epic=? AND source='HISTORICAL'" if historical_only else 'epic=?'
        rows=list(self.db.execute(
            f'SELECT at,open,high,low,close,spread FROM candles WHERE {where} ORDER BY at DESC LIMIT ?',
            (epic,n)))
        return [dict(zip(cols,r)) for r in reversed(rows)]

class AdaptiveBrain:
    """Instrument-isolated online learner synced to the M1 trading tempo.

    Background labels use the NEXT completed minute (HORIZON=1) so learning
    does not trail eight bars behind live decisions. Real closed-trade P&L is
    the primary corrective signal (see SingleTradeEngine._learn_closed_trade).
    Uncalibrated softmax scores are not probabilities of profitable trades.
    """
    LABEL_VERSION=4  # v4: 1-bar market labels + trade P&L regret learning
    HORIZON=1        # same cadence as M1 decisions — not an 8-minute lag
    GROWTH_INTERVAL=8   # faster structural growth so the net adapts with trades
    FAST_GROWTH_INTERVAL=4  # sustained high reconstruction error

    def __init__(self,store,n=16,data_dir=None):
        self.store=store
        self.path=(Path(data_dir) if data_dir else DATA)/'brain.npz'
        self.initial_neurons=n
        self.core=NeuralCore(n=n)
        self.epic=None
        self._legacy_epic=self.store.get('legacy_brain_epic')
        self.history=deque(maxlen=1000)
        self.pending=deque()
        self.correct=0;self.labeled=0;self.last_change=0.;self.last_mid=None
        self.prior_record=None;self.last_regime='LEARNING'
        self.feature_schema=None
        self.fusion_labels=0
        # Runtime dims track base OHLC (+ approved dynamic) and fusion external block.
        self.ohlc_dim=FEATURE_DIM
        self.fusion_dim=FUSION_DIM
        self.ohlc_schema=FEATURE_SCHEMA
        self.fusion_schema=FUSION_SCHEMA
        self.model_version=0
        # Backward-compatible old brain.npz: bind once to the first observed EPIC.
        # Once bound, never load the saved brain for another instrument.
        self.load()
        if self._legacy_epic:
            self.epic=self._legacy_epic
            self._restore_context(self.epic)
            self._restore_pending(self.epic)

    def _path_for(self,epic):
        if epic==self._legacy_epic:return self.path
        import hashlib
        return self.path.parent/('brain_'+hashlib.sha256(epic.encode('utf8')).hexdigest()[:16]+'.npz')

    def _restore_context(self,epic):
        self.history=deque(maxlen=1000);self.pending=deque()
        self.correct=0;self.labeled=0;self.last_mid=None;self.last_change=0.
        self.prior_record=None;self.last_regime='LEARNING'
        # Old (one-tick) labels remain in the audit table, but must NOT silently
        # become evidence for the new eight-observation training objective.
        rows=list(self.store.db.execute('''
            SELECT id,action,scores,price,label,spread,tick_size,time
            FROM signals WHERE epic=? AND label_version=? ORDER BY id DESC LIMIT 1000
        ''',(epic,self.LABEL_VERSION)))
        for signal_id,action,scores,price,label,spread,tick_size,bar_time in reversed(rows):
            try:
                cls=int(label) if label is not None else None
                parsed=json.loads(scores)
                if not isinstance(parsed,dict):continue
                self.history.append({'signal_id':signal_id,'price':price,'scores':parsed,
                    'action':action,'label':cls,'time':bar_time,
                    'spread_bps':float(spread)*10000/float(price)
                    if spread is not None and price and price>0 else 0.})
            except (ValueError,TypeError,ZeroDivisionError):continue
        good=self.store.db.execute('''SELECT action,label FROM signals
            WHERE epic=? AND label_version=? AND label IS NOT NULL''',(epic,self.LABEL_VERSION))
        for action,label in good:
            self.labeled+=1
            self.correct+=int(action==('BUY','SELL','HOLD')[int(label)])

    def _json_transition(self,transition):
        if transition is None:return None
        return {'z':np.asarray(transition['z']).tolist(),
                'h':np.asarray(transition['h']).tolist(),
                'c':np.asarray(transition['c']).tolist(),
                'dt':float(transition['dt']),
                'neuron_ids':np.asarray(transition.get('neuron_ids',np.arange(len(transition['h'])))).tolist()}

    def _activate(self,epic):
        if self.epic==epic:return
        if self.epic is not None:self.save()
        if self._legacy_epic is None:
            self._legacy_epic=epic
            self.store.set('legacy_brain_epic',epic)
            # In-memory core already contains the legacy weights, if present.
        else:
            self.core=NeuralCore(n=self.initial_neurons)
            self.feature_schema=None  # Schema belongs to the selected EPIC, not the prior one.
            self.fusion_labels=0
            self.load(self._path_for(epic),expected_epic=epic)
        self.epic=epic
        self._restore_context(epic)
        self._restore_pending(epic)
        self.store.event('learning','Instrument learning context activated',{'epic':epic})

    def load(self,path=None,expected_epic=None):
        path=Path(path) if path else self.path
        if not path.exists():return
        try:
            with np.load(path,allow_pickle=False) as d:
                if expected_epic and 'epic' in d and str(d['epic'])!=expected_epic:
                    raise ValueError('Brain file instrument mismatch')
                names=('P','W','R','O','b','h','c','tau','tau_c','mu','nu','age','utility','G')
                for name in names:setattr(self.core,name,d[name].copy())
                self.core.n=len(self.core.h)
                self.core.d=self.core.P.shape[1]
                self.core.last_z=np.zeros(self.core.d)
                self.core.steps=int(d['steps']);self.core.vbar=float(d['vbar'])
                for name in ('births','deaths'):
                    if name in d:setattr(self.core,name,int(d[name]))
                self.feature_schema=str(d['feature_schema']) if 'feature_schema' in d else None
                self.fusion_labels=int(d['fusion_labels']) if 'fusion_labels' in d else 0
                self.ohlc_dim=int(d['ohlc_dim']) if 'ohlc_dim' in d else FEATURE_DIM
                self.fusion_dim=int(d['fusion_dim']) if 'fusion_dim' in d else FUSION_DIM
                self.ohlc_schema=str(d['ohlc_schema']) if 'ohlc_schema' in d else FEATURE_SCHEMA
                self.fusion_schema=str(d['fusion_schema']) if 'fusion_schema' in d else FUSION_SCHEMA
                self.model_version=int(d['model_version']) if 'model_version' in d else 0
                if ((self.feature_schema==self.fusion_schema and self.core.d!=self.fusion_dim)
                    or (self.feature_schema==self.ohlc_schema and self.core.d!=self.ohlc_dim)
                    or (self.feature_schema==FUSION_SCHEMA and self.core.d!=FUSION_DIM and 'fusion_dim' not in d)
                    or (self.feature_schema==FEATURE_SCHEMA and self.core.d!=FEATURE_DIM and 'ohlc_dim' not in d)):
                    raise ValueError('Stored neural feature schema and input weights disagree')
                if 'neuron_ids' in d:
                    self.core.neuron_ids=d['neuron_ids'].astype(np.int64).copy()
                    self.core.next_neuron_id=int(d['next_neuron_id'])
                else:
                    self.core.neuron_ids=np.arange(self.core.n,dtype=np.int64)
                    self.core.next_neuron_id=self.core.n
        except Exception as exc:
            self.store.event('error','Neural state load failed',{'type':type(exc).__name__,'file':path.name})
            # Never continue with a partly loaded/mismatched network.
            self.core=NeuralCore(n=self.initial_neurons)
            self.feature_schema=None
            self.fusion_labels=0
            self.ohlc_dim=FEATURE_DIM;self.fusion_dim=FUSION_DIM
            self.ohlc_schema=FEATURE_SCHEMA;self.fusion_schema=FUSION_SCHEMA
            self.model_version=0

    def _restore_pending(self,epic):
        path=self._path_for(epic)
        if not path.exists():return
        try:
            with np.load(path,allow_pickle=False) as d:
                if 'pending_json' not in d:return
                saved_schema=str(d['feature_schema']) if 'feature_schema' in d else 'legacy_quote_v2'
                if saved_schema!=(self.feature_schema or 'legacy_quote_v2'):return
                if 'epic' in d and str(d['epic'])!=epic:return
                records=json.loads(str(d['pending_json']))
                for p in records[-self.HORIZON:]:
                    if p['epic']==epic and p['signal_id'] is not None:
                        self.pending.append(p)
                if self.pending:
                    latest=self.pending[-1]
                    self.last_mid=float(latest['mid']);self.last_change=float(latest['change'])
        except Exception as exc:
            self.pending.clear()
            self.store.event('warning','Pending labels could not be restored',{'type':type(exc).__name__})

    def save(self):
        self.path.parent.mkdir(exist_ok=True,parents=True)
        path=self._path_for(self.epic) if self.epic else self.path
        tmp=path.with_suffix('.tmp.npz');c=self.core
        payload={k:getattr(c,k) for k in ('P','W','R','O','b','h','c','tau','tau_c','mu','nu','age','utility','G','neuron_ids')}
        payload.update(steps=c.steps,vbar=c.vbar,births=c.births,deaths=c.deaths,
                       next_neuron_id=c.next_neuron_id,epic=self.epic or '',
                       feature_schema=self.feature_schema or 'legacy_quote_v2',
                       fusion_labels=self.fusion_labels,
                       ohlc_dim=self.ohlc_dim,fusion_dim=self.fusion_dim,
                       ohlc_schema=self.ohlc_schema,fusion_schema=self.fusion_schema,
                       model_version=self.model_version,
                       pending_json=json.dumps(list(self.pending),separators=(',',':')))
        np.savez_compressed(tmp,**payload)
        tmp.replace(path)

    def backup_weights(self, tag='rollback'):
        """Copy current neural weights to a versioned .npz — real rollback fodder."""
        self.save()
        src=self._path_for(self.epic) if self.epic else self.path
        if not src.exists():return None
        safe=''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in str(tag))[:48]
        dest=src.with_name(f"{src.stem}.bak_{safe}_v{int(self.model_version)}.npz")
        import shutil
        shutil.copy2(src,dest)
        return str(dest)

    def restore_weights(self, weights_path):
        """Reload neural weights from a backup file into the live core."""
        path=Path(weights_path)
        if not path.exists():
            self.store.event('error','Weight restore missing file',{'path':str(path)})
            return False
        before_ver=self.model_version
        self.load(path,expected_epic=self.epic)
        # Persist restored weights as the active brain file.
        self.save()
        self.store.event('learning','Neural weights restored from backup',
            {'path':path.name,'from_version':before_ver,
             'model_version':self.model_version,'inputs':self.core.d,
             'neurons':self.core.n})
        return True

    def apply_registry(self, registry):
        """Sync dims/schemas from FeatureRegistry; expand weights if approved set grew.

        Vector layout is always [BASE_OHLC | DYNAMIC | EXTERNAL]. When already on
        fusion and new DYNAMIC features are approved, columns are *inserted*
        before the EXTERNAL block — never appended after peers/news.
        """
        if registry is None:return False
        old_ohlc=self.ohlc_dim
        ohlc_d=registry.ohlc_dim();fus_d=registry.fusion_dim()
        ohlc_s=registry.ohlc_schema();fus_s=registry.fusion_schema()
        grew=ohlc_d>self.ohlc_dim or fus_d>self.fusion_dim
        self.ohlc_dim=ohlc_d;self.fusion_dim=fus_d
        self.ohlc_schema=ohlc_s;self.fusion_schema=fus_s
        if self.feature_schema in (None,'legacy_quote_v2'):return False
        fs=self.feature_schema or ''
        on_fusion=(fs==self.fusion_schema or fs==FUSION_SCHEMA or 'intermarket_news' in fs)
        if (fs==self.ohlc_schema or fs==FEATURE_SCHEMA or
                (FEATURE_SCHEMA in fs and 'intermarket_news' not in fs)):
            target=self.ohlc_dim;schema=self.ohlc_schema;insert_at=None
        elif on_fusion:
            target=self.fusion_dim;schema=self.fusion_schema
            # New dynamic slots land at old OHLC boundary (before EXTERNAL).
            insert_at=old_ohlc if ohlc_d>old_ohlc else None
        else:
            return False
        if self.core.d==target and self.feature_schema==schema:return False
        if self.core.d>target:
            raise ValueError('Registry shrink is not supported without explicit rollback')
        if self.core.d<target:
            self.core.expand_inputs(target,insert_at=insert_at)
            self.pending.clear()
            self.core.last_transition=None
            self.model_version+=1
            self.store.event('learning','Neural inputs expanded for approved features',
                {'epic':self.epic,'inputs':target,'schema':schema,
                 'insert_at':insert_at,'layout':'base|dynamic|external',
                 'model_version':self.model_version})
        self.feature_schema=schema
        self.save()
        return grew

    def _switch_to_market_features(self):
        """Versioned migration: never apply old 3-input weights to new OHLC units.

        Keep legacy weights on disk as an audit/rollback backup; old signals remain
        in SQLite under their original label_version, never replayed as v3.
        """
        if self.feature_schema==self.ohlc_schema and self.core.d==self.ohlc_dim:return
        path=self._path_for(self.epic)
        if path.exists():
            backup=path.with_name(path.stem+'.before_mtf_v3.npz')
            if not backup.exists():
                import shutil
                shutil.copy2(path,backup)
        self.core=NeuralCore(n=self.initial_neurons,d=self.ohlc_dim)
        self.feature_schema=self.ohlc_schema
        self.history=deque(maxlen=1000);self.pending=deque()
        self.correct=0;self.labeled=0
        self.last_change=0.;self.last_mid=None;self.prior_record=None
        self.last_regime='LEARNING'
        self.store.set('mtf_last_bar:'+self.epic,None)
        self.store.event('learning','New OHLC feature schema activated; old neural weights backed up',
            {'epic':self.epic,'feature_schema':self.ohlc_schema,'input_count':self.ohlc_dim})
        self.save()

    def _extend_to_external_features(self):
        """Expand input dimensions WITHOUT reinitializing learned neuron weights.

        Existing OHLC pending transitions are dropped — z length changes.
        Historical labels and network weights on shared columns are preserved.
        """
        if self.feature_schema==self.fusion_schema and self.core.d==self.fusion_dim:return
        if self.feature_schema not in (self.ohlc_schema,FEATURE_SCHEMA) or self.core.d!=self.ohlc_dim:
            raise ValueError('External feature expansion requires complete OHLC state')
        path=self._path_for(self.epic)
        if path.exists():
            backup=path.with_name(path.stem+'.before_fusion_v4.npz')
            if not backup.exists():
                import shutil
                shutil.copy2(path,backup)
        c=self.core
        c.expand_inputs(self.fusion_dim,init_scale=0.)
        # A shorter transition's z is not compatible with the expanded input.
        self.pending.clear()
        c.h[:]=0.;c.c[:]=0.
        self.last_mid=None;self.last_change=0.
        self.feature_schema=self.fusion_schema
        self.fusion_labels=0
        self.model_version+=1
        self.store.event('learning','Cross-market and news features joined neural inputs; old weights preserved',
            {'epic':self.epic,'feature_schema':self.fusion_schema,'inputs':self.fusion_dim,
             'preserved_steps':c.steps,'old_pending_labels_dropped':True,
             'model_version':self.model_version})
        self.save()

    @staticmethod
    def _label(from_mid,to_mid,from_spread,to_spread):
        # Full bid/ask spread in PRICE units, not a relative percentage or ticks.
        # Thresholding the entire horizon return instead of one poll avoids
        # spuriously labeling every small trending step as HOLD.
        delta=to_mid-from_mid
        cost=max(from_spread,to_spread)
        return 0 if delta>cost else (1 if delta < -cost else 2)

    def tick(self,mid,spread,tick_size,epic,dt=1.,features=None,bar_time=None,external_features=None):
        if (not isinstance(epic,str) or not epic or
            not all(map(math.isfinite,(mid,spread,tick_size))) or
            mid<=0 or tick_size<=0 or spread<0):return None
        self._activate(epic)
        if features is not None:
            if (bar_time is None or not isinstance(features,(list,tuple,np.ndarray))
                or not np.isfinite(features).all()):return None
            if external_features is not None:
                if len(features)!=self.fusion_dim or external_features is not True:return None
                # New or old OHLC weights are first moved to a known, versioned schema.
                if self.feature_schema!=self.fusion_schema:
                    self._switch_to_market_features()
                    self._extend_to_external_features()
            else:
                if len(features)!=self.ohlc_dim:return None
                # Never run a fusion network on fabricated zero external data.
                if self.feature_schema==self.fusion_schema:return None
                self._switch_to_market_features()
            prev_bar=self.store.get('mtf_last_bar:'+epic)
            if prev_bar and bar_time<=prev_bar:return None
            if prev_bar:
                from .market_context import candle_time
                gap=(candle_time(bar_time)-candle_time(prev_bar)).total_seconds()/60.
                if gap!=1.:
                    # Missing minute(s) invalidate the pending next-bar target.
                    self.pending.clear();self.core.h[:]=0;self.core.c[:]=0
                    self.last_mid=None;self.last_change=0.
                    self.store.event('learning','Skipped discontinuous OHLC minutes',
                        {'epic':epic,'gap_minutes':gap})
        prev=self.last_mid if self.last_mid is not None else mid
        change=(mid-prev)/tick_size
        previous_change=self.last_change
        x=list(features) if features is not None else [change,spread/tick_size,change-previous_change]

        # Label an older observation, never the latest one and never another EPIC.
        # Do this before step/growth, so gradients always have a safe state mapping.
        if len(self.pending)>=self.HORIZON:
            past=self.pending.popleft()
            if past['epic']==epic:
                label=self._label(float(past['mid']),mid,float(past['spread']),spread)
                transition=past.get('transition')
                if transition is not None:
                    self.core.update_utility(past['h'],past['c'],label,
                        neuron_ids=past.get('neuron_ids'))
                    loss=self.core.learn(transition,label)
                    if loss is not None and self.feature_schema==self.fusion_schema:
                        self.fusion_labels+=1
                    if loss is not None and (self.labeled+1)%100==0:
                        self.store.event('learning','Horizon-label learning checkpoint',
                            {'loss':loss,'horizon':self.HORIZON,'epic':epic})
                self.labeled+=1
                self.correct+=int(past['action']==('BUY','SELL','HOLD')[label])
                self.store.db.execute('UPDATE signals SET label=?,label_version=? WHERE id=? AND epic=?',
                    (str(label),self.LABEL_VERSION,past['signal_id'],epic))
                self.store.db.commit()
                sid=past.get('signal_id')
                if sid is not None:
                    for item in self.history:
                        if item.get('signal_id')==sid:
                            item['label']=label
                            break

        result=self.core.step(x,max(.1,min(float(dt),60)))
        # Structural growth is separate from learning; avoid allocating a neuron
        # nearly every quote and overwhelming gradients/CPU/memory.
        growth_interval=(self.FAST_GROWTH_INTERVAL if self.core.vbar>2.0
                         else self.GROWTH_INTERVAL)
        if self.core.steps%growth_interval==0:
            from .self_params import get as sp_get
            z=np.asarray(result['z'])
            grow_th=float(sp_get(self.store,'grow_error_threshold',.55))
            if self.core.grow_if_needed(z,threshold=grow_th,nmax=None):
                self.store.event('neural','Neuron automatically added',{'neurons':self.core.n})
        if self.core.steps%80==0:
            from .self_params import get as sp_get
            deleted=self.core.prune(
                min_age=int(sp_get(self.store,'prune_min_age',80)),
                threshold=float(sp_get(self.store,'prune_utility_threshold',-.01)))
            if deleted:self.store.event('neural','Low utility neurons removed',
                {'count':deleted,'neurons':self.core.n})
        cur=self.store.db.execute('''INSERT INTO signals
            (time,epic,action,scores,price,model_step,label,spread,tick_size,label_version)
            VALUES(?,?,?,?,?,?,NULL,?,?,?)''',
            (bar_time if features is not None else now(),epic,result['action'],json.dumps(result['scores']),mid,self.core.steps,
             spread,tick_size,self.LABEL_VERSION))
        self.store.db.commit()
        tr=self._json_transition(self.core.last_transition)
        self.pending.append({'epic':epic,'mid':mid,'spread':spread,'tick_size':tick_size,
            'change':change,'bar_time':bar_time,'transition':tr,'h':self.core.h.tolist(),
            'c':self.core.c.tolist(),'neuron_ids':self.core.neuron_ids.tolist(),
            'action':result['action'],'signal_id':cur.lastrowid})
        self.prior_record=self.pending[-1]
        self.history.append({'signal_id':cur.lastrowid,'price':mid,'scores':result['scores'],
            'action':result['action'],'label':None,'time':bar_time if features is not None else now(),
            'spread_bps':10000*spread/mid})
        self.last_regime=('HIGH VOLATILITY' if abs(change)>10 else
            'RANGING' if abs(change)<spread/tick_size else 'TRENDING')
        self.last_mid=mid;self.last_change=change
        if features is not None:self.store.set('mtf_last_bar:'+epic,bar_time)
        # Persist the complete short pending horizon each poll, to avoid losing
        # training state when the Windows process is restarted or switched.
        self.save()
        result['market_regime']=self.last_regime
        result['confidence']=max(result['scores'].values())
        result['accuracy_proxy']=self.correct/self.labeled if self.labeled else None
        return result


from .external_risk import assess_news
from .calendar_feed import refresh_calendar
from .instrument_profile import instrument_profile
from .intermarket import IntermarketMonitor
import os

class SingleTradeEngine:
    def __init__(self,store,brain):
        self.store=store;self.brain=brain;self.broker=None
        self.armed=False;self.execution_enabled=False;self.mandate=None
        self.positions=[];self.account={};self.market={};self.last_signal=None;self.last_error=None
        self.position_age=0;self.last_position_id=None;self.last_closed_step=-1
        self.last_entry_signal_step=-1;self.last_order_ref=None
        self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
        self.pending_order=False;self.pending_close=False;self.lock=asyncio.Lock()
        self._pending_entry_brain=None  # neural state at the decision that opened the trade
        self.last_time=None;self.last_account_tick=0;self.last_candle_tick=-20
        self.policy=AutoEvolution(store);self.tick_count=0;self.latest_close_id=None
        self.market_context={'ready':False,'reason':'Waiting for complete multi-timeframe candles'}
        self.latest_fusion_ready=False
        self.last_policy_decision='HOLD';self.last_entry_reason='Awaiting a fresh signal'
        self.last_entry_audit_key=None
        self.protected=set();self._seen_initial=False;self._unresolved_at_login=False
        self._paused_for_multi=False;self._paused_for_mismatch=False;self._paused_for_read=False;self.operator_paused=False
        self.protection_brief={'status':'Awaiting first robot-opened position'}
        self.last_protection_bar=None
        self.last_calendar_refresh=0.0
        self._calendar_task=None
        self.calendar_feed_error=None
        self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
        self.intermarket=IntermarketMonitor()
        self._peer_refresh_task=None
        self._peer_requested_bar=None
        self.news_calendar_path=self.store.path.parent/'economic_calendar.json' if hasattr(self.store,'path') else __import__('pathlib').Path('data/economic_calendar.json')
        # Autonomous self-development (OHLC discovery → validation → dim expand).
        self.feature_registry=FeatureRegistry(store)
        self.feature_discovery=FeatureDiscovery(store)
        self.feature_validator=FeatureValidator(
            store,self.feature_registry,discovery=self.feature_discovery)
        self.profit_learning=ProfitLearning(store)
        self.model_validator=ModelValidator(store)
        from .shadow_paper import ShadowPaperJournal
        self.shadow_paper=ShadowPaperJournal(store)
        if hasattr(self.brain,'apply_registry'):
            self.brain.apply_registry(self.feature_registry)
        self._last_self_dev_bar=None
        self.shadow_brain=None  # optional alternate architecture (never executes LIVE)
    def _news_risk(self, when=None):
        # Use the engine clock so candle learning, news features and entry guards
        # share one timeline (and tests can freeze time via monkeypatching now()).
        result=assess_news(self.mandate['epic'] if self.mandate else '',
                           self.news_calendar_path,
                           now=when or clock(),
                           instrument_currencies=self.instrument_profile['currencies'])
        if result.get('enabled') and result.get('status') in ('CLEAR','NEWS_WINDOW'):
            # A transient network failure does not invalidate a still-current
            # verified cached calendar; the freshness checks are in assess_news.
            if self.calendar_feed_error:
                return dict(result,reason=result['reason']+' (using fresh cached calendar)')
            return result
        if self.calendar_feed_error:
            return {'enabled':True,'entry_allowed':False,'status':'CALENDAR_FEED_ERROR',
                    'reason':self.calendar_feed_error}
        if not self.last_calendar_refresh and not self.news_calendar_path.exists():
            return {'enabled':True,'entry_allowed':False,'status':'CALENDAR_WAITING',
                    'reason':'Waiting for Forex Factory calendar download'}
        if not result.get('enabled'):
            return {'enabled':True,'entry_allowed':False,'status':'CALENDAR_UNAVAILABLE',
                    'reason':'News protection inactive or calendar file missing; new entries blocked'}
        return result

    async def _refresh_calendar_if_due(self):
        moment=time.monotonic()
        if self.last_calendar_refresh and moment-self.last_calendar_refresh<300:return
        self.last_calendar_refresh=moment
        result=await refresh_calendar(self.news_calendar_path)
        self.calendar_feed_error=None if result['updated'] else result['reason']
        if not result['updated']:
            self.store.event('warning','Forex Factory calendar unavailable; new entries blocked',
                             {'reason':result['reason']})

    def _schedule_calendar_in_background(self):
        """Keep news learning current during an open trade without delaying SL/TP."""
        if self._calendar_task is not None and not self._calendar_task.done():return
        if self.last_calendar_refresh and time.monotonic()-self.last_calendar_refresh<300:return
        self._calendar_task=asyncio.create_task(self._refresh_calendar_if_due())

    def cancel_calendar_refresh(self):
        if self._calendar_task is not None and not self._calendar_task.done():
            self._calendar_task.cancel()
        self._calendar_task=None

    def _external_entry_check(self,action):
        """Economic-event safety only. Peer movements are already neural inputs.

        Vetoing a neural BUY/SELL a *second* time using correlation heuristics
        recreates the user's old permanent-HOLD trap. Unavailable peer data
        is handled by the neural feature readiness check before this point.
        """
        news=self._news_risk()
        if not news['entry_allowed']:
            return {'entry_allowed':False,'status':news['status'],'reason':'News guard: '+news['reason']}
        return {'entry_allowed':True,'status':'AI_INTERMARKET_FUSION',
                'reason':'Intermarket evidence is evaluated by the neural model, not a duplicate veto'}

    def cancel_peer_refresh(self):
        task=self._peer_refresh_task
        if task is not None and not task.done():task.cancel()
        self._peer_refresh_task=None
        self._peer_requested_bar=None

    def _refresh_peers_without_delaying_protection(self, instrument, candles):
        """Read reference markets in background while a position is open.

        Never let slow secondary-price GETs postpone broker position protection.
        At most one refresh per completed candle; inference checks exact bar time.
        """
        bar=self.market_context.get('bar_time')
        if not bar or self._peer_requested_bar==bar:return
        if self._peer_refresh_task and not self._peer_refresh_task.done():return
        self._peer_requested_bar=bar
        monitor=self.intermarket
        broker=self.broker
        epic=self.mandate['epic']
        async def task_body():
            try:
                await monitor.refresh(broker,epic,instrument,candles)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                monitor.state={'status':'UNAVAILABLE','entry_allowed':False,
                    'reason':'Reference refresh error: '+type(exc).__name__,'peers':[]}
                self.store.event('warning','Background reference prices unavailable',
                    {'epic':epic,'type':type(exc).__name__})
        self._peer_refresh_task=asyncio.create_task(task_body())

    def attach(self,broker):
        self.broker=broker;self.armed=True;self.execution_enabled=True
        self.mandate=None;self.positions=[];self.last_position_id=None;self.last_closed_step=-1
        self.last_entry_signal_step=-1;self.position_age=0;self.last_time=None
        self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
        self._seen_initial=False;self.protected=set();self.latest_close_id=None
        self._paused_for_multi=False;self._paused_for_mismatch=False;self._paused_for_read=False;self.operator_paused=False
        self.last_error=None;self.account={};self.last_entry_audit_key=None
        self.latest_fusion_ready=False
        self.last_calendar_refresh=0.;self.calendar_feed_error=None
        self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
        self.cancel_peer_refresh()
        self.cancel_calendar_refresh()
        self.intermarket=IntermarketMonitor()
        self.protection_brief={'status':'Waiting for robot-opened position'}
        self.last_protection_bar=None
        self._unresolved_at_login=bool(self.store.unresolved())
        # Session start MUST NOT reuse a cached adoptive size from SQLite.
        # Size/EPIC authorization is valid only after a live broker position is
        # adopted in THIS login. Stale mandate rows caused restarts to keep
        # trading the old lots without a fresh manual confirmation.
        saved=self.store.get('mandate')
        if isinstance(saved,dict) and (saved.get('epic') or saved.get('size')):
            self.store.set('mandate',None)
            self.store.event('reconcile',
                'Previous adoptive size cleared on login; open a new manual trade to authorize',
                {'previous_epic':saved.get('epic'),'previous_size':saved.get('size'),
                 'previous_deal_id':saved.get('initial_deal_id') or saved.get('last_manual_deal_id'),
                 'data_dir':str(self.store.path.parent)})
        self.mandate=None
        self._seen_initial=False
        self.last_closed_step=-1
        if self._unresolved_at_login:
            self.execution_enabled=False;self.store.event('warning','Previous order outcome unresolved; waiting for reconciliation')
    async def prepare_new_instrument(self):
        """Explicit operator-authorized switch, no open/unresolved broker positions.

        The next first mobile trade becomes the NEW instrument mandate. Neural
        files and historical trades from prior instruments are preserved.
        """
        async with self.lock:
            if not self.broker:raise RuntimeError('Not connected to broker')
            if self.store.unresolved():raise ValueError('Unresolved broker operation')
            await self.sync()  # Fresh read, not a cached open-position count.
            if self.positions:raise ValueError('Close all broker positions before switching')
            if self.store.unresolved():raise ValueError('Unresolved broker operation')
            previous=self.mandate['epic'] if self.mandate else None
            self.mandate=None;self.store.set('mandate',None)
            self.last_signal=None;self.last_closed_step=-1
            self.last_entry_signal_step=-1;self.last_entry_audit_key=None
            self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
            self.last_position_id=None;self._seen_initial=False
            self.position_age=0;self.market={}
            self.market_context={'ready':False,'reason':'Waiting for a NEW manual trade in the selected market'}
            self.latest_fusion_ready=False
            self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
            self.cancel_peer_refresh()
            self.cancel_calendar_refresh()
            self.intermarket=IntermarketMonitor()
            self._paused_for_mismatch=False;self.operator_paused=False
            self.armed=True;self.execution_enabled=True
            self.store.event('control','Market mandate cleared by operator; awaiting new first mobile trade',
                             {'previous_epic':previous})
            return {'ok':True,'previous_epic':previous,'next_step':'Open first trade manually on Capital.com'}

    @staticmethod
    def _normalize_direction(value):
        if value is None:return None
        text=str(value).strip().upper()
        if text in ('BUY','SELL'):return text
        if text in ('LONG','B'):return 'BUY'
        if text in ('SHORT','S'):return 'SELL'
        return None

    @staticmethod
    def _looks_like_epic(value):
        """Capital.com epic codes are compact tokens; 'Gold Spot' is a label, not an epic."""
        if not isinstance(value,str):return False
        text=value.strip()
        if not text or ' ' in text or len(text)>64:return False
        return bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._\-]*',text))

    @staticmethod
    def _extract_epic(position,market):
        """Capital.com usually puts epic under market; symbol is often a display name."""
        for source in (market,position):
            if not isinstance(source,dict):continue
            for key in ('epic','instrumentEpic'):
                value=source.get(key)
                if SingleTradeEngine._looks_like_epic(value):
                    return value.strip()
            symbol=source.get('symbol')
            if SingleTradeEngine._looks_like_epic(symbol):
                return symbol.strip()
        return None

    @staticmethod
    def _extract_instrument_name(position,market):
        for source in (market,position):
            if not isinstance(source,dict):continue
            for key in ('instrumentName','name'):
                value=source.get(key)
                if isinstance(value,str) and value.strip():
                    return value.strip()
            symbol=source.get('symbol')
            # Human labels such as "Gold Spot" help market search when epic is missing.
            if isinstance(symbol,str) and symbol.strip() and not SingleTradeEngine._looks_like_epic(symbol):
                return symbol.strip()
        return None

    def _map_positions(self,data):
        out=[]
        for raw in data or []:
            if not isinstance(raw,dict):continue
            p=raw.get('position') if isinstance(raw.get('position'),dict) else raw
            m=raw.get('market') if isinstance(raw.get('market'),dict) else {}
            direction=self._normalize_direction(p.get('direction'))
            epic=self._extract_epic(p,m)
            out.append({'dealId':p.get('dealId') or p.get('dealID'),
                'epic':epic,
                'instrument_name':self._extract_instrument_name(p,m),
                'size':p.get('size'),'direction':direction,'level':p.get('level'),
                'stopLevel':p.get('stopLevel'),
                'profitLevel':p.get('profitLevel') if p.get('profitLevel') is not None else p.get('limitLevel'),
                'upl':p.get('upl'),'currency':p.get('currency'),'contractSize':p.get('contractSize')})
        return out

    def _supersede_stale_open_orders(self,live_deal_id):
        """Drop journal OPEN rows once the broker already shows a live deal.

        Stale PENDING/UNCERTAIN OPEN (often without a usable confirm reference)
        otherwise keep execution_enabled=False and used to block mandate adoption,
        leaving the UI on WAITING FOR MOBILE TRADE while UPL already showed the trade.
        """
        cleared=0
        for op in list(self.store.unresolved()):
            if op.get('kind')!='OPEN':continue
            self.store.order_update(op['id'],'SUPERSEDED',op.get('reference'))
            cleared+=1
            self.store.event('reconcile','Stale OPEN superseded by live broker position',
                {'order_id':op['id'],'live_deal_id':live_deal_id,
                 'previous_status':op.get('status')})
        if cleared:
            self._unresolved_at_login=bool(self.store.unresolved())
            if (not self._unresolved_at_login and not self.operator_paused and
                not self._paused_for_multi and not self._paused_for_read):
                self.armed=True;self.execution_enabled=True
        return cleared
    @staticmethod
    def _protection_matches(live,payload):
        """Reconcile requested levels, not merely pre-existing non-null stops.

        Trail PUTs are often SL-only (profit=None) while Capital shows Take Profit
        Off — requiring live.profitLevel used to suspend the robot after every trail.
        """
        if not live or live.get('stopLevel') is None:
            return False
        tick=_num(payload.get('tick'),.01) or .01
        tolerance=max(1e-6,.51*tick)
        for requested,actual in ((payload.get('stop'),live.get('stopLevel')),
                                 (payload.get('profit'),live.get('profitLevel'))):
            if requested is None:continue
            a=_num(actual);b=_num(requested)
            if a is None or b is None or abs(a-b)>tolerance:return False
        return True

    async def reconcile_outstanding(self):
        """At session start, settle durable operations via broker data where possible.
        No guesswork for requests without a receipt reference.
        """
        if not self.broker:return False
        outstanding=self.store.unresolved()
        if not outstanding:return True
        positions=self._map_positions(await self.broker.positions())
        for item in outstanding:
            reference=item['reference']
            # A protection update is idempotently verifiable from a fresh broker
            # position read. Do not replay the PUT when its outcome is uncertain.
            if item['kind']=='PROTECT':
                live=next((p for p in positions if p['dealId']==item['deal_id']),None)
                if live is None:
                    self.store.order_update(item['id'],'SUPERSEDED',reference)
                    self.store.event('reconcile','Old protection request belongs to a position no longer open',{'deal_id':item['deal_id']})
                    continue
                try:request=json.loads(item['payload'])
                except (ValueError,TypeError):request={}
                if self._protection_matches(live,request):
                    self.store.order_update(item['id'],'CONFIRMED',reference)
                    self.store.event('reconcile','Protection verified from live broker position',{'deal_id':item['deal_id']})
                    continue
            if not reference:continue
            try:confirmed=await self.broker.confirm(reference)
            except Exception:continue
            status=confirmed.get('dealStatus')
            if status=='REJECTED':
                self.store.order_update(item['id'],'REJECTED',reference)
                self.store.event('reconcile','Previous request confirmed rejected',{'reference':reference})
                continue
            if status!='ACCEPTED':continue
            kind=item['kind']
            try:payload=json.loads(item['payload'])
            except (ValueError,TypeError):payload={}
            matched=(kind=='OPEN' and len(positions)==1 and
                positions[0]['epic']==payload.get('epic') and
                positions[0]['direction']==payload.get('direction') and
                Decimal(str(positions[0]['size']))==Decimal(str(payload.get('size')))) or \
                (kind=='CLOSE' and not any(p['dealId']==item['deal_id'] for p in positions)) or \
                (kind=='PROTECT' and any(p['dealId']==item['deal_id'] and
                                    self._protection_matches(p,payload) for p in positions))
            if matched:
                self.store.order_update(item['id'],'CONFIRMED',reference)
                self.store.event('reconcile','Recovered confirmed broker request',{'kind':kind,'reference':reference})
        self._unresolved_at_login=bool(self.store.unresolved())
        if not self._unresolved_at_login and not self.operator_paused and not self._paused_for_multi:
            self.armed=True;self.execution_enabled=True
        return not self._unresolved_at_login

    async def _fill_missing_epic(self,mapped):
        """If positions lack epic but have a name, resolve via Capital.com search."""
        if len(mapped)!=1:return mapped
        row=mapped[0]
        if row.get('epic') or not row.get('instrument_name'):return mapped
        if not hasattr(self.broker,'search_markets'):return mapped
        try:
            payload=await self.broker.search_markets(str(row['instrument_name']))
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if not isinstance(markets,list):return mapped
            name=str(row['instrument_name']).strip().lower()
            for market in markets:
                if not isinstance(market,dict):continue
                epic=market.get('epic')
                label=str(market.get('instrumentName') or '').strip().lower()
                if epic and (label==name or name in label or label in name):
                    row={**row,'epic':str(epic).strip()}
                    return [row]
            # Fall back to the first searchable epic rather than staying stuck.
            for market in markets:
                epic=market.get('epic') if isinstance(market,dict) else None
                if epic:
                    row={**row,'epic':str(epic).strip()}
                    self.store.event('reconcile','Resolved missing epic via market search',
                        {'instrument_name':row.get('instrument_name'),'epic':row['epic']})
                    return [row]
        except Exception as exc:
            self.store.event('warning','Could not resolve epic for open position',
                             {'type':type(exc).__name__,'instrument_name':row.get('instrument_name')})
        return mapped

    async def sync(self):
        if not self.broker:raise RuntimeError('Not connected')
        latest=self._map_positions(await self.broker.positions())
        latest=await self._fill_missing_epic(latest)
        prev_id=self.last_position_id
        self.positions=latest
        if len(latest)>1:
            if not self._paused_for_multi:self.store.event('warning','Multiple open positions; autonomous execution suspended')
            self.armed=False;self.execution_enabled=False;self._paused_for_multi=True
        elif self._paused_for_multi and len(latest)<=1 and not self.operator_paused and not self.store.unresolved():
            self._paused_for_multi=False;self.armed=True;self.execution_enabled=True
            self.store.event('reconcile','Multiple-position conflict resolved; automatic execution resumed')
        current=latest[0]['dealId'] if len(latest)==1 else None
        if prev_id and prev_id!=current:
            self.last_closed_step=self.brain.core.steps
            self.latest_close_id=prev_id
            exit_guess=_num(self.market.get('mid'))
            # If our CLOSE confirm is in flight, do NOT learn yet — sync() runs
            # before confirm profit/level and would train on mid + clear entry_brain.
            if self.pending_close:
                self.store.close_trade(prev_id,exit_guess,source='pending_close_sync')
                self.store.event('trade','Deal closed pending broker confirm P&L',
                                 {'dealId':prev_id,'exit_guess':exit_guess})
            else:
                # SL/TP/manual vanish: resolve Capital cash P&L — never settle for ticks_only
                # when history/UPL still has account-currency profit.
                if exit_guess is None:
                    # market may be {} after adopt; last UPL row has no level — keep None.
                    snap=self.store.get('last_upl:'+str(prev_id)) or {}
                    exit_guess=_num(snap.get('exit_hint'))
                conf={'level':exit_guess,'source':'position_disappeared'}
                pnl,src,ccy=await self._resolve_broker_pnl(prev_id,conf)
                if pnl is not None:
                    conf['profit']=pnl
                    if ccy:conf['pnl_currency']=ccy
                self._learn_closed_trade(prev_id,conf)
                self.store.event('trade','Previous broker deal no longer open',
                                 {'dealId':prev_id,'pnl':pnl,'pnl_source':src,'pnl_currency':ccy})
        if current:
            self.position_age=(self.position_age+1 if current==prev_id else 0)
            self.store.upsert_trade(latest[0])
            self._snapshot_open_upl(latest[0])
        else:self.position_age=0
        self.last_position_id=current
        # Live open position → this session's instrument/size mandate.
        # After login, mandate is intentionally empty; the first valid broker
        # position must be adopted even if an old robot_open marker remains in
        # SQLite from a previous process (that marker used to leave the UI stuck
        # on WAITING FOR MOBILE TRADE while UPL already showed an open deal).
        if len(latest)==1:
            p=latest[0]
            deal_id=p.get('dealId')
            try: size=Decimal(str(p['size']))
            except (ValueError,TypeError,ArithmeticError): size=Decimal(0)
            valid=bool(deal_id and p.get('epic') and p.get('direction') in ('BUY','SELL')
                       and size.is_finite() and size>0)
            own_order=bool(deal_id and self.store.get('robot_open:'+str(deal_id)))
            open_in_flight=bool(self.pending_order or any(
                op['kind']=='OPEN' for op in self.store.unresolved()))
            already_this_deal=(self.mandate is not None and
                deal_id==self.mandate.get('last_manual_deal_id',self.mandate.get('initial_deal_id')))
            fresh_session=self.mandate is None
            # Fresh session: adopt the single live broker deal even if a stale
            # OPEN journal row remains (that used to keep WAITING FOR MOBILE TRADE
            # while UPL already showed the manual Gold/etc. position).
            # Mid-session: adopt only a NEW non-robot manual deal, and never while
            # this process still has an OPEN in flight.
            should_adopt=(valid and not already_this_deal and
                          (fresh_session or (not open_in_flight and not own_order and
                           deal_id!=self.store.get('latest_robot_deal_id'))))
            if should_adopt:
                old=self.mandate
                self.mandate={'epic':p['epic'],'size':str(p['size']),
                              'initial_deal_id':deal_id,'last_manual_deal_id':deal_id}
                self._seen_initial=True;self.last_closed_step=-1
                self.last_entry_signal_step=-1
                self.last_signal=None;self.last_decision_bar=None
                self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
                self.last_entry_audit_key=None
                if not old or old['epic']!=p['epic']:
                    # Switch model state BEFORE the manual deal can close; old
                    # instrument step counters must never gate a new market.
                    if hasattr(self.brain,'_activate'):
                        self.brain._activate(p['epic'])
                    self.last_candle_tick=-20
                    self.cancel_peer_refresh();self.cancel_calendar_refresh()
                    self.intermarket=IntermarketMonitor()
                    self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
                    self.market_context={'ready':False,'reason':'Waiting for completed candles on newly adopted market'}
                    self.latest_fusion_ready=False
                    self.last_calendar_refresh=0.;self.calendar_feed_error=None
                    self.market={}
                self.store.set('mandate',self.mandate)
                if fresh_session and open_in_flight:
                    self._supersede_stale_open_orders(deal_id)
                self.store.event('adoption','Live broker position adopted for this session',
                    {'deal_id':deal_id,'epic':p['epic'],'size':str(p['size']),
                     'direction':p.get('direction'),
                     'previous_epic':old['epic'] if old else None,
                     'fresh_session':fresh_session,
                     'cleared_stale_open':bool(fresh_session and open_in_flight)})
            elif self.mandate is None and latest:
                # Surface why WAITING FOR MOBILE TRADE persists despite an open deal.
                self.store.event('warning','Open broker position not adopted yet',
                    {'deal_id':deal_id,'epic':p.get('epic'),
                     'instrument_name':p.get('instrument_name'),
                     'direction':p.get('direction'),'size':p.get('size'),
                     'valid':valid,'own_order_marker':own_order,
                     'open_in_flight':open_in_flight})
        # The mandate's size is a cached sizing reference, not evidence of the
        # currently open position. On every broker sync (including restart),
        # refresh it from the actual single open position of the authorized
        # instrument. Never infer a position from the local trade history.
        # A different instrument or ambiguous/multiple positions must still
        # trigger the existing safety stop.
        if self.mandate and len(latest)==1:
            p=latest[0]
            try: live_size=Decimal(str(p['size']))
            except (ValueError, TypeError, ArithmeticError): live_size=Decimal(0)
            if (p['epic']==self.mandate['epic'] and
                p.get('dealId')==self.mandate.get('last_manual_deal_id',self.mandate.get('initial_deal_id')) and
                p.get('direction') in ('BUY','SELL') and live_size.is_finite() and
                live_size>0 and not self.store.unresolved()):
                if Decimal(str(self.mandate['size']))!=live_size:
                    previous=self.mandate['size']
                    self.mandate={**self.mandate,'size':str(p['size'])}
                    self.store.set('mandate',self.mandate)
                    self.store.event('reconcile','Adopted sizing refreshed from live broker position',
                        {'deal_id':p['dealId'],'previous_size':previous,'live_size':str(p['size'])})
        mismatch=False
        if self.mandate and len(latest)==1:
            p=latest[0]
            try:
                live_size=Decimal(str(p['size']))
                mismatch=(p['epic']!=self.mandate['epic'] or
                          not live_size.is_finite() or live_size<=0 or
                          (p.get('dealId')==self.mandate.get('last_manual_deal_id',
                              self.mandate.get('initial_deal_id')) and
                           live_size!=Decimal(str(self.mandate['size']))))
            except (ValueError,TypeError,ArithmeticError):mismatch=True
        if mismatch:
            if not self._paused_for_mismatch:
                self.store.event('warning','Position instrument/size differs from adopted trade; execution suspended')
            self._paused_for_mismatch=True
            self.execution_enabled=False;self.armed=False
        elif self._paused_for_mismatch:
            # Only clear this specific pause when the broker reports a safe
            # position state. Other pauses and unresolved orders remain blocking.
            self._paused_for_mismatch=False
            if (len(latest)<=1 and not self.operator_paused and
                not self._paused_for_multi and not self._paused_for_read and
                not self.store.unresolved() and not self._unresolved_at_login):
                self.armed=True;self.execution_enabled=True
                self.store.event('reconcile','Position mismatch resolved; execution resumed')
        return self.positions
    @staticmethod
    def _min_distance(market,price):
        rules=market.get('dealingRules',{})
        rule=rules.get('minStopOrProfitDistance') or rules.get('minNormalStopOrLimitDistance') or {}
        value=_num(rule.get('value'),0) or 0
        unit=rule.get('unit')
        if unit=='PERCENTAGE':return price*value/100
        if unit=='POINTS':return value
        return None
    @staticmethod
    def _tick(market):
        """Price increment for stopLevel/profitLevel (Capital order grid).

        Prefer snapshot.decimalPlacesFactor — that is the price grid the broker
        accepts on PUT. onePipMeans is a pip/value hint and can disagree (e.g.
        0.025), which produced rejected trail levels like 4006.075.
        """
        inst=market.get('instrument',{});snap=market.get('snapshot',{})
        decimals=snap.get('decimalPlacesFactor')
        if isinstance(decimals,int) and 0<=decimals<=10:return 10.**(-decimals)
        tick=_num(inst.get('onePipMeans'))
        if tick and tick>0:return tick
        return None
    async def _account_and_candles(self):
        if self.tick_count-self.last_account_tick>=10:
            self.last_account_tick=self.tick_count
            try:
                self.account=await self.broker.accounts()
                # Once account currency is known, fix GOLD→USD cash mislabels.
                self.repair_broker_pnl_currencies()
            except Exception as exc:self.store.event('warning','Account refresh unsuccessful',{'type':type(exc).__name__})
        if self.tick_count-self.last_candle_tick>=20 and self.mandate:
            self.last_candle_tick=self.tick_count
            if not hasattr(self.broker,'prices'):return
            try:
                source=await self.broker.prices(self.mandate['epic'],resolution='MINUTE',maximum=180)
                for p in source.get('prices',[]):
                    def midprice(name):
                        item=p.get(name) or {};b=_num(item.get('bid'));a=_num(item.get('ask'))
                        return (a+b)/2 if a is not None and b is not None else None
                    at=candle_bucket(p.get('snapshotTimeUTC') or p.get('snapshotTime'))
                    current_bucket=candle_bucket(now())
                    if at is None or at>=current_bucket:continue
                    close_item=p.get('closePrice') or {}
                    cb=_num(close_item.get('bid'));ca=_num(close_item.get('ask'))
                    bar_spread=(ca-cb) if ca is not None and cb is not None and ca>=cb else None
                    bar={'at':at,
                         'open':midprice('openPrice'),'high':midprice('highPrice'),
                         'low':midprice('lowPrice'),'close':midprice('closePrice'),
                         'spread':bar_spread}
                    if bar['at'] and all(v is not None for k,v in bar.items() if k not in ('at','spread')):
                        self.store.candle(self.mandate['epic'],bar,source='HISTORICAL')
            except Exception as exc:self.store.event('warning','Candle refresh unsuccessful',{'type':type(exc).__name__})
    async def poll(self):
        async with self.lock:
            await self.sync()
            if not self.mandate:return
            # Do not delay managing SL/TP on an OPEN trade for a remote news feed.
            # External feeds gate future NEW entries only.
            if not self.positions:await self._refresh_calendar_if_due()
            broker_market=await self.broker.market(self.mandate['epic'])
            snap=broker_market.get('snapshot',{});inst=broker_market.get('instrument',{})
            bid=_num(snap.get('bid'));ask=_num(snap.get('offer'));tick=self._tick(broker_market)
            streamed=getattr(self,'quote_stream',None)
            streamed=streamed.current(self.mandate['epic']) if streamed else None
            if streamed:bid=streamed['bid'];ask=streamed['ask']
            if bid is None or ask is None or tick is None or ask<bid or bid<=0:return
            mid=(bid+ask)/2;moment=time.monotonic();dt=moment-self.last_time if self.last_time else 1
            self.last_time=moment;self.tick_count+=1
            self.instrument_profile=instrument_profile(self.mandate['epic'],inst)
            self.market={'epic':self.mandate['epic'],'bid':bid,'ask':ask,'mid':mid,'spread':ask-bid,'tick':tick,
                         'time':now(),'status':snap.get('marketStatus'),'min_distance':self._min_distance(broker_market,mid),
                         'instrument_name':inst.get('name') or inst.get('epic') or self.mandate['epic'],
                         'source':'WEBSOCKET' if streamed else 'REST'}
            # Broker protection is handled before slow secondary data calls.
            # Missing SL → install bracket. Missing TP alone is valid (Capital TP Off
            # after trail) — never invent TP every poll or trail cannot run.
            if len(self.positions)==1 and self.positions[0].get('stopLevel') is None:
                await self.protect_position(self.positions[0])
                if len(self.positions)==1 and self.positions[0].get('stopLevel') is None:
                    return

            if self.positions:
                # News download is background-only while an open position needs
                # protection/SLTP management. Initial missing stops were already
                # addressed above, before any secondary feed is scheduled.
                self._schedule_calendar_in_background()
            # Unfinished quote candle is for the chart only, not learning/trading.
            bucket=now()[:16]+':00+00:00'
            existing=self.store.db.execute('SELECT open,high,low FROM candles WHERE epic=? AND at=?',(self.mandate['epic'],bucket)).fetchone()
            bar={'at':bucket,'open':existing[0] if existing else mid,
                 'high':max(mid,existing[1]) if existing else mid,
                 'low':min(mid,existing[2]) if existing else mid,'close':mid,
                 'spread':ask-bid}
            self.store.candle(self.mandate['epic'],bar)
            await self._account_and_candles()
            # Only BROKER-REPORTED complete MINUTE OHLC bars may feed the AI.
            # The forming quote candle is explicitly excluded by market_features.
            candles=self.store.candles(self.mandate['epic'],n=190,historical_only=True)
            self.feature_registry.load()
            if hasattr(self.brain,'apply_registry'):
                self.brain.apply_registry(self.feature_registry)
            self.market_context=market_features(
                candles,spread=ask-bid,epic=self.mandate['epic'],
                approved=self.feature_registry.approved,
                schema=self.feature_registry.ohlc_schema())
            # Reference broker reads are background-only while managing an open
            # position; a slow feed must NOT stall live SL/TP supervision.
            if self.market_context['ready']:
                if self.positions:
                    self._refresh_peers_without_delaying_protection(inst,candles)
                else:
                    await self.intermarket.refresh(self.broker,self.mandate['epic'],inst,candles)
            new_closed_bar=False
            if self.market_context['ready']:
                self.brain.last_regime=self.market_context['trend']
                fused=fusion_features(self.market_context,self.intermarket.state,
                    self.mandate['epic'],self.news_calendar_path,self.instrument_profile['currencies'],
                    now=clock(),expected_ohlc_dim=self.brain.ohlc_dim,
                    fusion_schema=self.brain.fusion_schema)
                self.latest_fusion_ready=fused is not None
                # Prefer fusion when ready. Transient peer/news gaps must NOT wipe
                # the last neural vote (that created permanent HOLD / no-signal).
                # Never invent zero external features — just keep the prior signal.
                if fused is not None:
                    next_signal=self.brain.tick(self.market_context['close'],ask-bid,tick,
                        self.mandate['epic'],dt=60,features=fused,
                        bar_time=self.market_context['bar_time'],external_features=True)
                elif self.brain.feature_schema!=self.brain.fusion_schema:
                    next_signal=self.brain.tick(self.market_context['close'],ask-bid,tick,
                        self.mandate['epic'],dt=60,features=self.market_context['features'],
                        bar_time=self.market_context['bar_time'])
                else:
                    next_signal=None
                if next_signal is not None:
                    next_signal=dict(next_signal,
                        model_version=self.brain.model_version,
                        feature_schema=self.brain.feature_schema,
                        market_bar=self.market_context.get('bar_time'),
                        trend=self.market_context.get('trend'),
                        regime=self.market_context.get('regime'))
                    self.last_signal=next_signal
                    self.last_decision_bar=self.market_context['bar_time']
                    new_closed_bar=True
                    self._run_self_development_cycle(candles,spread=ask-bid)
                self.brain.last_regime=self.market_context['trend']
            else:
                self.latest_fusion_ready=False
                self.last_signal=None  # Never trade stale/incomplete OHLC context.
            # Bind the decision-time neural state (captured in submit) to the deal.
            # Fallback: only if we never captured at entry (e.g. manual adopt).
            if len(self.positions)==1:
                p=self.positions[0];key='entry_brain:'+str(p['dealId'])
                if self.store.get(key) is None and self._pending_entry_brain:
                    pending=self._pending_entry_brain
                    if pending.get('direction')==p.get('direction') and pending.get('epic')==p.get('epic'):
                        self.store.set(key,{**pending,'entry':p.get('level') if p.get('level') is not None else pending.get('entry')})
                        self._pending_entry_brain=None
                elif self.store.get(key) is None and self.brain.core.last_transition:
                    tr=self.brain.core.last_transition
                    if len(tr['h'])==self.brain.core.n:
                        self.store.set(key,{'epic':p['epic'],'direction':p['direction'],
                            'entry':p['level'],'tick_size':tick,'entry_spread':ask-bid,
                            'transition':self.brain._json_transition(tr),
                            'captured':'poll_fallback'})
            if new_closed_bar and self.brain.labeled:
                self.policy.evolve(self.brain.correct/self.brain.labeled,self.brain.labeled,list(self.brain.history))
            if not self.armed or not self.execution_enabled:return
            if len(self.positions)==1:
                p=self.positions[0]
                # Missing SL → install bracket. Missing TP alone must NOT reinstall
                # TP (Capital TP Off after trail is intentional for SL ratchet).
                if p.get('stopLevel') is None:
                    await self.protect_position(p);return
                # Live exit = broker SL / TP / trail only. Neural market-delete
                # was matching M1 noise; the network still learns from cash P&L
                # when those broker exits confirm. Intent is logged, never sent.
                if (new_closed_bar and self.last_signal and
                    self.last_exit_processed_bar!=self.last_decision_bar):
                    self.last_exit_processed_bar=self.last_decision_bar
                    scores=self.last_signal['scores']
                    vote=self.policy.neural_decision(scores)
                    streak_key='exit_opposite_streak:'+str(p['dealId'])
                    if vote in ('BUY','SELL') and vote!=p['direction']:
                        streak=int(self.store.get(streak_key) or 0)+1
                    else:
                        streak=0
                    self.store.set(streak_key,streak)
                    intent=self.policy.evaluate_neural_exit(
                        scores,p['direction'],
                        returns_bps=self.market_context.get('returns_bps'),
                        opposite_streak=streak,store=self.store)
                    if intent=='CLOSE':
                        self.store.event('exit','Neural CLOSE intent ignored — SL/TP/trail only',
                            {'deal_id':p['dealId'],'direction':p['direction'],'vote':vote,
                             'streak':streak,'returns_bps':self.market_context.get('returns_bps'),
                             'neural_market_exit':False})
                await self.adapt_protection(p)
            elif (len(self.positions)==0 and self.last_signal and
                  self.last_closed_step>=0 and new_closed_bar and
                  self.last_entry_processed_bar!=self.last_decision_bar):
                self.last_entry_processed_bar=self.last_decision_bar
                # No hardcoded re-entry cooldown — neural argmax decides each M1 bar.
                evaluation=self._entry_evaluation()
                action=evaluation['action']
                self.last_policy_decision=action
                self.last_entry_reason=self._signal_reason(action,snap.get('marketStatus'),evaluation)
                self.store.event('entry',
                    'Neural entry approved' if action in ('BUY','SELL') else 'Neural HOLD or external execution pause',
                    {'epic':self.mandate['epic'],'bar_time':self.last_decision_bar,
                     'proposal':evaluation.get('proposal'), 'decision':action,
                     'reason':self.last_entry_reason,'model_step':self.brain.core.steps})
                # One entry opportunity per completed M1 bar. The first new
                # confirmed bar after a broker-verified close can already act.
                if (action in ('BUY','SELL') and
                    self.brain.core.steps>max(self.last_closed_step,self.last_entry_signal_step) and
                    snap.get('marketStatus')=='TRADEABLE'):
                    await self.submit(action,broker_market)
    def _spread_bps(self):
        mid=self.market.get('mid') or 0
        return 10000*(self.market.get('spread') or 0)/mid if mid>0 else 0.

    def _adaptive_threshold(self):
        accuracy=self.brain.correct/self.brain.labeled if self.brain.labeled else None
        return self.policy.adaptive_threshold(accuracy,self.brain.labeled,self._spread_bps())

    def _entry_evaluation(self):
        if not self.market_context.get('ready',False):
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'Market context unavailable: '+self.market_context.get('reason','unknown'),
                    'evidence':None}
        if not self.last_signal or not self.mandate or self.brain.epic!=self.mandate['epic']:
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'Waiting for same-instrument neural signal','evidence':None}
        ctx=self.market_context or {}
        result=self.policy.evaluate_neural_entry(
            self.last_signal['scores'],
            returns_bps=ctx.get('returns_bps'),
            trend=ctx.get('trend'),
            regime=ctx.get('regime'))
        if result['action'] in ('BUY','SELL'):
            safety=self._external_entry_check(result['action'])
            if not safety['entry_allowed']:
                return dict(result,action='HOLD',reason=safety['reason'],external_veto=safety['status'])
        return result

    def _signal_reason(self,action,market_status=None,evaluation=None):
        if not self.last_signal:return 'No neural signal'
        evaluation=evaluation or self._entry_evaluation()
        if action=='HOLD':return 'Policy HOLD: '+evaluation['reason']
        if market_status!='TRADEABLE':return 'Broker market not tradeable'
        if self.brain.core.steps<=max(self.last_closed_step,self.last_entry_signal_step):
            return 'Waiting for first completed new candle after previous trade'
        return 'Validated directional proposal; broker order pending or being submitted'

    async def _confirm_reconcile(self,receipt,order_id,kind,deal_id=None):
        ref=receipt.get('dealReference')
        if not ref:raise RuntimeError('No broker deal reference: order result uncertain')
        self.store.order_update(order_id,'PENDING',ref)
        status=None;confirmation={}
        for delay in (0,.4,1.0,2.0):
            if delay:await asyncio.sleep(delay)
            confirmation=await self.broker.confirm(ref)
            status=confirmation.get('dealStatus')
            if status in ('ACCEPTED','REJECTED'):break
        if status=='REJECTED':
            self.store.order_update(order_id,'REJECTED',ref)
            self.store.event('order','Broker rejected order',{'kind':kind,'reference':ref,'reason':confirmation.get('reason')})
            return False
        if status!='ACCEPTED':raise RuntimeError('Broker confirmation not final')
        await self.sync()
        if kind=='OPEN':
            req=json.loads(self.store.db.execute('SELECT payload FROM orders WHERE id=?',(order_id,)).fetchone()[0])
            matched=(len(self.positions)==1 and self.positions[0]['epic']==self.mandate['epic'] and
                     self.positions[0]['direction']==req['direction'] and
                     Decimal(str(self.positions[0]['size']))==Decimal(str(req['size'])))
        elif kind=='CLOSE':matched=not any(p['dealId']==deal_id for p in self.positions)
        else:
            req=json.loads(self.store.db.execute('SELECT payload FROM orders WHERE id=?',(order_id,)).fetchone()[0])
            matched=(len(self.positions)==1 and self.positions[0]['dealId']==deal_id and
                     self._protection_matches(self.positions[0],req))
        if not matched:
            for delay in (.5,1.,2.):
                await asyncio.sleep(delay);await self.sync()
                if kind=='OPEN' and len(self.positions)==1 and self.positions[0]['epic']==self.mandate['epic']:matched=True
                if kind=='CLOSE' and not any(p['dealId']==deal_id for p in self.positions):matched=True
                if kind=='PROTECT' and len(self.positions)==1 and self.positions[0]['dealId']==deal_id and self._protection_matches(self.positions[0],req):matched=True
                if matched:break
        if not matched:raise RuntimeError('Accepted request not reconciled with broker positions')
        self.store.order_update(order_id,'CONFIRMED',ref)
        if kind=='OPEN' and self.positions:
            p=self.positions[0]
            # Persist origin so a later reconnect does not mistake a robot
            # deal for a NEW manual mandate.
            self.store.set('robot_open:'+str(p['dealId']),True)
            self.store.set('latest_robot_deal_id',p['dealId'])
            # Trail enroll needs entry+SL; TP is optional (Capital often TP Off).
            self._enroll_adaptive_protection(p,force=True)
        if kind=='PROTECT' and self.positions:
            p=self.positions[0]
            if p.get('dealId')==deal_id and self.store.get('robot_open:'+str(deal_id)):
                # SL/TP may arrive on a later PUT — enroll trail then if OPEN lacked TP.
                self._enroll_adaptive_protection(p)
        if kind=='CLOSE':
            # Confirm often omits profit on CLOSE; fill from history/UPL before learn.
            pnl,src,_ccy=await self._resolve_broker_pnl(deal_id,confirmation)
            if pnl is not None and _num(confirmation.get('profit')) is None and _num(confirmation.get('pnl')) is None:
                confirmation={**confirmation,'profit':pnl}
            if _ccy and not confirmation.get('pnl_currency'):
                confirmation={**confirmation,'pnl_currency':_ccy}
            self._learn_closed_trade(deal_id,confirmation)
            if src=='ticks_only':
                self.store.event('warning','Close learned without broker cash P&L (ticks_only)',
                                 {'deal_id':deal_id,'reference':ref})
        self.store.event('order','Broker order confirmed and reconciled',{'kind':kind,'reference':ref,'deal_id':confirmation.get('dealId')})
        return True
    def _account_currency(self):
        account=self.account.get('accounts',[]) if isinstance(self.account,dict) else []
        active=next((a for a in account if a.get('preferred') or
                     a.get('accountId')==self.account.get('currentAccountId')),
                    account[0] if account else {})
        cur=(active.get('currency') or '').strip().upper()
        return cur or None

    def _cash_currency(self, hint=None):
        """Currency for broker-settled cash P&L / UPL.

        Always the *account* currency. Capital may tag GOLD positions/activity
        with USD (quote), but confirm.profit / UPL settle in GBP (etc.).
        Never store instrument quote currency on cash rows — if account currency
        is still unknown, return None (repair later) instead of trusting the hint.
        """
        return self._account_currency()

    def repair_broker_pnl_currencies(self):
        """Relabel mis-tagged broker cash rows (e.g. GOLD USD quote → account GBP)."""
        acct=self._account_currency()
        if not acct:return 0
        n=self.store.repair_broker_pnl_currency(acct)
        if n:
            self.store.event('reconcile','Relabeled broker cash P&L to account currency',
                {'account_currency':acct,'rows':n})
        return n

    def _realized_trade_pnl(self,deal_id,entry,exit_level,direction,broker_pnl=None,currency=None):
        """Prefer broker cash P&L in account currency. Never invent price×size cash.

        GOLD mid is USD-quoted; Capital.com confirm.profit / position.upl are in
        the account currency (often GBP). (exit-entry)*size looks like dollars
        and must not be stored as money.
        """
        if broker_pnl is not None and math.isfinite(broker_pnl):
            return float(broker_pnl),'broker',self._cash_currency(currency)
        return None,'ticks_only',None

    def _note_scratch_close(self,pnl_value,deal_id=None):
        """Log repeated penny closes — no hardcoded re-entry ban (neural decides)."""
        if pnl_value is None:return
        try:val=abs(float(pnl_value))
        except (TypeError,ValueError):return
        if val>0.05:
            self.store.set('scratch_streak',0)
            return
        streak=int(self.store.get('scratch_streak') or 0)+1
        self.store.set('scratch_streak',streak)
        if streak<2:return
        self.store.event('entry','Repeated scratch closes (neural still decides next entry)',
                         {'pnl':pnl_value,'deal_id':deal_id,'streak':streak})

    def _snapshot_open_upl(self,position):
        """Keep last broker UPL (account currency) so SL/TP auto-closes are not ticks_only."""
        deal_id=position.get('dealId')
        upl=_num(position.get('upl'))
        if not deal_id or upl is None or not math.isfinite(upl):return
        # Ignore position.currency when it is the instrument quote (GOLD→USD).
        ccy=self._cash_currency(position.get('currency'))
        self.store.set('last_upl:'+str(deal_id),{
            'upl':float(upl),'currency':ccy,'at':now(),
            'exit_hint':_num(position.get('level')) or _num((self.market or {}).get('mid'))})

    async def _pnl_from_broker_history(self,deal_id):
        """Pull closed-trade cash from Capital history/activity (not mid×size)."""
        if not self.broker or not deal_id:return None
        deal=str(deal_id)
        # Activity often names the dealId on SL/TP/USER closes.
        if hasattr(self.broker,'activity'):
            try:
                payload=await self.broker.activity(last_period=1800,detailed=True)
                for row in payload.get('activities') or []:
                    if str(row.get('dealId') or '')!=deal:continue
                    details=row.get('details') if isinstance(row.get('details'),dict) else {}
                    for key in ('profit','pnl','guaranteedStopProfit','realizedPnl'):
                        val=_num(details.get(key) if details else None)
                        if val is None:val=_num(row.get(key))
                        if val is not None and math.isfinite(val):
                            # Cash settles in account currency — ignore quote tags.
                            return float(val),self._cash_currency(row.get('currency'))
            except Exception as exc:
                self.store.event('warning','Activity history P&L lookup failed',
                                 {'type':type(exc).__name__,'deal_id':deal})
        if hasattr(self.broker,'transactions'):
            try:
                payload=await self.broker.transactions(last_period=1800,tx_type='TRADE')
                for row in payload.get('transactions') or []:
                    ref=str(row.get('reference') or row.get('transactionId') or '')
                    if deal not in ref and ref!=deal:continue
                    val=_num(row.get('pnl'))
                    if val is None:val=_num(row.get('cash'))
                    if val is not None and math.isfinite(val):
                        return float(val),self._cash_currency(row.get('currency'))
            except Exception as exc:
                self.store.event('warning','Transaction history P&L lookup failed',
                                 {'type':type(exc).__name__,'deal_id':deal})
        return None

    async def _resolve_broker_pnl(self,deal_id,confirmation=None):
        """Never settle for ticks_only when Capital still has cash P&L somewhere.

        Order: confirm.profit → history/activity → last live UPL snapshot.
        """
        confirmation=confirmation or {}
        ccy=self._cash_currency(confirmation.get('pnl_currency') or confirmation.get('currency'))
        for key in ('profit','pnl'):
            val=_num(confirmation.get(key))
            if val is not None and math.isfinite(val):
                return float(val),'broker',ccy
        # One quick retry — Capital journals can lag slightly after SL/TP fire.
        found=await self._pnl_from_broker_history(deal_id)
        if not found and (hasattr(self.broker,'activity') or hasattr(self.broker,'transactions')):
            await asyncio.sleep(.8)
            found=await self._pnl_from_broker_history(deal_id)
        if found:
            pnl,hist_ccy=found
            return pnl,'broker',self._cash_currency(hist_ccy)
        snap=self.store.get('last_upl:'+str(deal_id)) or {}
        upl=_num(snap.get('upl'))
        if upl is not None and math.isfinite(upl):
            return float(upl),'broker',self._cash_currency(snap.get('currency'))
        return None,'ticks_only',None

    def _learn_closed_trade(self,deal_id,confirmation):
        """Immediate self-evaluation from a closed trade's signed outcome.

        When the trade is a LOSS, the entry-time neural state is trained toward
        the opposite action (regret). Wins reinforce. Cash P&L is stored only
        from broker profit (+ account currency). Without broker profit, learning
        still uses directional ticks — History cash stays blank rather than fake $.
        """
        key='entry_brain:'+str(deal_id)
        saved=self.store.get(key)
        exit_level=_num(confirmation.get('level'))
        broker_pnl=_num(confirmation.get('profit'))
        if broker_pnl is None:broker_pnl=_num(confirmation.get('pnl'))
        hint_ccy=(confirmation.get('pnl_currency') or confirmation.get('currency') or '').strip().upper() or None
        # Allow overwrite when we finally have broker cash after a vanish guess.
        overwrite=(confirmation.get('source')!='position_disappeared') or broker_pnl is not None
        # Even without a saved neural entry state, still record broker cash P&L.
        if not saved:
            row=self.store.db.execute(
                'SELECT entry,direction FROM trades WHERE deal_id=?',(deal_id,)).fetchone()
            if row and exit_level is not None and row[0] is not None and row[1] in ('BUY','SELL'):
                pnl_value,pnl_source,currency=self._realized_trade_pnl(
                    deal_id,row[0],exit_level,row[1],broker_pnl,currency=hint_ccy)
                self.store.close_trade(deal_id,exit_level,pnl_value,currency=currency,
                                       source=pnl_source,overwrite=overwrite)
                if pnl_source=='broker':self._note_scratch_close(pnl_value,deal_id)
            elif exit_level is not None or broker_pnl is not None:
                currency=hint_ccy or (self._account_currency() if broker_pnl is not None else None)
                self.store.close_trade(deal_id,exit_level,broker_pnl,currency=currency,
                                       source='broker' if broker_pnl is not None else None,
                                       overwrite=overwrite)
                if broker_pnl is not None:self._note_scratch_close(broker_pnl,deal_id)
            return
        epic=saved.get('epic')
        entry=_num(saved.get('entry'))
        tick=_num(saved.get('tick_size'))
        direction=saved.get('direction')
        # market{} is cleared on adopt; fall back to mandate/brain so SL/TP vanishes
        # still record broker cash instead of ticks_only.
        live_epic=(self.market.get('epic') or (self.mandate or {}).get('epic') or
                   getattr(self.brain,'epic',None))
        if exit_level is None:
            exit_level=_num((self.market or {}).get('mid')) or _num(saved.get('entry'))
        mismatched=(not epic or epic!=self.brain.epic or
            (live_epic and epic!=live_epic) or
            entry is None or not tick or tick<=0 or
            direction not in ('BUY','SELL') or not saved.get('transition'))
        if mismatched:
            if direction in ('BUY','SELL') and (broker_pnl is not None or exit_level is not None):
                pnl_value,pnl_source,currency=self._realized_trade_pnl(
                    deal_id,entry,exit_level or entry,direction,broker_pnl,currency=hint_ccy)
                self.store.close_trade(deal_id,exit_level,pnl_value,currency=currency,
                                       source=pnl_source,overwrite=overwrite)
                if pnl_source=='broker':self._note_scratch_close(pnl_value,deal_id)
            self.store.event('learning','Trade outcome skipped: missing/mismatched entry units or instrument',
                {'deal_id':deal_id,'entry_epic':epic,'model_epic':self.brain.epic,
                 'live_epic':live_epic,'broker_pnl':broker_pnl})
            self.store.set(key,None)
            return
        sign=1 if direction=='BUY' else -1
        realized_ticks=sign*(exit_level-entry)/tick if exit_level is not None else 0.
        pnl_value,pnl_source,currency=self._realized_trade_pnl(
            deal_id,entry,exit_level,direction,broker_pnl,currency=hint_ccy)
        if broker_pnl is not None and math.isfinite(broker_pnl):
            pnl_sign=1 if broker_pnl>0 else (-1 if broker_pnl<0 else 0)
        else:
            pnl_sign=1 if realized_ticks>0.5 else (-1 if realized_ticks<-0.5 else 0)
        # Profit-centred labels — no entry cooldown. Flip-after-loss → HOLD
        # (never auto-train opposite churn). Other losses use counterfactual HOLD
        # preference via profit_learning before optional opposite regret.
        prev_out=self.store.get('last_trade_outcome') or {}
        flip_after_loss=bool(
            pnl_sign<0 and isinstance(prev_out,dict) and
            int(prev_out.get('pnl_sign') or 0)<0 and
            prev_out.get('direction') in ('BUY','SELL') and
            prev_out.get('direction')!=direction)
        spread_cost=_num(saved.get('entry_spread')) or 0.
        shadow_action=None
        if self.shadow_brain is not None and saved.get('shadow_scores'):
            sc=saved['shadow_scores']
            try:
                shadow_action=max(('BUY','SELL','HOLD'),key=lambda a:float(sc.get(a,0)))
            except (TypeError,ValueError):
                shadow_action=None
        # Profit ledger: ONLY broker-confirmed £ — never ±1 tick fakes.
        if pnl_source=='broker' and pnl_value is not None:
            decision=self.profit_learning.record_closed(
                deal_id=deal_id,epic=epic,direction=direction,entry=entry,
                exit_level=exit_level,broker_pnl=pnl_value,
                currency=currency,spread=spread_cost,flip_after_loss=flip_after_loss,
                model_version=getattr(self.brain,'model_version',0),
                feature_schema=getattr(self.brain,'feature_schema',None),
                shadow_action=shadow_action,require_broker_pnl=True)
            label=decision['label'];mode=decision['mode']
            if label is not None:label=int(label)
        else:
            # Tick path: same rule as £ — wins reinforce side; else counterfactual.
            from .profit_learning import best_action_from_counterfactual
            sim=best_action_from_counterfactual(entry,exit_level,spread=spread_cost)
            if pnl_sign>0 and direction in ('BUY','SELL'):
                label=0 if direction=='BUY' else 1;mode='reinforce_win_ticks'
            elif sim and sim['best'] in ('BUY','SELL','HOLD'):
                label={'BUY':0,'SELL':1,'HOLD':2}[sim['best']]
                prefix=('flip_loss_counterfactual_' if flip_after_loss
                        else ('loss_counterfactual_' if pnl_sign<0 else 'flat_counterfactual_'))
                mode=prefix+sim['best'].lower()+'_ticks'
            else:
                label,mode=None,'ticks_no_counterfactual'
            self.profit_learning.record_closed(
                deal_id=deal_id,epic=epic,direction=direction,entry=entry,
                exit_level=exit_level,broker_pnl=None,
                currency=currency,spread=spread_cost,flip_after_loss=flip_after_loss,
                model_version=getattr(self.brain,'model_version',0),
                feature_schema=getattr(self.brain,'feature_schema',None),
                shadow_action=shadow_action,require_broker_pnl=True)
        from .self_params import load as load_self_params, evolve_from_feedback
        sp=load_self_params(self.store)
        if label is None:
            repeats=0
        elif mode in ('reinforce_win','reinforce_win_ticks'):
            repeats=int(sp['learn_win_repeats'])
        elif 'flat_counterfactual' in str(mode) or mode in ('flat_hold',):
            repeats=1
        else:
            repeats=int(sp['learn_loss_repeats'])
        # Penny-loss closes must not spam any class into the core (HOLD trap).
        # Exact £0 / flat broker rows still use the light counterfactual path —
        # Capital often snapshots UPL=0 at close while ticks still moved.
        scratch_hold=False
        if pnl_value is not None:
            try:
                cash=float(pnl_value)
                scratch_hold=(-0.05<=cash<0.)
            except (TypeError,ValueError):
                scratch_hold=False
        if scratch_hold:
            repeats=0
            mode=str(mode)+'_scratch_skip'
        learn_lr=float(sp['learn_lr'])
        transition=saved['transition']
        losses=[]
        for _ in range(repeats):
            step_loss=self.brain.core.learn(transition,label,lr=learn_lr)
            if step_loss is not None:losses.append(step_loss)
        # Independent shadow core trains on the same delayed label (side-by-side).
        if repeats>0:
            self._shadow_learn(transition,label,lr=learn_lr)
        if pnl_source=='broker' and pnl_value is not None:
            # Queue evidence only — commit when long-horizon £ proves improvement.
            evolve_from_feedback(self.store,broker_pnl=pnl_value,commit=False)
            from .self_params import maybe_commit_long_horizon
            maybe_commit_long_horizon(self.store,self.profit_learning.broker_pnls())
        if repeats>0 and transition.get('h') is not None and transition.get('c') is not None:
            self.brain.core.update_utility(transition['h'],transition['c'],label,
                neuron_ids=transition.get('neuron_ids'))
        self.brain.labeled+=1
        self.brain.correct+=int(pnl_sign>0)
        self.store.close_trade(deal_id,exit_level,pnl_value,currency=currency,
                               source=pnl_source,overwrite=overwrite)
        self.store.set('last_trade_outcome',{
            'direction':direction,'pnl_sign':int(pnl_sign),'deal_id':deal_id,
            'pnl':pnl_value,'mode':mode})
        # Joint system: cash P&L also teaches SL/TP shape (lot+market stay the risk frame).
        if pnl_value is not None and pnl_source=='broker':
            bk=saved.get('bracket_kappa');bs=saved.get('bracket_sl_scale')
            if bk is None or bs is None:
                cur=bracket_learn.load_params(self.store);bk,bs=cur['kappa'],cur['sl_scale']
            bracket_learn.record_outcome(self.store,kappa=bk,sl_scale=bs,pnl=pnl_value,
                                         currency=currency,deal_id=deal_id)
            promoted=bracket_learn.evolve_from_history(self.store)
            if promoted:
                self.store.event('bracket','SL/TP shape promoted from cash P&L history',promoted)
                learning_generation.bump(self.store,'bracket_shape_promoted')
            self._note_scratch_close(pnl_value,deal_id)
        if repeats>0 and self.brain.core.last_z is not None:
            grow_th=float(sp['grow_error_threshold'])
            if self.brain.core.grow_if_needed(np.asarray(self.brain.core.last_z),threshold=grow_th,nmax=None):
                self.store.event('neural','Neuron added after closed-trade learning',
                                 {'neurons':self.brain.core.n,'deal_id':deal_id})
        self.brain.save()
        self.store.event('learning','Closed-trade P&L self-evaluation updated the neural entry state',
            {'deal_id':deal_id,'epic':epic,'direction':direction,'mode':mode,
             'pnl':pnl_value,'pnl_source':pnl_source,'pnl_currency':currency,
             'broker_pnl':broker_pnl,
             'realized_directional_ticks':realized_ticks,'target':label,
             'learn_repeats':repeats,'scratch_hold_skip':scratch_hold,
             'last_loss':losses[-1] if losses else None,
             'tick_size':tick,
             'bracket':bracket_learn.load_params(self.store)})
        try:
            # Milestone evolve only — force=True every close inflated policy
            # attempts/Paaudze without changing LIVE neural entry.
            self.policy.evolve(self.brain.correct/max(1,self.brain.labeled),
                               self.brain.labeled,list(self.brain.history),
                               force=False)
        except Exception as exc:
            self.store.event('warning','Trade-timed evolution skipped',
                             {'type':type(exc).__name__,'deal_id':deal_id})
        self.store.set(key,None)

    def _enroll_adaptive_protection(self,p,*,force=False):
        """Register robot deal for broker trail. Needs entry+SL; TP optional.

        Capital often confirms OPEN with stopLevel set and Take Profit Off —
        requiring TP here used to skip enrollment entirely, so trail never ran.
        """
        deal_id=p.get('dealId')
        if not deal_id:return False
        key='adaptive_protection:'+str(deal_id)
        existing=self.store.get(key)
        if existing and existing.get('owner')=='AUTO' and not force:
            # Refresh missing original_profit if TP appeared later.
            tp=_num(p.get('profitLevel'));entry=_num(p.get('level'))
            if tp is not None and entry is not None and not existing.get('original_profit'):
                sign=1 if p.get('direction')=='BUY' else -1
                if sign*(tp-entry)>0:
                    meta=dict(existing);meta['original_profit']=abs(tp-entry)
                    self.store.set(key,meta)
            return True
        if not self.store.get('robot_open:'+str(deal_id)):
            return False
        entry=_num(p.get('level'));stop=_num(p.get('stopLevel'));tp=_num(p.get('profitLevel'))
        direction=p.get('direction');sign={'BUY':1,'SELL':-1}.get(direction)
        if entry is None or stop is None or sign is None:return False
        if sign*(entry-stop)<=0:return False
        meta={
            'owner':'AUTO','deal_id':deal_id,'epic':p.get('epic'),
            'original_risk':abs(entry-stop),
            'original_profit':(abs(tp-entry) if tp is not None and sign*(tp-entry)>0 else None),
            'opened':(existing or {}).get('opened') or now()}
        self.store.set(key,meta)
        self.store.event('protection','Robot-opened position enrolled in adaptive SL/TP',
            {'deal_id':deal_id,'initial_risk':meta['original_risk'],
             'initial_profit':meta['original_profit'],'tp_optional':tp is None})
        return True

    async def adapt_protection(self,p):
        """Trail SL behind broker extreme — ROBOT only; continues past BE.

        Pre-BE gap = original_risk; after BE gap = recent M1 median range from
        OHLC (same zone data), not a hardcoded fraction of R.
        """
        key='adaptive_protection:'+str(p['dealId'])
        # Late enroll if OPEN lacked TP / meta was never written.
        if self.store.get('robot_open:'+str(p.get('dealId'))):
            self._enroll_adaptive_protection(p)
        meta=self.store.get(key)
        if not meta or meta.get('owner')!='AUTO':
            self.protection_brief={'status':'Manual position or earlier bot order: existing SL/TP left unchanged'}
            return
        if self.store.unresolved() or self.pending_order or self.pending_close:return
        if self.market.get('status')!='TRADEABLE':return
        if self.market.get('tick') is None or self.market.get('min_distance') is None:return
        opened=candle_time(meta.get('opened')) if meta.get('opened') else None
        candles=self.store.candles(p['epic'],n=120,historical_only=True)
        zone_range=ohlc_range_distance(candles)  # lookback from data
        extreme=trail_extreme_from_broker(
            p['direction'],self.market.get('bid'),self.market.get('ask'),
            candles=candles,opened_at=opened,prior_extreme=meta.get('trail_extreme'))
        if extreme is not None or zone_range is not None:
            meta=dict(meta);meta['trail_extreme']=extreme
            if zone_range is not None:meta['zone_range']=float(zone_range)
            self.store.set(key,meta)
        mark=self.market.get('time') or self.market_context.get('bar_time')
        proposal=propose_broker_trail(p,self.market,meta,extreme=extreme,mark_time=mark,
                                      zone_range=float(zone_range) if zone_range is not None else None)
        self.protection_brief={'status':'Broker trail proposal' if proposal else 'No broker-native trail improvement',
                               'deal_id':p['dealId'],'extreme':extreme,'proposal':proposal}
        if not proposal:return
        runtime=self.store.get('adaptive_runtime:'+str(p['dealId']),{}) or {}
        now_mono=time.monotonic()
        wait=15.0 if runtime.get('status')=='REJECTED' else 3.0
        last_mono=runtime.get('mono')
        if last_mono is not None and (now_mono-float(last_mono))<wait:return
        stop=proposal['stop_level'];tp=proposal.get('profit_level')
        oid=self.store.order('PROTECT',p['dealId'],{'stop':stop,'profit':tp,
            'tick':self.market['tick'],'reason':'BROKER_TRAIL','extreme':extreme,
            'min_distance':self.market.get('min_distance')})
        try:
            receipt=await self.broker.update_position(p['dealId'],stop_level=stop,profit_level=tp)
            if await self._confirm_reconcile(receipt,oid,'PROTECT',p['dealId']):
                self.store.set('adaptive_runtime:'+str(p['dealId']),{
                    'mono':now_mono,'stop':stop,'profit':tp,'extreme':extreme,'status':'CONFIRMED',
                    'mark':mark})
                self.protection_brief['status']='Broker-confirmed trail update'
                self.store.event('protection','Trail SL verified by broker',{
                    'deal_id':p['dealId'],'new_stop':stop,'extreme':extreme,
                    'min_distance':self.market.get('min_distance'),'tick':self.market.get('tick')})
            else:
                self.protection_brief['status']='Broker rejected trail; old levels retained'
                self.store.set('adaptive_runtime:'+str(p['dealId']),{
                    'mono':now_mono,'status':'REJECTED','mark':mark})
                self.store.event('warning','Trail SL rejected; original levels retained',
                                 {'deal_id':p['dealId'],'stop':stop,'extreme':extreme,
                                  'min_distance':self.market.get('min_distance'),'tick':self.market.get('tick')})
        except Exception as exc:
            self.store.order_update(oid,'UNCERTAIN')
            self.armed=False;self.execution_enabled=False
            self.protection_brief['status']='Uncertain broker PUT; automatic execution suspended'
            self.store.event('error','Trail protection outcome uncertain; suspended for reconciliation',{
                'deal_id':p['dealId'],'type':type(exc).__name__,
                'broker_status':getattr(getattr(exc,'response',None),'status_code',None),
                'broker_error':_safe_broker_error(exc)})

    async def protect_position(self,p):
        if self.pending_order or self.pending_close:return
        if p.get('stopLevel') is not None and p.get('profitLevel') is not None:
            self.protected.add(p['dealId']);return
        if self.store.unresolved():return
        min_dist=self.market.get('min_distance');tick=self.market.get('tick');mid=self.market.get('mid')
        if min_dist is None or not tick or not mid:
            self.store.event('warning','Protection not possible: unsupported broker distance/price rule')
            self.execution_enabled=False;return
        candles=self.store.candles(p.get('epic') or self.mandate.get('epic'),n=120,
                                   historical_only=True)
        zone=bracket_learn.zone_at_open(candles)
        rng=zone['range_distance'] or ohlc_range_distance(candles)
        spread=float(self.market.get('spread') or 0)
        buffer=max(2*float(tick),2*spread)
        base_sl=max(float(min_dist)+buffer,float(rng or 0))
        bp=bracket_learn.propose_for_open(self.store,ohlc_kappa=zone.get('kappa'),
                                         base_sl=base_sl,max_tp=zone.get('max_tp'),
                                         candles=candles)
        result=stop_take(p['level'] or mid,p['direction'],tick,self.market['spread'],min_dist,
                         kappa=bp['kappa'],sl_scale=bp['sl_scale'],
                         range_distance=float(rng) if rng is not None else 0)
        stop=result['stop_level'] if p.get('stopLevel') is None else None
        profit=result['profit_level'] if p.get('profitLevel') is None else None
        order_id=self.store.order('PROTECT',p['dealId'],{'stop':stop,'profit':profit,'tick':tick})
        try:
            receipt=await self.broker.update_position(p['dealId'],stop_level=stop,profit_level=profit)
            if await self._confirm_reconcile(receipt,order_id,'PROTECT',p['dealId']):
                self.protected.add(p['dealId'])
            else:
                self.armed=False;self.execution_enabled=False
                self.store.event('error','Protection rejected or could not be reconciled',{'deal_id':p.get('dealId'),'reference':receipt.get('dealReference'),'requested_stop':str(stop),'requested_profit':str(profit)})
        except Exception as exc:
            self.store.order_update(order_id,'UNCERTAIN')
            self.armed=False;self.execution_enabled=False
            self.store.event('error','Position protection uncertain; trading suspended',{'type':type(exc).__name__, 'broker_status':getattr(getattr(exc,'response',None),'status_code',None), 'broker_error':_safe_broker_error(exc), 'deal_id':p.get('dealId'), 'stop_level':str(stop), 'take_profit_level':str(profit)})
    async def exit_position(self,deal_id):
        if self.pending_order or self.pending_close or not self.execution_enabled:return
        self.pending_close=True
        oid=self.store.order('CLOSE',deal_id,{'deal_id':deal_id})
        try:
            receipt=await self.broker.close_position(deal_id)
            if not await self._confirm_reconcile(receipt,oid,'CLOSE',deal_id):
                self.armed=False;self.execution_enabled=False
        except Exception as exc:
            self.store.order_update(oid,'UNCERTAIN')
            self.armed=False;self.execution_enabled=False
            self.store.event('error','Exit outcome uncertain, execution suspended',{'type':type(exc).__name__})
        finally:self.pending_close=False
    async def submit(self,action,broker_market=None):
        if self.pending_order or self.pending_close or self.positions or not self.armed or not self.execution_enabled or not self.mandate:return
        if action not in ('BUY','SELL') or self.last_closed_step<0:return
        # Never submit when the learned market context is stale/incomplete.
        if not self.market_context.get('ready'):return
        if not self._external_entry_check(action)['entry_allowed']:return
        self.pending_order=True
        try:
            await self.sync()
            if self.positions or not self._external_entry_check(action)['entry_allowed']:return
            market=broker_market or await self.broker.market(self.mandate['epic'])
            snap=market.get('snapshot',{});tick=self._tick(market)
            mid=self.market.get('mid');spread=self.market.get('spread')
            if snap.get('marketStatus') not in (None,'TRADEABLE') or not tick or mid is None or spread is None:return
            minimum=self._min_distance(market,mid)
            if minimum is None:
                self.store.event('warning','Unrecognized instrument SL/TP distance; skipped order')
                return
            size=Decimal(self.mandate['size'])
            if size<=0:return
            entry=self.market.get('ask') if action=='BUY' else self.market.get('bid')
            # SL/TP computed from the same M1 OHLC the neural net already sees.
            candles=self.store.candles(self.mandate['epic'],n=120,historical_only=True)
            zone=bracket_learn.zone_at_open(candles)
            rng=zone['range_distance'] or ohlc_range_distance(candles)
            buffer=max(2*float(tick),2*float(spread))
            base_sl=max(float(minimum)+buffer,float(rng or 0))
            bp=bracket_learn.propose_for_open(self.store,ohlc_kappa=zone.get('kappa'),
                                             base_sl=base_sl,max_tp=zone.get('max_tp'),
                                             candles=candles)
            brackets=stop_take(entry,action,tick,spread,minimum,
                               kappa=bp['kappa'],sl_scale=bp['sl_scale'],
                               range_distance=float(rng) if rng is not None else 0)
            self.store.event('bracket','OHLC zone × learned raise/cut at open',{
                'kappa':bp['kappa'],'sl_scale':bp['sl_scale'],
                'kappa_mult':bp.get('kappa_mult'),'sl_mult':bp.get('sl_mult'),
                'median_up':bp.get('median_up'),'median_down':bp.get('median_down'),
                'lookback':bp.get('lookback') or zone.get('lookback'),
                'base_sl':base_sl,'max_tp':zone.get('max_tp'),
                'stop_distance':float(brackets['stop_distance']),
                'profit_distance':float(brackets['profit_distance']),
                'learn_samples':bp.get('nudge_samples')})
            # Snapshot the neural state that voted for THIS entry — not a later bar.
            if self.brain.core.last_transition and len(self.brain.core.last_transition['h'])==self.brain.core.n:
                self._pending_entry_brain={
                    'epic':self.mandate['epic'],'direction':action,'entry':entry,
                    'tick_size':tick,'entry_spread':spread,
                    'transition':self.brain._json_transition(self.brain.core.last_transition),
                    'scores':(self.last_signal or {}).get('scores'),
                    'shadow_scores':self._shadow_scores_now(),
                    'bar_time':self.last_decision_bar,'captured':'submit',
                    'bracket_kappa':bp['kappa'],'bracket_sl_scale':bp['sl_scale'],
                    'bracket_explored':bool(bp.get('explored')),
                }
            # Durable order record BEFORE a real order is sent; never blindly retry.
            oid=self.store.order('OPEN',None,{'epic':self.mandate['epic'],'direction':action,'size':str(size),
                'stop_distance':str(brackets['stop_distance']),'profit_distance':str(brackets['profit_distance']),
                'kappa':bp['kappa'],'sl_scale':bp['sl_scale']})
            self.last_entry_signal_step=self.brain.core.steps
            try:
                receipt=await self.broker.open_position(self.mandate['epic'],action,size,
                    stop_distance=brackets['stop_distance'],profit_distance=brackets['profit_distance'])
                self.last_order_ref=receipt.get('dealReference')
                await self._confirm_reconcile(receipt,oid,'OPEN')
                if self.positions and self._pending_entry_brain:
                    deal_id=self.positions[0].get('dealId')
                    key='entry_brain:'+str(deal_id)
                    if deal_id and self.store.get(key) is None:
                        pending=self._pending_entry_brain
                        self.store.set(key,{**pending,
                            'entry':self.positions[0].get('level') if self.positions[0].get('level') is not None else pending.get('entry')})
                    self._pending_entry_brain=None
            except Exception as exc:
                self.store.order_update(oid,'UNCERTAIN',self.last_order_ref)
                self.execution_enabled=False;self.armed=False
                self._pending_entry_brain=None
                self.store.event('error','Entry outcome uncertain; no automatic retry',{'type':type(exc).__name__})
        finally:self.pending_order=False
    def snapshot(self):
        account=self.account.get('accounts',[]) if isinstance(self.account,dict) else []
        active=next((a for a in account if a.get('preferred') or a.get('accountId')==self.account.get('currentAccountId')),account[0] if account else {})
        evaluation=self._entry_evaluation() if not self.positions else None
        news=self._news_risk()
        intermarket=self.intermarket.state
        decision=(self.policy.neural_decision(self.last_signal['scores'],self.positions[0]['direction'])
            if self.last_signal else 'HOLD') if self.positions else evaluation['action']
        return {'connected':bool(self.broker),'armed':self.armed,'execution_enabled':self.execution_enabled,
                'mandate':self.mandate,'data_dir':str(self.store.path.parent),
                'positions':self.positions,'market':self.market,'signal':self.last_signal,
                'neural':dict(self.brain.core.snapshot(),feature_schema=getattr(self.brain,'feature_schema',None),
                    input_dimension=self.brain.core.d,
                    external_features_learned=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                        and getattr(self.brain,'fusion_schema',None) is not None),
                    external_feed_ready=self.latest_fusion_ready,
                    external_trained_labels=getattr(self.brain,'fusion_labels',0),
                    model_version=getattr(self.brain,'model_version',0),
                    ohlc_dim=getattr(self.brain,'ohlc_dim',None),
                    fusion_dim=getattr(self.brain,'fusion_dim',None),
                    feature_registry=self.feature_registry.snapshot()),
                'accuracy_proxy':self.brain.correct/max(1,self.brain.labeled),
                'labeled_samples':self.brain.labeled,'market_regime':self.brain.last_regime,
                'market_context':self.market_context,'adaptive_protection':self.protection_brief,
                'economic_news_risk':news,'intermarket_analysis':intermarket,
                'instrument_profile':self.instrument_profile,
                'account':{'accountId':active.get('accountId'),'currency':active.get('currency'),
                           'balance':active.get('balance',{})},'error':self.last_error,
                'pending_order':self.pending_order,'pending_close':self.pending_close,
                'unresolved_orders':self.store.unresolved(),
                # Paaudze = real brain advances (features/weights/brackets/params),
                # not only shadow Python policy file swaps (LIVE uses neural vote).
                'code_generation':learning_generation.display(self.store,self.policy.generation),
                'learning_generation':learning_generation.current(self.store),
                'policy_generation':self.policy.generation,
                'position_age':self.position_age,'last_order_reference':self.last_order_ref,
                'tick_count':self.tick_count,
                'policy_decision':decision,
                'policy_proposal':evaluation['proposal'] if evaluation else decision,
                'directional_advantage':self.policy.score_edge(self.last_signal['scores']) if self.last_signal else 0.,
                'directional_validation':evaluation.get('evidence') if evaluation else None,
                'adaptive_threshold':None,
                'policy_base_threshold':None,
                'entry_margin_calibration':evaluation.get('calibration') if evaluation else None,
                'threshold_unit':'neural_argmax_no_zone_hardcode',
                'neural_decision_bar':self.last_decision_bar,
                'neural_entry_rule':'neural argmax BUY/SELL/HOLD — zone/SHIFT evidence only',
                'neural_market_exit':False,
                'neural_exit_rule':'disabled — broker SL/TP/trail closes; neural learns from cash P&L',
                'entry_eligible':bool(self.market_context.get('ready') and self.mandate and not self.positions and self.last_closed_step>=0 and self.armed and self.execution_enabled and not self.store.unresolved()),
                'entry_signal_validated':bool(evaluation and evaluation.get('action') in ('BUY','SELL')),
                'entry_decision_pending_new_bar':bool(self.last_decision_bar==self.last_entry_processed_bar),
                'entry_policy_reason':evaluation.get('reason') if evaluation else None,
                'entry_independent_samples':(evaluation.get('evidence') or {}).get('samples') if evaluation else None,
                'entry_can_submit_now':bool(evaluation and evaluation.get('action') in ('BUY','SELL') and
                    self.market.get('status')=='TRADEABLE' and self.market_context.get('ready') and
                    self.mandate and not self.positions and self.last_closed_step>=0 and self.armed and
                    self.execution_enabled and not self.store.unresolved() and
                    self._external_entry_check(evaluation['action'])['entry_allowed'] and
                    self.brain.core.steps>max(self.last_closed_step,self.last_entry_signal_step) and
                    self.last_decision_bar is not None and
                    self.last_entry_processed_bar!=self.last_decision_bar),
                'entry_block_reason':('Execution paused' if not self.armed or not self.execution_enabled else
                    'Unresolved broker operation' if self.store.unresolved() else
                    'Existing open position' if self.positions else
                    'No adoptive size this session — open a new manual trade' if not self.mandate else
                    'Waiting for first manually opened trade to close' if self.last_closed_step<0 else
                    'Market context unavailable: '+self.market_context.get('reason','') if not self.market_context.get('ready') else
                    'Waiting for next completed M1 candle' if self.last_entry_processed_bar==self.last_decision_bar and self.last_decision_bar is not None else
                    self._signal_reason(decision,self.market.get('status'),evaluation) if self.last_signal else
                    'Waiting for aligned external neural inputs' if (
                        getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                        and getattr(self.brain,'fusion_schema',None) is not None) else
                    'Waiting for signal'),
                'self_development':{
                    'registry':self.feature_registry.snapshot(),
                    'profit':self.profit_learning.summary(),
                    'model_stack':len(self.model_validator.stack),
                    'shadow_active':self.shadow_brain is not None,
                    'shadow_paper':self.shadow_paper.summary(),
                    'self_params':__import__('app.self_params',fromlist=['snapshot']).snapshot(self.store)},
                'bracket':bracket_learn.load_params(self.store),
                'bracket_learn_reason':self.store.get('bracket_learn_reason'),
                'bracket_outcomes':len(self.store.get('bracket_outcome_history',[]) or [])}

    def _bars_and_spreads_from_candles(self,candles):
        """Chronological OHLC + per-bar spreads (None when unknown — never invent)."""
        bars=[];spreads=[];times=[]
        for raw in sorted(candles or [],key=lambda r:r.get('at') or ''):
            try:
                bars.append(tuple(float(raw[k]) for k in ('open','high','low','close')))
            except (TypeError,ValueError,KeyError):
                continue
            times.append(raw.get('at'))
            sp=_num(raw.get('spread'))
            spreads.append(float(sp) if sp is not None and sp>=0 else None)
        return bars,spreads,times

    def _ensure_shadow_brain(self):
        """Independent shadow NeuralCore — same input dim, own weights (never LIVE)."""
        d=self.brain.core.d
        if self.shadow_brain is None or self.shadow_brain.d!=d:
            alt_n=max(8,self.brain.core.n+4 if self.brain.core.n<48 else max(8,self.brain.core.n-4))
            self.shadow_brain=self.brain.core.clone_architecture(
                n=alt_n,seed=11+alt_n,copy_weights=False)
            self.store.event('shadow','Independent shadow neural core ready',
                {'neurons':self.shadow_brain.n,'dim':d,'live_neurons':self.brain.core.n})
        return self.shadow_brain

    def _shadow_learn(self,transition,label,lr=None):
        shadow=self._ensure_shadow_brain()
        if transition is None or label not in (0,1,2):return
        z=transition.get('z')
        if z is None or len(np.asarray(z))!=shadow.d:return
        shadow.learn(transition,label,lr=lr)

    def _shadow_scores_now(self):
        """One forward pass on live last_z for shadow journal (no LIVE order)."""
        shadow=self._ensure_shadow_brain()
        z=self.brain.core.last_z
        if z is None or len(z)!=shadow.d:return None
        try:
            # Use shadow's own h/c — step advances shadow state on closed bars only
            # via learn; here we score without mutating by using _forward snapshot.
            hn,cn,logits=shadow._forward(
                np.asarray(z,dtype=float),shadow.h,shadow.c,1.0)
            prob=shadow._softmax(logits)
            return dict(zip(('BUY','SELL','HOLD'),map(float,prob)))
        except Exception:
            return None

    def _run_self_development_cycle(self,candles,*,spread=0.):
        """Discover → validate → expand (decision-time data only). Never blocks orders."""
        from .historical_features import build_feature_matrix
        bar=self.market_context.get('bar_time')
        if not bar or bar==self._last_self_dev_bar:return
        self._last_self_dev_bar=bar
        bars,spreads,times=self._bars_and_spreads_from_candles(candles)
        if len(bars)<120:return
        window=bars[-61:]
        # Discovery uses the latest known per-bar spread when present.
        last_sp=next((s for s in reversed(spreads) if s is not None),None)
        if last_sp is None:last_sp=float(spread) if spread is not None else 0.
        proposals=self.feature_discovery.on_closed_bar(
            bar,window,spread=last_sp,registry=self.feature_registry)
        self._ensure_shadow_brain()
        # Independent shadow paper step on every closed ready bar (not LIVE remap).
        self._shadow_paper_step(mid=self.market_context.get('close'),
                                spread=last_sp,bar_time=bar)
        if not proposals:
            self.policy.shadow_self_dev_tick(self)
            return
        # Real historical feature matrix — never tile one live snapshot.
        existing,names,indices=build_feature_matrix(
            bars,spreads,approved=self.feature_registry.approved)
        # Validation refuses a single live spread pasted on all history.
        approved_any=False
        for prop in proposals[:6]:
            result=self.feature_validator.validate_and_maybe_approve(
                prop['id'],bars,spreads=spreads,times=times,
                existing_matrix=existing,existing_names=names,
                existing_indices=indices)
            if result.get('approved'):
                approved_any=True
        if approved_any:
            # Snapshot *current* weights onto the stack before expanding.
            pre_path=None
            if hasattr(self.brain,'backup_weights'):
                pre_path=self.brain.backup_weights(
                    tag=f'v{int(self.brain.model_version)}')
            pre_specs=[dict(x) for x in self.feature_registry.approved]
            self.model_validator.push_version({
                'model_version':self.brain.model_version,
                'feature_schema':self.brain.feature_schema,
                'ohlc_dim':self.brain.ohlc_dim,
                'fusion_dim':self.brain.fusion_dim,
                'approved':self.feature_registry.approved_ids(),
                'approved_specs':pre_specs,
                'weights_path':pre_path})
            old_ohlc=self.brain.ohlc_dim
            self.brain.apply_registry(self.feature_registry)
            learning_generation.bump(self.store,'feature_registry_expanded')
            # Keep shadow dim aligned (insert dynamic before external if on fusion).
            if self.shadow_brain is not None and self.shadow_brain.d!=self.brain.core.d:
                insert=old_ohlc if self.brain.feature_schema==self.brain.fusion_schema else None
                try:
                    self.shadow_brain.expand_inputs(self.brain.core.d,insert_at=insert)
                except Exception:
                    self.shadow_brain=None
                    self._ensure_shadow_brain()
            post_path=None
            if hasattr(self.brain,'backup_weights'):
                post_path=self.brain.backup_weights(
                    tag=f'v{int(self.brain.model_version)}')
            post_specs=[dict(x) for x in self.feature_registry.approved]
            self.model_validator.push_version({
                'model_version':self.brain.model_version,
                'feature_schema':self.brain.feature_schema,
                'ohlc_dim':self.brain.ohlc_dim,
                'fusion_dim':self.brain.fusion_dim,
                'approved':self.feature_registry.approved_ids(),
                'approved_specs':post_specs,
                'weights_path':post_path})
        self.policy.shadow_self_dev_tick(self)

    def _shadow_paper_step(self,*,mid,spread=0.,bar_time=None):
        """Advance independent shadow paper journal from current shadow vote."""
        scores=self._shadow_scores_now()
        if not scores or mid is None:return None
        try:
            action=max(('BUY','SELL','HOLD'),key=lambda a:float(scores.get(a,0)))
        except (TypeError,ValueError):
            return None
        # Scale price→£ from LIVE broker history so paper is cash-comparable.
        live=getattr(self.profit_learning,'history',[]) or []
        self.shadow_paper.update_cash_scale(live)
        return self.shadow_paper.on_bar(
            action,mid=mid,spread=spread,bar_time=bar_time,
            cash_scale=self.shadow_paper.cash_scale)

    def restore_neural_weights(self, version_meta_or_path):
        """Rollback hook: restore feature registry + neural weights together.

        Accepts a full version_meta dict (preferred) or a bare weights path
        string for older call sites/tests.
        """
        if isinstance(version_meta_or_path,dict):
            meta=version_meta_or_path
            path=meta.get('weights_path')
            specs=meta.get('approved_specs')
        else:
            meta=None;path=version_meta_or_path;specs=None
        # Registry first so layout matches the weight file about to load.
        if specs is not None:
            self.feature_registry.replace_approved(specs)
        ok=False
        if path and hasattr(self.brain,'restore_weights'):
            ok=bool(self.brain.restore_weights(path))
        if ok or specs is not None:
            # Re-sync brain schema labels from restored registry without expanding.
            reg=self.feature_registry
            self.brain.ohlc_dim=reg.ohlc_dim()
            self.brain.fusion_dim=reg.fusion_dim()
            self.brain.ohlc_schema=reg.ohlc_schema()
            self.brain.fusion_schema=reg.fusion_schema()
            # If weights restored, feature_schema on brain comes from the file;
            # if only registry restored, keep brain.feature_schema as loaded.
            self.shadow_brain=None
            self._ensure_shadow_brain()
            self.store.event('learning','Model snapshot restored',
                {'weights':bool(ok),'registry':specs is not None,
                 'ohlc_dim':self.brain.ohlc_dim,
                 'approved':self.feature_registry.approved_ids(),
                 'model_version':(meta or {}).get('model_version') if isinstance(meta,dict) else None})
        return bool(ok or specs is not None)

