"""Automatic broker/AI/order/learning cycle and persistent event journal.

Non-idempotent operations are journaled BEFORE transmission. Ambiguous outcomes
are reconciled, never blindly repeated. No order until an initial mobile trade has
been detected in this authenticated session and broker confirmed its closure.
"""
import asyncio
import json
import math
import os
import re
import resource
import sqlite3
import time
from collections import deque
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
import numpy as np
from .core import NeuralCore
from .autonomy import AutoEvolution
from .math_rules import (stop_take, ohlc_range_distance,
                         distances_meet_broker_min, levels_meet_broker_min,
                         cash_risk, tick_value_from_instrument)
from . import bracket_learn
from . import hard_risk
from .protection_ai import propose_broker_trail, trail_extreme_from_broker
from .storage import DATA
from .market_context import market_features, candle_bucket, candle_time, FEATURE_SCHEMA, FEATURE_DIM
from .market_fusion import (
    FUSION_SCHEMA, FUSION_DIM, fusion_features, fusion_dim_for, EXTERNAL_NAMES,
    fusion_gap_reason,
)
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
        self.db=sqlite3.connect(str(path),check_same_thread=False,timeout=30)
        try:
            # Refuse trading against malformed state/order journals. Recovery is
            # always explicit; NEVER discard or recreate the old DB silently.
            check=self.db.execute('PRAGMA integrity_check').fetchall()
            if check!=[('ok',)]:
                raise sqlite3.DatabaseError('integrity_check failed: '+str(check[:3]))
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA busy_timeout=5000')
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
    def close(self):
        """Release SQLite handles so learning reset can move robot.sqlite3 safely."""
        db=getattr(self,'db',None)
        if db is None:return
        try:db.close()
        except Exception:pass
        self.db=None
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
    Audit #17: minute-direction and trade-PnL keep separate counters and
    OBJECTIVE_WEIGHTS so frequent M1 labels cannot wash out trade learning.
    Uncalibrated softmax scores are not probabilities of profitable trades.
    """
    LABEL_VERSION=4  # v4: 1-bar market labels + trade P&L regret learning
    HORIZON=1        # same cadence as M1 decisions — not an 8-minute lag
    GROWTH_INTERVAL=8   # faster structural growth so the net adapts with trades
    FAST_GROWTH_INTERVAL=4  # sustained high reconstruction error
    # Relative objective weights (core.OBJECTIVE_WEIGHTS): trade_pnl=1.0,
    # minute_direction=0.2 → trade signal is 5× stronger per update step.
    MINUTE_OBJECTIVE_WEIGHT=0.2
    TRADE_OBJECTIVE_WEIGHT=1.0

    def __init__(self,store,n=16,data_dir=None,seed=7):
        self.store=store
        self.path=(Path(data_dir) if data_dir else DATA)/'brain.npz'
        self.initial_neurons=n
        # Per-client seed (hub) so fresh brains are not identical clones.
        self.init_seed=int(seed)
        self.core=NeuralCore(n=n,seed=self.init_seed)
        self.epic=None
        self._legacy_epic=self.store.get('legacy_brain_epic')
        self.history=deque(maxlen=1000)
        self.pending=deque()
        # Audit #17: minute-direction vs trade-PnL tallies stay separate.
        # `correct`/`labeled` remain the minute-direction aliases (UI/compat).
        self.minute_correct=0;self.minute_labeled=0
        self.trade_correct=0;self.trade_labeled=0
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
        self.minute_correct=0;self.minute_labeled=0
        # Trade-PnL counters are session/runtime (not rebuilt from minute signals).
        self.trade_correct=0;self.trade_labeled=0
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
            # Signals table holds minute-direction labels only (audit #17).
            self.minute_labeled+=1
            self.minute_correct+=int(action==('BUY','SELL','HOLD')[int(label)])
        self.labeled=self.minute_labeled
        self.correct=self.minute_correct

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
            self.core=NeuralCore(n=self.initial_neurons,seed=self.init_seed)
            self.feature_schema=None  # Schema belongs to the selected EPIC, not the prior one.
            self.fusion_labels=0
            self.load(self._path_for(epic),expected_epic=epic)
        self.epic=epic
        self._restore_context(epic)
        self._restore_pending(epic)
        # New/reset instrument context requires prep/eval before LIVE OPEN (audit #22).
        # Warm brains with enough online labels can re-qualify via trading_readiness().
        if self.labeled < 64:
            try:
                from . import model_readiness
                model_readiness.reset_state(
                    self.store,epic,
                    reason='New/reset instrument — load candles then train/eval before LIVE OPEN')
            except Exception:
                pass
        self.store.event('learning','Instrument learning context activated',{'epic':epic})

    def _reset_fresh_core(self):
        self.core=NeuralCore(n=self.initial_neurons,seed=self.init_seed)
        self.feature_schema=None
        self.fusion_labels=0
        self.ohlc_dim=FEATURE_DIM;self.fusion_dim=FUSION_DIM
        self.ohlc_schema=FEATURE_SCHEMA;self.fusion_schema=FUSION_SCHEMA
        self.model_version=0

    def _core_snapshot(self):
        """In-memory clone of weights + schema so a failed restore can roll back."""
        c=self.core
        names=('P','W','R','O','b','h','c','tau','tau_c','mu','nu','age','utility','G','neuron_ids')
        arrays={n:getattr(c,n).copy() for n in names if hasattr(c,n)}
        return {
            'arrays':arrays,
            'n':c.n,'d':c.d,'steps':c.steps,'vbar':c.vbar,
            'births':c.births,'deaths':c.deaths,
            'next_neuron_id':getattr(c,'next_neuron_id',c.n),
            'feature_schema':self.feature_schema,'fusion_labels':self.fusion_labels,
            'ohlc_dim':self.ohlc_dim,'fusion_dim':self.fusion_dim,
            'ohlc_schema':self.ohlc_schema,'fusion_schema':self.fusion_schema,
            'model_version':self.model_version,
        }

    def _apply_core_snapshot(self, snap):
        c=self.core
        for name,arr in snap['arrays'].items():
            setattr(c,name,arr.copy())
        c.n=int(snap['n']);c.d=int(snap['d'])
        c.last_z=np.zeros(c.d)
        c.steps=int(snap['steps']);c.vbar=float(snap['vbar'])
        c.births=int(snap['births']);c.deaths=int(snap['deaths'])
        c.next_neuron_id=int(snap['next_neuron_id'])
        self.feature_schema=snap['feature_schema']
        self.fusion_labels=int(snap['fusion_labels'])
        self.ohlc_dim=int(snap['ohlc_dim']);self.fusion_dim=int(snap['fusion_dim'])
        self.ohlc_schema=snap['ohlc_schema'];self.fusion_schema=snap['fusion_schema']
        self.model_version=int(snap['model_version'])

    def _read_weight_file(self, path, expected_epic=None):
        """Parse + validate a .npz brain file. Raises on corrupt/mismatch. No mutate."""
        path=Path(path)
        with np.load(path,allow_pickle=False) as d:
            file_epic=str(d['epic']) if 'epic' in d else ''
            if expected_epic and file_epic and str(file_epic)!=str(expected_epic):
                raise ValueError('Brain file instrument mismatch')
            names=('P','W','R','O','b','h','c','tau','tau_c','mu','nu','age','utility','G')
            arrays={name:d[name].copy() for name in names}
            n=len(arrays['h']);d_in=arrays['P'].shape[1]
            feature_schema=str(d['feature_schema']) if 'feature_schema' in d else None
            ohlc_dim=int(d['ohlc_dim']) if 'ohlc_dim' in d else FEATURE_DIM
            fusion_dim=int(d['fusion_dim']) if 'fusion_dim' in d else FUSION_DIM
            ohlc_schema=str(d['ohlc_schema']) if 'ohlc_schema' in d else FEATURE_SCHEMA
            fusion_schema=str(d['fusion_schema']) if 'fusion_schema' in d else FUSION_SCHEMA
            if ((feature_schema==fusion_schema and d_in!=fusion_dim)
                or (feature_schema==ohlc_schema and d_in!=ohlc_dim)
                or (feature_schema==FUSION_SCHEMA and d_in!=FUSION_DIM and 'fusion_dim' not in d)
                or (feature_schema==FEATURE_SCHEMA and d_in!=FEATURE_DIM and 'ohlc_dim' not in d)):
                raise ValueError('Stored neural feature schema and input weights disagree')
            for name in names:
                arr=arrays[name]
                if not np.isfinite(arr).all():
                    raise ValueError(f'Non-finite weights in {name}')
            if 'neuron_ids' in d:
                neuron_ids=d['neuron_ids'].astype(np.int64).copy()
                next_neuron_id=int(d['next_neuron_id'])
            else:
                neuron_ids=np.arange(n,dtype=np.int64)
                next_neuron_id=n
            if len(neuron_ids)!=n:
                raise ValueError('neuron_ids length disagrees with core size')
            meta={
                'epic':file_epic,'n':n,'d':d_in,
                'steps':int(d['steps']),'vbar':float(d['vbar']),
                'births':int(d['births']) if 'births' in d else 0,
                'deaths':int(d['deaths']) if 'deaths' in d else 0,
                'feature_schema':feature_schema,
                'fusion_labels':int(d['fusion_labels']) if 'fusion_labels' in d else 0,
                'ohlc_dim':ohlc_dim,'fusion_dim':fusion_dim,
                'ohlc_schema':ohlc_schema,'fusion_schema':fusion_schema,
                'model_version':int(d['model_version']) if 'model_version' in d else 0,
                'neuron_ids':neuron_ids,'next_neuron_id':next_neuron_id,
            }
            arrays['neuron_ids']=neuron_ids
            return arrays,meta

    def _apply_weight_payload(self, arrays, meta):
        c=self.core
        for name,arr in arrays.items():
            setattr(c,name,arr.copy())
        c.n=int(meta['n']);c.d=int(meta['d'])
        c.last_z=np.zeros(c.d)
        c.steps=int(meta['steps']);c.vbar=float(meta['vbar'])
        c.births=int(meta['births']);c.deaths=int(meta['deaths'])
        c.next_neuron_id=int(meta['next_neuron_id'])
        self.feature_schema=meta['feature_schema']
        self.fusion_labels=int(meta['fusion_labels'])
        self.ohlc_dim=int(meta['ohlc_dim']);self.fusion_dim=int(meta['fusion_dim'])
        self.ohlc_schema=meta['ohlc_schema'];self.fusion_schema=meta['fusion_schema']
        self.model_version=int(meta['model_version'])

    def load(self,path=None,expected_epic=None):
        """Boot-time load. Corrupt files reset to a fresh core (return False)."""
        path=Path(path) if path else self.path
        if not path.exists():return False
        try:
            arrays,meta=self._read_weight_file(path,expected_epic=expected_epic)
            self._apply_weight_payload(arrays,meta)
            return True
        except Exception as exc:
            self.store.event('error','Neural state load failed',{'type':type(exc).__name__,'file':path.name})
            # Never continue with a partly loaded/mismatched network.
            self._reset_fresh_core()
            return False

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
        epic_tag=''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in str(self.epic or 'unknown'))[:24]
        dest=src.with_name(f"{src.stem}.bak_{epic_tag}_{safe}_v{int(self.model_version)}.npz")
        import shutil
        shutil.copy2(src,dest)
        return str(dest)

    def restore_weights(self, weights_path, *, registry=None,
                        expected_feature_schema=None, expected_ohlc_dim=None,
                        expected_fusion_dim=None):
        """Reload neural weights from a backup — fail closed on corrupt/mismatch.

        Never reports success after silently resetting to a fresh core. Validates
        instrument, feature schema, and (when provided) registry dims before
        activating and saving.
        """
        path=Path(weights_path)
        if not path.exists():
            self.store.event('error','Weight restore missing file',{'path':str(path)})
            return False
        before_ver=self.model_version
        snap=self._core_snapshot()
        try:
            arrays,meta=self._read_weight_file(path,expected_epic=self.epic)
            file_epic=meta.get('epic') or ''
            if self.epic and file_epic and str(file_epic)!=str(self.epic):
                raise ValueError('Backup instrument does not match live epic')
            if expected_feature_schema and meta.get('feature_schema') not in (
                    None,expected_feature_schema):
                raise ValueError('Backup feature_schema mismatch')
            if expected_ohlc_dim is not None and int(meta['ohlc_dim'])!=int(expected_ohlc_dim):
                raise ValueError('Backup ohlc_dim mismatch')
            if expected_fusion_dim is not None and int(meta['fusion_dim'])!=int(expected_fusion_dim):
                raise ValueError('Backup fusion_dim mismatch')
            if registry is not None:
                reg_ohlc=registry.ohlc_dim();reg_fus=registry.fusion_dim()
                reg_os=registry.ohlc_schema();reg_fs=registry.fusion_schema()
                fs=meta.get('feature_schema')
                if fs in (reg_os,FEATURE_SCHEMA) and int(meta['d'])!=int(reg_ohlc):
                    raise ValueError('Backup OHLC dim disagrees with feature registry')
                if fs in (reg_fs,FUSION_SCHEMA) and int(meta['d'])!=int(reg_fus):
                    raise ValueError('Backup fusion dim disagrees with feature registry')
                if fs not in (None,reg_os,reg_fs,FEATURE_SCHEMA,FUSION_SCHEMA,
                              meta.get('ohlc_schema'),meta.get('fusion_schema')):
                    # Allow exact match to schemas stored in the file itself.
                    if fs not in (meta.get('ohlc_schema'),meta.get('fusion_schema')):
                        raise ValueError('Backup feature_schema not recognized for registry')
            self._apply_weight_payload(arrays,meta)
            # Persist restored weights as the active brain file only after validation.
            self.save()
        except Exception as exc:
            self._apply_core_snapshot(snap)
            self.store.event('error','Weight restore refused — live core unchanged',
                {'path':path.name,'type':type(exc).__name__,'detail':str(exc)[:200],
                 'epic':self.epic,'from_version':before_ver})
            return False
        self.store.event('learning','Neural weights restored from backup',
            {'path':path.name,'from_version':before_ver,
             'model_version':self.model_version,'inputs':self.core.d,
             'neurons':self.core.n,'epic':self.epic,
             'feature_schema':self.feature_schema})
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
        self.core=NeuralCore(n=self.initial_neurons,d=self.ohlc_dim,seed=self.init_seed)
        self.feature_schema=self.ohlc_schema
        self.history=deque(maxlen=1000);self.pending=deque()
        self.minute_correct=0;self.minute_labeled=0
        self.trade_correct=0;self.trade_labeled=0
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
        self.store.event('learning','Cross-market, news and public liquidity features joined neural inputs; old weights preserved',
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
        # Audit #17: minute-direction objective (weight 0.2) — not trade-PnL.
        if len(self.pending)>=self.HORIZON:
            past=self.pending.popleft()
            if past['epic']==epic:
                label=self._label(float(past['mid']),mid,float(past['spread']),spread)
                transition=past.get('transition')
                if transition is not None:
                    self.core.update_utility(past['h'],past['c'],label,
                        neuron_ids=past.get('neuron_ids'))
                    from .core import OBJECTIVE_MINUTE_DIRECTION
                    loss=self.core.learn(transition,label,
                                         objective=OBJECTIVE_MINUTE_DIRECTION)
                    if loss is not None and self.feature_schema==self.fusion_schema:
                        self.fusion_labels+=1
                    if loss is not None and (self.minute_labeled+1)%100==0:
                        self.store.event('learning','Horizon-label learning checkpoint',
                            {'loss':loss,'horizon':self.HORIZON,'epic':epic,
                             'objective':'minute_direction',
                             'objective_weight':self.MINUTE_OBJECTIVE_WEIGHT,
                             'proven_improvement':False})
                self.minute_labeled+=1
                hit=int(past['action']==('BUY','SELL','HOLD')[label])
                self.minute_correct+=hit
                self.labeled=self.minute_labeled
                self.correct=self.minute_correct
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
        # Uncalibrated max softmax — NOT calibrated profit probability (audit #24).
        result['confidence']=max(result['scores'].values())
        result['model_score']=result['confidence']
        # Minute-direction label hit-rate — NOT closed-trade win rate (audit #24).
        result['minute_direction_accuracy']=self.correct/self.labeled if self.labeled else None
        result['accuracy_proxy']=result['minute_direction_accuracy']
        return result

    def trading_readiness(self):
        """Prep/eval readiness for LIVE OPEN (audit #22)."""
        from . import model_readiness
        return model_readiness.trading_readiness(self.store,self)

    def ensure_prep_eval(self,candles,*,tick_size=0.01,spread_fallback=0.0):
        """Load candles → train split → holdout eval; block LIVE until ready."""
        from . import model_readiness
        return model_readiness.ensure_prep_eval(
            self.store,self,candles,tick_size=tick_size,spread_fallback=spread_fallback)

    def reset_prep_eval(self,reason='Model reset — prep/eval required before LIVE OPEN'):
        from . import model_readiness
        if self.epic:
            return model_readiness.reset_state(self.store,self.epic,reason=reason)
        return None

    def mark_prep_ready(self,reason='Operator/test readiness override'):
        """Explicit ready mark for tests/fixtures — not a score-edge bypass."""
        from . import model_readiness
        if not self.epic:return None
        state=model_readiness.load_state(self.store,self.epic)
        state.update({'ready':True,'stage':'ready','reason':reason})
        return model_readiness.save_state(self.store,self.epic,state)

    def learn_from_trade(self, transition, label, *, lr=None, won=False,
                         count_sample=True):
        """Closed-trade P&L objective — separate from minute-direction (audit #17–18).

        Uses OBJECTIVE_TRADE_PNL weight (1.0). Updates trade_* counters only —
        never minute labeled/correct. Ordinary learn is never proven_improvement.
        """
        from .core import OBJECTIVE_TRADE_PNL
        if transition is None or label not in (0,1,2):return None
        loss=self.core.learn(transition,label,lr=lr,objective=OBJECTIVE_TRADE_PNL)
        meta=getattr(self.core,'last_learn_meta',None) or {}
        if count_sample and loss is not None:
            self.trade_labeled+=1
            self.trade_correct+=int(bool(won))
        if meta.get('proven_improvement'):
            meta=dict(meta);meta['proven_improvement']=False
            self.core.last_learn_meta=meta
        self.store.event('learning','Trade-PnL objective update',{
            'objective':'trade_pnl',
            'objective_weight':self.TRADE_OBJECTIVE_WEIGHT,
            'loss':loss,'won':bool(won),
            'trade_labeled':self.trade_labeled,'trade_correct':self.trade_correct,
            'minute_labeled':self.minute_labeled,
            'restored':bool(meta.get('restored')),
            'proven_improvement':False,
        })
        return loss


from .external_risk import assess_news
from .calendar_feed import refresh_calendar
from .liquidity_feed import refresh_liquidity
from .instrument_profile import instrument_profile
from .intermarket import IntermarketMonitor

class SingleTradeEngine:
    def __init__(self,store,brain,policy_path=None):
        self.store=store;self.brain=brain;self.broker=None
        self.armed=False;self.execution_enabled=False;self.mandate=None
        self.positions=[];self.account={};self.market={};self.last_signal=None;self.last_error=None
        self.position_age=0;self.last_position_id=None;self.last_closed_step=-1
        self.last_entry_signal_step=-1;self.last_order_ref=None
        self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
        self.pending_order=False;self.pending_close=False;self.lock=asyncio.Lock()
        self._pending_entry_brain=None  # neural state at the decision that opened the trade
        self.last_time=None;self.last_account_tick=0;self.last_candle_tick=-20
        # Prefer explicit path; AutoEvolution also defaults next to store journal.
        self.policy=AutoEvolution(store,policy_path)
        self.tick_count=0;self.latest_close_id=None
        self.market_context={'ready':False,'reason':'Waiting for complete multi-timeframe candles'}
        self.latest_fusion_ready=False
        self.fusion_block_reason=None  # which EXTERNAL source blocks new fused entry
        self.last_policy_decision='HOLD';self.last_entry_reason='Awaiting a fresh signal'
        self.last_entry_audit_key=None
        self.protected=set();self._seen_initial=False;self._unresolved_at_login=False
        self._paused_for_multi=False;self._paused_for_mismatch=False;self._paused_for_read=False;self.operator_paused=False
        self.protection_brief={'status':'Awaiting first robot-opened position'}
        self.last_protection_bar=None
        self.last_calendar_refresh=0.0
        self._calendar_task=None
        self.calendar_feed_error=None
        self.last_liquidity_refresh=0.0
        self._liquidity_task=None
        self.liquidity_feed_error=None
        self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
        self.intermarket=IntermarketMonitor()
        self._peer_refresh_task=None
        self._peer_requested_bar=None
        self.news_calendar_path=self.store.path.parent/'economic_calendar.json' if hasattr(self.store,'path') else __import__('pathlib').Path('data/economic_calendar.json')
        self.liquidity_path=self.store.path.parent/'liquidity_snapshot.json' if hasattr(self.store,'path') else __import__('pathlib').Path('data/liquidity_snapshot.json')
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

    async def _refresh_liquidity_if_due(self):
        moment=time.monotonic()
        if self.last_liquidity_refresh and moment-self.last_liquidity_refresh<45:return
        self.last_liquidity_refresh=moment
        epic=(self.mandate or {}).get('epic')
        result=await refresh_liquidity(
            self.liquidity_path, epic=epic, profile=self.instrument_profile)
        self.liquidity_feed_error=None if result.get('ok') else result.get('reason')
        if not result.get('ok'):
            self.store.event('warning','Public liquidity feed incomplete for instrument',
                             {'epic':epic,'reason':result.get('reason'),
                              'sources':result.get('sources'),
                              'routes':(result.get('routes') or {}).get('label')})
        else:
            self.store.event('market','Public volume/DOM snapshot refreshed',
                             {'epic':epic,'sources':result.get('sources'),
                              'routes':(result.get('routes') or {}).get('label')})

    def _schedule_liquidity_in_background(self):
        if self._liquidity_task is not None and not self._liquidity_task.done():return
        if self.last_liquidity_refresh and time.monotonic()-self.last_liquidity_refresh<45:return
        self._liquidity_task=asyncio.create_task(self._refresh_liquidity_if_due())

    def cancel_liquidity_refresh(self):
        if self._liquidity_task is not None and not self._liquidity_task.done():
            self._liquidity_task.cancel()
        self._liquidity_task=None

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
        self.fusion_block_reason=None
        self.last_calendar_refresh=0.;self.calendar_feed_error=None
        self.last_liquidity_refresh=0.;self.liquidity_feed_error=None
        self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
        self.cancel_peer_refresh()
        self.cancel_calendar_refresh()
        self.cancel_liquidity_refresh()
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
    def _reload_instrument_components(self):
        """Rebind in-memory learning objects after an instrument bag switch."""
        self.feature_registry=FeatureRegistry(self.store)
        self.feature_discovery=FeatureDiscovery(self.store)
        self.feature_validator=FeatureValidator(
            self.store,self.feature_registry,discovery=self.feature_discovery)
        self.profit_learning=ProfitLearning(self.store)
        self.model_validator=ModelValidator(self.store)
        from .shadow_paper import ShadowPaperJournal
        self.shadow_paper=ShadowPaperJournal(self.store)
        self.shadow_brain=None
        self._last_self_dev_bar=None
        if hasattr(self.brain,'apply_registry'):
            try:self.brain.apply_registry(self.feature_registry)
            except Exception as exc:
                self.store.event('warning','Registry apply after instrument switch deferred',
                    {'type':type(exc).__name__})

    def _switch_instrument_learning(self, previous_epic, new_epic):
        """Park P&L/shadow/SLTP/registry bags so markets never share live keys."""
        from .instrument_isolation import switch_instrument_bag
        if previous_epic and previous_epic==new_epic:return {'unchanged':True}
        # Persist live neural weights for the outgoing instrument first.
        if previous_epic and getattr(self.brain,'epic',None)==previous_epic:
            try:self.brain.save()
            except Exception:pass
        result=switch_instrument_bag(self.store,previous_epic,new_epic)
        self._reload_instrument_components()
        return result

    async def prepare_new_instrument(self):
        """Explicit operator-authorized switch, no open/unresolved broker positions.

        The next first mobile trade becomes the NEW instrument mandate. Neural
        weight files and per-epic learning bags from prior instruments are
        preserved under instrument-scoped store keys (audit #29).
        """
        async with self.lock:
            if not self.broker:raise RuntimeError('Not connected to broker')
            if self.store.unresolved():raise ValueError('Unresolved broker operation')
            await self.sync()  # Fresh read, not a cached open-position count.
            if self.positions:raise ValueError('Close all broker positions before switching')
            if self.store.unresolved():raise ValueError('Unresolved broker operation')
            previous=self.mandate['epic'] if self.mandate else None
            if previous:
                self._switch_instrument_learning(previous,None)
            self.mandate=None;self.store.set('mandate',None)
            self.last_signal=None;self.last_closed_step=-1
            self.last_entry_signal_step=-1;self.last_entry_audit_key=None
            self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
            self.last_position_id=None;self._seen_initial=False
            self.position_age=0;self.market={}
            self.market_context={'ready':False,'reason':'Waiting for a NEW manual trade in the selected market'}
            self.latest_fusion_ready=False
            self.fusion_block_reason=None
            self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
            self.cancel_peer_refresh()
            self.cancel_calendar_refresh()
            self.cancel_liquidity_refresh()
            self.intermarket=IntermarketMonitor()
            self._paused_for_mismatch=False;self.operator_paused=False
            self.armed=True;self.execution_enabled=True
            self.store.event('control','Market mandate cleared by operator; awaiting new first mobile trade',
                             {'previous_epic':previous,'instrument_bag_parked':bool(previous)})
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

    def _order_fully_matched(self, kind, req, deal_id, confirmation=None, ref=None):
        """Full broker reconcile check — epic alone is never enough (audit #31).

        OPEN: epic + direction + size, and when the confirm carries a dealId it
        must identify the live position. CLOSE: deal gone. PROTECT: dealId + levels.
        """
        if kind=='CLOSE':
            return not any(p.get('dealId')==deal_id for p in self.positions)
        if kind=='PROTECT':
            return (len(self.positions)==1 and self.positions[0].get('dealId')==deal_id and
                    self._protection_matches(self.positions[0],req or {}))
        if kind!='OPEN' or not req or not self.mandate:
            return False
        if len(self.positions)!=1:return False
        p=self.positions[0]
        try:
            size_ok=Decimal(str(p.get('size')))==Decimal(str(req.get('size')))
        except (ValueError,TypeError,ArithmeticError):
            size_ok=False
        epic_ok=(p.get('epic')==self.mandate.get('epic') and
                 (req.get('epic') is None or p.get('epic')==req.get('epic')))
        dir_ok=p.get('direction')==req.get('direction')
        if not (epic_ok and dir_ok and size_ok):
            return False
        # Identifiable broker operation: confirm dealId must match live when present.
        conf=confirmation or {}
        conf_deal=conf.get('dealId') or conf.get('dealID')
        if conf_deal is not None and str(p.get('dealId'))!=str(conf_deal):
            return False
        # dealReference already authenticated the accept; require it when provided.
        if ref and conf.get('dealReference') not in (None,ref):
            return False
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
        # Never clear mismatch / read-only / operator pauses just because orders settled.
        if (not self._unresolved_at_login and not self.operator_paused
                and not self._paused_for_multi and not self._paused_for_mismatch
                and not self._paused_for_read):
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
                if pnl is not None and src=='broker':
                    conf['profit']=pnl
                    if ccy:conf['pnl_currency']=ccy
                    self._learn_closed_trade(prev_id,conf)
                else:
                    # Audit #1: do not freeze last UPL as realized — mark pending and retry.
                    self.store.close_trade(prev_id,exit_guess,source='pending_broker_pnl',overwrite=True)
                    self._queue_pending_pnl(prev_id,exit_level=exit_guess)
                    self.store.event('trade','Deal closed; broker cash P&L pending re-request',
                                     {'dealId':prev_id,'exit_guess':exit_guess,'pnl_source':src})
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
                    # Park prior instrument bags (P&L/shadow/SLTP/registry) then
                    # switch model state BEFORE the manual deal can close.
                    prev_epic=old['epic'] if old else None
                    self._switch_instrument_learning(prev_epic,p['epic'])
                    if hasattr(self.brain,'_activate'):
                        self.brain._activate(p['epic'])
                    self.last_candle_tick=-20
                    self.cancel_peer_refresh();self.cancel_calendar_refresh()
                    self.cancel_liquidity_refresh()
                    self.intermarket=IntermarketMonitor()
                    self.instrument_profile={'mapped':False,'currencies':[],'kind':'UNKNOWN'}
                    self.market_context={'ready':False,'reason':'Waiting for completed candles on newly adopted market'}
                    self.latest_fusion_ready=False
                    self.fusion_block_reason=None
                    self.last_calendar_refresh=0.;self.calendar_feed_error=None
                    self.last_liquidity_refresh=0.;self.liquidity_feed_error=None
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
            # Audit #1: re-request pending realized cash without using UPL as final.
            try:await self._retry_pending_broker_pnl()
            except Exception as exc:
                self.store.event('warning','Pending broker P&L retry failed',
                                 {'type':type(exc).__name__})
            if not self.mandate:return
            # Do not delay managing SL/TP on an OPEN trade for a remote news feed.
            # External feeds gate future NEW entries only.
            if not self.positions:
                await self._refresh_calendar_if_due()
                await self._refresh_liquidity_if_due()
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
                # News/liquidity downloads are background-only while an open
                # position needs protection/SLTP management. Initial missing
                # stops were already addressed above.
                self._schedule_calendar_in_background()
                self._schedule_liquidity_in_background()
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
            on_fusion=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                       and getattr(self.brain,'fusion_schema',None) is not None)
            if self.market_context['ready']:
                self.brain.last_regime=self.market_context['trend']
                fused=fusion_features(self.market_context,self.intermarket.state,
                    self.mandate['epic'],self.news_calendar_path,self.instrument_profile['currencies'],
                    now=clock(),expected_ohlc_dim=self.brain.ohlc_dim,
                    fusion_schema=self.brain.fusion_schema,
                    liquidity_path=self.liquidity_path)
                self.latest_fusion_ready=fused is not None
                if fused is not None:
                    self.fusion_block_reason=None
                else:
                    # Name the EXTERNAL source that blocks a new fused decision.
                    self.fusion_block_reason=fusion_gap_reason(
                        self.market_context,self.intermarket.state,
                        self.mandate['epic'],self.news_calendar_path,
                        self.instrument_profile['currencies'],
                        now=clock(),expected_ohlc_dim=self.brain.ohlc_dim,
                        liquidity_path=self.liquidity_path)
                # Prefer fusion when ready. After fusion-model transition, a gap
                # must NOT drive a NEW entry from a stale prior signal. Open
                # position protection continues below regardless of fusion.
                # Pre-fusion OHLC models may still tick on local features alone.
                if fused is not None:
                    next_signal=self.brain.tick(self.market_context['close'],ask-bid,tick,
                        self.mandate['epic'],dt=60,features=fused,
                        bar_time=self.market_context['bar_time'],external_features=True)
                elif not on_fusion:
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
                    # Audit #32: never run neural growth / feature search before
                    # open-position protection. Flat books may self-dev now;
                    # open books defer until after trail/protect below.
                    if not self.positions:
                        self._run_self_development_cycle(candles,spread=ask-bid)
                elif on_fusion and fused is None and not self.positions:
                    # Flat + fusion gap: stale signal must not authorize entry.
                    # Keep last_signal for UI audit only when a position is open.
                    self.last_signal=None
                self.brain.last_regime=self.market_context['trend']
            else:
                self.latest_fusion_ready=False
                self.fusion_block_reason='ohlc: market context not ready'
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
            # Audit #22: historical candle load → chronological train → holdout eval
            # before autonomous LIVE OPEN. Separate from the live tick above.
            if (not self.positions and self.market_context.get('ready') and
                hasattr(self.brain,'ensure_prep_eval')):
                try:
                    rd=self.brain.trading_readiness()
                    if not rd.get('ready'):
                        self.brain.ensure_prep_eval(
                            candles,tick_size=tick or 0.01,spread_fallback=ask-bid)
                except Exception as exc:
                    self.store.event('warning','Prep/eval stage failed',
                                    {'type':type(exc).__name__})
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
                # Protection finished first — heavy self-dev only if resources allow
                # and never instead of trail/protect (audit #32).
                if new_closed_bar:
                    self._run_self_development_cycle(candles,spread=ask-bid,
                                                     defer_if_busy=True)
            elif (len(self.positions)==0 and self.last_signal and
                  self.last_closed_step>=0 and new_closed_bar and
                  self.last_entry_processed_bar!=self.last_decision_bar):
                # After fusion transition, refuse new entries while EXTERNAL data
                # is gapped — never act on a stale pre-gap neural vote.
                on_fusion=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                           and getattr(self.brain,'fusion_schema',None) is not None)
                if on_fusion and not self.latest_fusion_ready:
                    self.last_entry_reason='External fusion gap blocks new entry: '+(
                        self.fusion_block_reason or 'aligned external neural inputs unavailable')
                    self.store.event('entry','New entry blocked — fusion data gap',
                        {'epic':self.mandate['epic'],'bar_time':self.market_context.get('bar_time'),
                         'block':self.fusion_block_reason,'open_protection':'continues_if_position'})
                else:
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

    def _fusion_entry_blocked(self):
        """True when fusion-schema model lacks fresh EXTERNAL inputs for a new entry."""
        on_fusion=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                   and getattr(self.brain,'fusion_schema',None) is not None)
        return bool(on_fusion and not self.latest_fusion_ready)

    def live_open_gates(self,action=None):
        """Shared LIVE OPEN helper for poll/_entry_evaluation/submit (audit #22–#23).

        Direction stays neural argmax; this only reports whether readiness +
        utility gates currently allow submitting an OPEN. Other agents editing
        submit should call this before placing an order.
        """
        readiness=self.brain.trading_readiness() if hasattr(self.brain,'trading_readiness') else {'ready':False,'reason':'No readiness API'}
        if not readiness.get('ready'):
            return {'allowed':False,'reason':readiness.get('reason') or 'Model prep/eval not ready',
                    'readiness':readiness,'economic_utility':None}
        if action is not None and action not in ('BUY','SELL'):
            return {'allowed':False,'reason':'No directional action','readiness':readiness,
                    'economic_utility':None}
        return {'allowed':True,'reason':'Readiness passed — utility checked in evaluate_neural_entry',
                'readiness':readiness,'economic_utility':None}

    def _entry_evaluation(self):
        if not self.market_context.get('ready',False):
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'Market context unavailable: '+self.market_context.get('reason','unknown'),
                    'evidence':None}
        if self._fusion_entry_blocked():
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'External fusion gap blocks new entry: '+(
                        self.fusion_block_reason or 'aligned external neural inputs unavailable'),
                    'evidence':None,'fusion_gap':True,'fusion_block':self.fusion_block_reason}
        if not self.last_signal or not self.mandate or self.brain.epic!=self.mandate['epic']:
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'Waiting for same-instrument neural signal','evidence':None}
        ctx=self.market_context or {}
        readiness=self.brain.trading_readiness() if hasattr(self.brain,'trading_readiness') else None
        history=list(self.brain.history)
        result=self.policy.evaluate_neural_entry(
            self.last_signal['scores'],
            returns_bps=ctx.get('returns_bps'),
            trend=ctx.get('trend'),
            regime=ctx.get('regime'),
            history=history,
            readiness=readiness,
            spread_bps=self._spread_bps())
        if result['action'] in ('BUY','SELL'):
            gates=self.live_open_gates(result['action'])
            if not gates['allowed']:
                return dict(result,action='HOLD',reason=gates['reason'],
                            readiness=gates.get('readiness'))
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
        req=None
        if kind in ('OPEN','PROTECT'):
            req=json.loads(self.store.db.execute('SELECT payload FROM orders WHERE id=?',(order_id,)).fetchone()[0])
        matched=self._order_fully_matched(kind,req,deal_id,confirmation,ref)
        if not matched:
            for delay in (.5,1.,2.):
                await asyncio.sleep(delay);await self.sync()
                # Retries must re-check the FULL match (epic+direction+size+deal),
                # never epic alone (audit #31).
                matched=self._order_fully_matched(kind,req,deal_id,confirmation,ref)
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
            # Confirm often omits profit on CLOSE; fill from history before learn.
            # Never promote last UPL to realized cash (audit #1).
            pnl,src,_ccy=await self._resolve_broker_pnl(deal_id,confirmation)
            if pnl is not None and src=='broker':
                if _num(confirmation.get('profit')) is None and _num(confirmation.get('pnl')) is None:
                    confirmation={**confirmation,'profit':pnl}
                if _ccy and not confirmation.get('pnl_currency'):
                    confirmation={**confirmation,'pnl_currency':_ccy}
                self._learn_closed_trade(deal_id,confirmation)
            else:
                exit_lvl=_num(confirmation.get('level'))
                self.store.close_trade(deal_id,exit_lvl,source='pending_broker_pnl',overwrite=True)
                self._queue_pending_pnl(deal_id,exit_level=exit_lvl)
                self.store.event('warning','Close confirmed; broker cash P&L pending re-request',
                                 {'deal_id':deal_id,'reference':ref,'pnl_source':src})
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

    def _account_equity(self):
        """Best-effort account equity/balance for drawdown gating (audit #21)."""
        account=self.account.get('accounts',[]) if isinstance(self.account,dict) else []
        active=next((a for a in account if a.get('preferred') or
                     a.get('accountId')==self.account.get('currentAccountId')),
                    account[0] if account else {})
        bal=active.get('balance') if isinstance(active,dict) else None
        if isinstance(bal,dict):
            for key in ('equity','balance','available'):
                v=_num(bal.get(key))
                if v is not None and v>0:return float(v)
        return _num(bal) if not isinstance(bal,dict) else None

    def _infer_exit_reason(self,deal_id,confirmation,*,entry=None,direction=None,
                           exit_level=None,tick=None):
        """Classify SL / TP / trail / vanish / manual for bracket learning (audit #19)."""
        conf=confirmation or {}
        raw=(conf.get('exit_reason') or conf.get('reason') or conf.get('closeReason')
             or conf.get('actionType') or conf.get('activityType'))
        if raw:
            got=bracket_learn.normalize_exit_reason(raw)
            if got!='unknown':return got
        src=str(conf.get('source') or '')
        if src=='position_disappeared':
            # Try level proximity before falling back to vanish.
            pass
        elif src in ('manual','user'):
            return 'manual'
        meta=self.store.get('adaptive_protection:'+str(deal_id)) or {}
        entry_n=_num(entry);exit_n=_num(exit_level);tick_n=_num(tick)
        stop_dist=_num(meta.get('original_risk')) or _num(
            (self.store.get('entry_brain:'+str(deal_id)) or {}).get('stop_distance'))
        sign={'BUY':1,'SELL':-1}.get(direction)
        if entry_n is not None and exit_n is not None and sign is not None:
            move=sign*(exit_n-entry_n)
            tol=max(float(tick_n or 0)*1.5,abs(float(stop_dist or 0))*0.15,1e-9)
            profit_dist=_num((self.store.get('entry_brain:'+str(deal_id)) or {}).get('profit_distance'))
            if stop_dist and move<=-(float(stop_dist)-tol):
                if meta.get('owner')=='AUTO' and meta.get('trail_extreme') is not None:
                    return 'trail'
                return 'sl'
            if profit_dist and move>=(float(profit_dist)-tol):
                return 'tp'
            if meta.get('owner')=='AUTO' and meta.get('trail_extreme') is not None and move<0:
                return 'trail'
        if src=='position_disappeared':
            return 'vanish'
        return 'unknown'

    def _bracket_path_context(self,deal_id,confirmation,saved):
        """Build exit/path/vol/cost context for post-trade SL/TP adaptation."""
        saved=saved or {}
        entry=_num(saved.get('entry'))
        direction=saved.get('direction')
        tick=_num(saved.get('tick_size'))
        exit_level=_num((confirmation or {}).get('level'))
        if exit_level is None:
            exit_level=_num((self.market or {}).get('mid'))
        stop_distance=_num(saved.get('stop_distance'))
        meta=self.store.get('adaptive_protection:'+str(deal_id)) or {}
        if stop_distance is None:
            stop_distance=_num(meta.get('original_risk'))
        exit_reason=self._infer_exit_reason(
            deal_id,confirmation,entry=entry,direction=direction,
            exit_level=exit_level,tick=tick)
        # Recent M1 median range as vol context at close.
        epic=saved.get('epic') or (self.mandate or {}).get('epic')
        vol=None
        if epic:
            candles=self.store.candles(epic,n=60,historical_only=True)
            zone=bracket_learn.zone_at_open(candles)
            vol=_num(zone.get('range_distance'))
        costs=_num(saved.get('entry_spread'))
        adverse=None
        if direction in ('BUY','SELL') and entry is not None and exit_level is not None:
            sign=1 if direction=='BUY' else -1
            signed=sign*(exit_level-entry)
            adverse=abs(signed) if signed<0 else 0.0
        return {
            'exit_reason':exit_reason,'direction':direction,'entry':entry,
            'exit_level':exit_level,'stop_distance':stop_distance,
            'adverse_path':adverse,'volatility':vol,'costs':costs,
        }

    def _hard_risk_gate(self,*,proposed_risk=None):
        """Operator hard limits before OPEN — neural cannot raise these (audit #20/#21)."""
        equity=self._account_equity()
        if equity is not None:
            hard_risk.update_peak_equity(self.store,equity)
        return hard_risk.entry_allowed(
            self.store,proposed_risk=proposed_risk,equity=equity)

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

    def _history_lookback(self,deal_id,*,queued_at=None):
        """Prefer absolute from_time from queue/trade open; else lastPeriod seconds."""
        from_time=None
        for raw in (queued_at,
                    (self.store.db.execute(
                        'SELECT opened FROM trades WHERE deal_id=?',(str(deal_id),)
                        ).fetchone() or [None])[0]):
            if not raw:continue
            try:
                # Capital accepts ISO-ish timestamps; keep store value if parseable.
                from_time=str(raw).replace(' ','T')
                if from_time.endswith('+00:00'):from_time=from_time[:-6]+'Z'
                break
            except Exception:
                from_time=None
        # 24h window covers overnight SL/TP after restart; queued_at narrows when known.
        return {'from_time':from_time,'last_period':None if from_time else 86400}

    @staticmethod
    def _row_cash_pnl(row,details=None):
        details=details if isinstance(details,dict) else {}
        for key in ('profit','pnl','guaranteedStopProfit','realizedPnl','cash'):
            val=_num(details.get(key))
            if val is None:val=_num(row.get(key))
            if val is not None and math.isfinite(val):
                return float(val)
        return None

    def _transaction_matches_deal(self,row,deal):
        """Capital tx.reference is often a short id, not the UUID dealId."""
        deal=str(deal)
        for key in ('reference','transactionId','dealId','dealReference','note'):
            ref=str(row.get(key) or '')
            if not ref:continue
            if ref==deal or deal in ref or ref in deal:
                return True
        return False

    async def _pnl_from_broker_history(self,deal_id,*,queued_at=None):
        """Pull closed-trade cash from Capital history/activity (not mid×size)."""
        if not self.broker or not deal_id:return None
        deal=str(deal_id)
        look=self._history_lookback(deal,queued_at=queued_at)
        # Activity often names the dealId on SL/TP/USER closes.
        if hasattr(self.broker,'activity'):
            try:
                kwargs=dict(detailed=True,deal_id=deal)
                if look['from_time']:
                    kwargs['from_time']=look['from_time'];kwargs['last_period']=None
                else:
                    kwargs['last_period']=look['last_period']
                payload=await self.broker.activity(**kwargs)
                for row in payload.get('activities') or []:
                    if str(row.get('dealId') or '')!=deal:continue
                    details=row.get('details') if isinstance(row.get('details'),dict) else {}
                    val=self._row_cash_pnl(row,details)
                    if val is not None:
                        return float(val),self._cash_currency(row.get('currency'))
            except Exception as exc:
                self.store.event('warning','Activity history P&L lookup failed',
                                 {'type':type(exc).__name__,'deal_id':deal})
        if hasattr(self.broker,'transactions'):
            try:
                kwargs=dict(tx_type='TRADE')
                if look['from_time']:
                    kwargs['from_time']=look['from_time'];kwargs['last_period']=None
                else:
                    kwargs['last_period']=look['last_period']
                payload=await self.broker.transactions(**kwargs)
                for row in payload.get('transactions') or []:
                    if not self._transaction_matches_deal(row,deal):continue
                    val=self._row_cash_pnl(row)
                    if val is not None:
                        return float(val),self._cash_currency(row.get('currency'))
            except Exception as exc:
                self.store.event('warning','Transaction history P&L lookup failed',
                                 {'type':type(exc).__name__,'deal_id':deal})
        return None

    async def _resolve_broker_pnl(self,deal_id,confirmation=None):
        """Confirm.profit → history/activity only.

        Last live UPL is NEVER stored as confirmed realized P&L (audit #1).
        When cash is still missing, return pending_broker_pnl so the caller
        can mark the trade pending and re-request from the broker.
        """
        confirmation=confirmation or {}
        ccy=self._cash_currency(confirmation.get('pnl_currency') or confirmation.get('currency'))
        for key in ('profit','pnl'):
            val=_num(confirmation.get(key))
            if val is not None and math.isfinite(val):
                return float(val),'broker',ccy
        # One quick retry — Capital journals can lag slightly after SL/TP fire.
        queued=None
        for item in (self.store.get('pending_broker_pnl_deals') or []):
            if isinstance(item,dict) and str(item.get('deal_id'))==str(deal_id):
                queued=item.get('queued_at');break
        found=await self._pnl_from_broker_history(deal_id,queued_at=queued)
        if not found and (hasattr(self.broker,'activity') or hasattr(self.broker,'transactions')):
            await asyncio.sleep(.8)
            found=await self._pnl_from_broker_history(deal_id,queued_at=queued)
        if found:
            pnl,hist_ccy=found
            return pnl,'broker',self._cash_currency(hist_ccy)
        # Keep UPL only as a diagnostic hint — never as source='broker'.
        snap=self.store.get('last_upl:'+str(deal_id)) or {}
        upl=_num(snap.get('upl'))
        if upl is not None and math.isfinite(upl):
            self.store.event('trade','Broker cash P&L still pending; UPL not used as realized',
                             {'deal_id':deal_id,'last_upl':float(upl),
                              'currency':self._cash_currency(snap.get('currency'))})
        return None,'pending_broker_pnl',ccy

    def _trade_already_learned(self,deal_id):
        """True when this deal's outcome already trained the LIVE core (audit #2)."""
        meta=self.store.get('learned_trade:'+str(deal_id))
        return bool(isinstance(meta,dict) and meta.get('learned'))

    def _mark_trade_learned(self,deal_id,*,mode=None,pnl=None,source=None):
        self.store.set('learned_trade:'+str(deal_id),{
            'learned':True,'at':now(),'mode':mode,'pnl':pnl,'source':source})

    def _queue_pending_pnl(self,deal_id,*,exit_level=None):
        pending=list(self.store.get('pending_broker_pnl_deals') or [])
        deal=str(deal_id)
        if deal not in [str(x.get('deal_id') if isinstance(x,dict) else x) for x in pending]:
            pending.append({'deal_id':deal,'exit_level':exit_level,'queued_at':now()})
            self.store.set('pending_broker_pnl_deals',pending[-40:])

    async def _retry_pending_broker_pnl(self):
        """Re-request pending cash P&L; refine records; learn at most once (audit #1–2)."""
        pending=list(self.store.get('pending_broker_pnl_deals') or [])
        if not pending or not self.broker:return
        remaining=[]
        for item in pending:
            deal=str(item.get('deal_id') if isinstance(item,dict) else item)
            queued=item.get('queued_at') if isinstance(item,dict) else None
            # Pass queued_at so history lookback is not stuck on last 30 minutes.
            found=await self._pnl_from_broker_history(deal,queued_at=queued)
            if not found:
                remaining.append(item if isinstance(item,dict) else {'deal_id':deal})
                continue
            pnl,hist_ccy=found
            ccy=self._cash_currency(hist_ccy)
            exit_level=item.get('exit_level') if isinstance(item,dict) else None
            already=self._trade_already_learned(deal)
            self.store.close_trade(deal,exit_level,pnl,currency=ccy,source='broker',overwrite=True)
            if already:
                self.store.event('trade','Late broker P&L refined record without re-learning',
                                 {'deal_id':deal,'pnl':pnl,'pnl_currency':ccy})
            else:
                conf={'level':exit_level,'profit':pnl,'pnl_currency':ccy,'source':'pending_pnl_retry'}
                self._learn_closed_trade(deal,conf)
            self.store.set('last_upl:'+deal,None)
        self.store.set('pending_broker_pnl_deals',remaining)
        # Flat book: also harvest Capital cash for open SQLite rows whose deal vanished
        # before we queued pending (restart / missed sync).
        if not self.positions:
            await self._reconcile_orphan_open_trades()

    async def _reconcile_orphan_open_trades(self):
        """When flat, close SQLite rows still open if Capital history has cash P&L."""
        if self.positions or not self.broker:return
        try:
            rows=self.store.db.execute(
                'SELECT deal_id,exit FROM trades WHERE closed IS NULL '
                'ORDER BY opened DESC LIMIT 20').fetchall()
        except Exception:
            return
        for deal_id,exit_lvl in rows:
            deal=str(deal_id)
            found=await self._pnl_from_broker_history(deal)
            if not found:continue
            pnl,ccy=found
            ccy=self._cash_currency(ccy)
            conf={'level':_num(exit_lvl),'profit':pnl,'pnl_currency':ccy,
                  'source':'orphan_history_reconcile'}
            if self._trade_already_learned(deal):
                self.store.close_trade(deal,_num(exit_lvl),pnl,currency=ccy,
                                      source='broker',overwrite=True)
            else:
                self._learn_closed_trade(deal,conf)
            self.store.event('reconcile','Orphan open trade closed from Capital history',
                             {'deal_id':deal,'pnl':pnl,'pnl_currency':ccy})
            self.last_closed_step=max(self.last_closed_step,self.brain.core.steps)

    def _counterfactual_context(self,deal_id,saved,*,epic,entry,exit_level,tick,spread):
        """Bars + SL/TP for path counterfactual (audit #15); empty → endpoint CF."""
        stop_d=_num((saved or {}).get('stop_distance'))
        profit_d=_num((saved or {}).get('profit_distance'))
        prot=self.store.get('adaptive_protection:'+str(deal_id)) or {}
        if stop_d is None:stop_d=_num(prot.get('original_risk'))
        if profit_d is None:profit_d=_num(prot.get('original_profit'))
        if stop_d is None or profit_d is None:
            try:
                row=self.store.db.execute(
                    "SELECT payload FROM orders WHERE kind='OPEN' AND status!='UNCERTAIN' "
                    "ORDER BY id DESC LIMIT 40").fetchall()
                for (payload,) in row or ():
                    try:data=json.loads(payload) if isinstance(payload,str) else (payload or {})
                    except (TypeError,ValueError,json.JSONDecodeError):continue
                    if data.get('epic') and epic and data.get('epic')!=epic:continue
                    if stop_d is None:stop_d=_num(data.get('stop_distance'))
                    if profit_d is None:profit_d=_num(data.get('profit_distance'))
                    if stop_d is not None and profit_d is not None:break
            except Exception:
                pass
        min_d=_num((self.market or {}).get('min_distance'))
        bars=[]
        bar_time=(saved or {}).get('bar_time')
        try:
            candles=self.store.candles(epic,n=190,historical_only=True) if epic else []
        except Exception:
            candles=[]
        if candles:
            started=False
            for c in candles:
                at=c.get('at')
                if bar_time and not started:
                    if at is None or str(at)<str(bar_time):continue
                    started=True
                elif not bar_time and not started:
                    tr=self.store.db.execute(
                        'SELECT opened FROM trades WHERE deal_id=?',(deal_id,)).fetchone()
                    opened=(tr[0] if tr else None)
                    if opened and at and str(at)<str(opened)[:19]:
                        continue
                    started=True
                if not started:continue
                mid=_num(c.get('close'));hi=_num(c.get('high'));lo=_num(c.get('low'))
                if mid is None:continue
                bars.append({'mid':mid,'high':hi if hi is not None else mid,
                             'low':lo if lo is not None else mid,
                             'spread':_num(c.get('spread')) if _num(c.get('spread')) is not None else spread,
                             'at':at})
            if len(bars)>120:bars=bars[:120]
        # No invented 2-point path — without OHLC, profit_learning falls back to
        # endpoint mid delta (still using shared one-RT cost rules).
        return {
            'bars':bars or None,
            'stop_distance':stop_d,
            'profit_distance':profit_d,
            'tick':tick,
            'min_distance':min_d,
            'trail':True,
        }

    def _learn_closed_trade(self,deal_id,confirmation):
        """Immediate self-evaluation from a closed trade's signed outcome.

        When the trade is a LOSS, the entry-time neural state is trained toward
        the opposite action (regret). Wins reinforce. Cash P&L is stored only
        from broker profit (+ account currency). Without broker profit, learning
        still uses directional ticks — History cash stays blank rather than fake $.
        Each deal trains LIVE weights at most once (audit #2).
        """
        key='entry_brain:'+str(deal_id)
        saved=self.store.get(key)
        exit_level=_num(confirmation.get('level'))
        broker_pnl=_num(confirmation.get('profit'))
        if broker_pnl is None:broker_pnl=_num(confirmation.get('pnl'))
        hint_ccy=(confirmation.get('pnl_currency') or confirmation.get('currency') or '').strip().upper() or None
        # Allow overwrite when we finally have broker cash after a vanish guess.
        overwrite=(confirmation.get('source')!='position_disappeared') or broker_pnl is not None
        already_learned=self._trade_already_learned(deal_id)
        # Late broker cash may refine the journal without repeating training.
        if already_learned:
            if broker_pnl is not None:
                currency=hint_ccy or self._account_currency()
                self.store.close_trade(deal_id,exit_level,broker_pnl,currency=currency,
                                       source='broker',overwrite=True)
                self.store.event('learning','Late broker P&L refined; neural learn skipped (once-only)',
                                 {'deal_id':deal_id,'pnl':broker_pnl})
            return
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
        # Audit #15: path CF with same SL/TP/trail/costs (not entry→exit delta alone).
        cf=self._counterfactual_context(deal_id,saved,epic=epic,entry=entry,
                                        exit_level=exit_level,tick=tick,
                                        spread=spread_cost)
        # Profit ledger: ONLY broker-confirmed £ — never ±1 tick fakes.
        if pnl_source=='broker' and pnl_value is not None:
            decision=self.profit_learning.record_closed(
                deal_id=deal_id,epic=epic,direction=direction,entry=entry,
                exit_level=exit_level,broker_pnl=pnl_value,
                currency=currency,spread=spread_cost,flip_after_loss=flip_after_loss,
                model_version=getattr(self.brain,'model_version',0),
                feature_schema=getattr(self.brain,'feature_schema',None),
                shadow_action=shadow_action,require_broker_pnl=True,**cf)
            label=decision['label'];mode=decision['mode']
            if label is not None:label=int(label)
        else:
            # Tick path: same rule as £ — wins reinforce side; else counterfactual.
            from .profit_learning import best_action_from_counterfactual
            sim=best_action_from_counterfactual(entry,exit_level,spread=spread_cost,**cf)
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
                shadow_action=shadow_action,require_broker_pnl=True,**cf)
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
        # Audit #17: trade-PnL via AdaptiveBrain.learn_from_trade — separate
        # counters/weights from minute-direction; does not bump minute labeled.
        for i in range(repeats):
            step_loss=self.brain.learn_from_trade(
                transition,label,lr=learn_lr,won=pnl_sign>0,count_sample=(i==0))
            if step_loss is not None:losses.append(step_loss)
        # Independent shadow core trains on same label using *its* decision-time h/c.
        if repeats>0:
            self._shadow_learn(None,label,lr=learn_lr)
        if pnl_source=='broker' and pnl_value is not None:
            # Queue evidence only — commit when long-horizon £ proves improvement.
            evolve_from_feedback(self.store,broker_pnl=pnl_value,commit=False)
            from .self_params import maybe_commit_long_horizon
            maybe_commit_long_horizon(self.store,self.profit_learning.broker_pnls())
        if repeats>0 and transition.get('h') is not None and transition.get('c') is not None:
            self.brain.core.update_utility(transition['h'],transition['c'],label,
                neuron_ids=transition.get('neuron_ids'))
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
            # Audit #19: pass exit reason + path/vol/costs — never P&L alone.
            path_ctx=self._bracket_path_context(deal_id,confirmation,saved)
            bracket_learn.record_outcome(
                self.store,kappa=bk,sl_scale=bs,pnl=pnl_value,
                currency=currency,deal_id=deal_id,**path_ctx)
            # Audit #21: operator daily loss / drawdown halt from realized cash.
            hard_risk.record_realized_pnl(self.store,pnl_value)
            equity=self._account_equity()
            if equity is not None:
                hard_risk.check_drawdown_halt(self.store,equity)
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
        # Audit #2: this deal's experience may train LIVE only once.
        self._mark_trade_learned(deal_id,mode=mode,pnl=pnl_value,source=pnl_source)
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
        stop=proposal['stop_level']
        # Always re-send live TP on trail. Omitting profitLevel on Capital PUT
        # turns Take Profit Off — TP mark can be touched with no fill left.
        tp=proposal.get('profit_level')
        if tp is None:tp=_num(p.get('profitLevel'))
        # Audit #9: skip trail PUT when SL/TP would violate broker min distance.
        if not levels_meet_broker_min(p.get('direction'),stop,tp,
                                      self.market.get('bid'),self.market.get('ask'),
                                      self.market.get('min_distance')):
            self.store.event('warning','Trail skipped: SL/TP below broker min distance',{
                'deal_id':p.get('dealId'),'stop':stop,'profit':tp,
                'min_distance':self.market.get('min_distance'),
                'tick':self.market.get('tick'),'bid':self.market.get('bid'),
                'ask':self.market.get('ask')})
            self.protection_brief={'status':'Trail skipped: SL/TP below broker min',
                                   'deal_id':p['dealId'],'proposal':proposal}
            return
        oid=self.store.order('PROTECT',p['dealId'],{'stop':stop,'profit':tp,
            'tick':self.market['tick'],'reason':'BROKER_TRAIL','extreme':extreme,
            'min_distance':self.market.get('min_distance'),'preserve_tp':tp is not None})
        try:
            receipt=await self.broker.update_position(p['dealId'],stop_level=stop,profit_level=tp)
            if await self._confirm_reconcile(receipt,oid,'PROTECT',p['dealId']):
                self.store.set('adaptive_runtime:'+str(p['dealId']),{
                    'mono':now_mono,'stop':stop,'profit':tp,'extreme':extreme,'status':'CONFIRMED',
                    'mark':mark})
                self.protection_brief['status']='Broker-confirmed trail update'
                self.store.event('protection','Trail SL verified by broker',{
                    'deal_id':p['dealId'],'new_stop':stop,'profit_level':tp,'extreme':extreme,
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
        # Audit #9: refuse protect PUT when computed SL/TP still below broker min.
        if not distances_meet_broker_min(result['stop_distance'],result['profit_distance'],min_dist):
            self.store.event('warning','Protection skipped: SL/TP below broker min distance',{
                'deal_id':p.get('dealId'),
                'stop_distance':float(result['stop_distance']),
                'profit_distance':float(result['profit_distance']),
                'min_distance':float(min_dist),'tick':float(tick),
                'kappa':bp['kappa'],'sl_scale':bp['sl_scale']})
            return
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
        # After fusion transition: never open on a stale signal during EXTERNAL gap.
        if self._fusion_entry_blocked():return
        if not self._external_entry_check(action)['entry_allowed']:return
        # Audit #22–#23: prep/eval readiness (and utility via evaluate_neural_entry).
        gates=self.live_open_gates(action)
        if not gates.get('allowed'):
            self.last_entry_reason=gates.get('reason') or 'LIVE OPEN blocked by readiness'
            return
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
            # Audit #9: after coeffs/rounding, refuse non-compliant SL/TP.
            if not distances_meet_broker_min(brackets['stop_distance'],
                                             brackets['profit_distance'],minimum):
                self.store.event('warning','OPEN skipped: SL/TP below broker min distance',{
                    'stop_distance':float(brackets['stop_distance']),
                    'profit_distance':float(brackets['profit_distance']),
                    'min_distance':float(minimum),'kappa':bp['kappa'],
                    'raised_to_broker_min':brackets.get('raised_to_broker_min')})
                return
            # Audit #20/#21: fixed mandate size × SL × tick_value vs operator hard limits.
            # Never auto-shrink size — block OPEN when cash risk exceeds the cap.
            tick_val=tick_value_from_instrument(market.get('instrument') or {},tick)
            proposed=None
            if tick_val is not None:
                try:
                    proposed=float(cash_risk(size,brackets['stop_ticks'],tick_val))
                except (ValueError,TypeError,ArithmeticError):
                    proposed=None
            gate=self._hard_risk_gate(proposed_risk=proposed)
            if not gate.get('allowed'):
                self.last_entry_reason=gate.get('reason') or 'hard risk blocked'
                self.store.event('risk',self.last_entry_reason,{
                    'proposed_risk':proposed,'size':str(size),
                    'stop_ticks':int(brackets['stop_ticks']),
                    'tick_value':float(tick_val) if tick_val is not None else None,
                    'limits':gate.get('limits'),'direction':action})
                return
            self.store.event('bracket','OHLC zone × learned raise/cut at open',{
                'kappa':bp['kappa'],'sl_scale':bp['sl_scale'],
                'kappa_mult':bp.get('kappa_mult'),'sl_mult':bp.get('sl_mult'),
                'median_up':bp.get('median_up'),'median_down':bp.get('median_down'),
                'lookback':bp.get('lookback') or zone.get('lookback'),
                'base_sl':base_sl,'max_tp':zone.get('max_tp'),
                'stop_distance':float(brackets['stop_distance']),
                'profit_distance':float(brackets['profit_distance']),
                'raised_to_broker_min':bool(brackets.get('raised_to_broker_min')),
                'cash_risk':proposed,
                'tick_value':float(tick_val) if tick_val is not None else None,
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
                    'stop_distance':float(brackets['stop_distance']),
                    'profit_distance':float(brackets['profit_distance']),
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
        readiness=(self.brain.trading_readiness()
                   if hasattr(self.brain,'trading_readiness') else {'ready':False})
        minute_acc=(self.brain.correct/self.brain.labeled) if self.brain.labeled else None
        # Closed-trade win rate is separate from minute-direction accuracy (audit #24).
        trade_wins=trade_losses=0
        try:
            for t in self.store.trades(200):
                if not t.get('closed') or t.get('pnl') is None:continue
                src=(t.get('pnl_source') or '').strip().lower()
                if src in ('ticks_only','price_size','pending_close_sync','position_disappeared'):
                    continue
                try:val=float(t['pnl'])
                except (TypeError,ValueError):continue
                if val>0:trade_wins+=1
                elif val<0:trade_losses+=1
        except Exception:
            pass
        trade_n=trade_wins+trade_losses
        trade_win_rate=(trade_wins/trade_n) if trade_n else None
        # In a position neural_decision may report CLOSE — diagnostic only (audit #25).
        raw_exit_intent=None
        if self.positions and self.last_signal:
            raw_exit_intent=self.policy.neural_decision(
                self.last_signal['scores'],self.positions[0]['direction'])
        decision=(raw_exit_intent if self.positions else
                  (evaluation['action'] if evaluation else 'HOLD'))
        gates=self.live_open_gates(evaluation['action'] if evaluation else None)
        return {'connected':bool(self.broker),'armed':self.armed,'execution_enabled':self.execution_enabled,
                'mandate':self.mandate,'data_dir':str(self.store.path.parent),
                'positions':self.positions,'market':self.market,'signal':self.last_signal,
                'neural':dict(self.brain.core.snapshot(),feature_schema=getattr(self.brain,'feature_schema',None),
                    input_dimension=self.brain.core.d,
                    external_features_learned=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                        and getattr(self.brain,'fusion_schema',None) is not None),
                    external_feed_ready=self.latest_fusion_ready,
                    fusion_block_reason=self.fusion_block_reason,
                    external_trained_labels=getattr(self.brain,'fusion_labels',0),
                    model_version=getattr(self.brain,'model_version',0),
                    ohlc_dim=getattr(self.brain,'ohlc_dim',None),
                    fusion_dim=getattr(self.brain,'fusion_dim',None),
                    feature_registry=self.feature_registry.snapshot()),
                # Audit #24: never merge minute-direction accuracy with trade win rate.
                'minute_direction_accuracy':minute_acc,
                'accuracy_proxy':minute_acc if minute_acc is not None else 0.,
                'trade_win_rate':trade_win_rate,
                'trade_wins':trade_wins,'trade_losses':trade_losses,
                'model_score_is_calibrated_profit_probability':False,
                'metric_labels':{
                    'minute_direction_accuracy':'M1 direction label hit-rate (not profit)',
                    'trade_win_rate':'Closed-trade win rate after broker cash settle',
                    'model_score':'Uncalibrated softmax score (not profit probability)',
                },
                'labeled_samples':self.brain.labeled,'market_regime':self.brain.last_regime,
                'model_readiness':readiness,
                'prep_eval':readiness,
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
                'directional_advantage_is_trading_edge':False,
                'directional_validation':evaluation.get('evidence') if evaluation else None,
                'economic_utility':(evaluation or {}).get('economic_utility'),
                'adaptive_threshold':None,
                'policy_base_threshold':None,
                'entry_margin_calibration':evaluation.get('calibration') if evaluation else None,
                'threshold_unit':'neural_argmax_no_zone_hardcode',
                'neural_decision_bar':self.last_decision_bar,
                'neural_entry_rule':'neural argmax BUY/SELL/HOLD; readiness+utility separate; zone/SHIFT evidence only — not entry bans',
                # Audit #25: neural CLOSE is diagnostic; live exit = broker SL/TP/trail.
                'neural_market_exit':False,
                'neural_close_diagnostic_only':True,
                'neural_close_executable':False,
                'neural_exit_intent':raw_exit_intent,
                'neural_exit_rule':'diagnostic CLOSE intent only — broker SL/TP/trail executes closes; neural market-delete is not a strategy',
                'entry_eligible':bool(self.market_context.get('ready') and self.mandate and not self.positions and self.last_closed_step>=0 and self.armed and self.execution_enabled and not self.store.unresolved() and readiness.get('ready')),
                'entry_signal_validated':bool(evaluation and evaluation.get('action') in ('BUY','SELL')),
                'entry_decision_pending_new_bar':bool(self.last_decision_bar==self.last_entry_processed_bar),
                'entry_policy_reason':evaluation.get('reason') if evaluation else None,
                'entry_independent_samples':(evaluation.get('evidence') or {}).get('samples') if evaluation else None,
                'entry_can_submit_now':bool(evaluation and evaluation.get('action') in ('BUY','SELL') and
                    gates.get('allowed') and
                    self.market.get('status')=='TRADEABLE' and self.market_context.get('ready') and
                    self.mandate and not self.positions and self.last_closed_step>=0 and self.armed and
                    self.execution_enabled and not self.store.unresolved() and
                    not self._fusion_entry_blocked() and
                    self._external_entry_check(evaluation['action'])['entry_allowed'] and
                    self.brain.core.steps>max(self.last_closed_step,self.last_entry_signal_step) and
                    self.last_decision_bar is not None and
                    self.last_entry_processed_bar!=self.last_decision_bar),
                'fusion_block_reason':self.fusion_block_reason,
                # Audit #14: when fusion EXTERNAL data blocks, name that source
                # before the generic prep/eval readiness message.
                'entry_block_reason':('Execution paused' if not self.armed or not self.execution_enabled else
                    'Unresolved broker operation' if self.store.unresolved() else
                    'Existing open position' if self.positions else
                    'No adoptive size this session — open a new manual trade' if not self.mandate else
                    'Waiting for first manually opened trade to close' if self.last_closed_step<0 else
                    'Market context unavailable: '+self.market_context.get('reason','') if not self.market_context.get('ready') else
                    ('External fusion gap blocks new entry: '+(self.fusion_block_reason or 'aligned external neural inputs unavailable'))
                        if self._fusion_entry_blocked() else
                    ('Model prep/eval not ready: '+(readiness.get('reason') or ''))
                        if not readiness.get('ready') else
                    # Prefer the real HOLD/skip reason over a cosmetic M1-wait mask
                    # when this bar was already evaluated (utility/news/steps).
                    (self.last_entry_reason
                     if (self.last_entry_processed_bar==self.last_decision_bar
                         and self.last_decision_bar is not None
                         and getattr(self,'last_policy_decision',None)=='HOLD'
                         and self.last_entry_reason)
                     else 'Waiting for next completed M1 candle'
                     if self.last_entry_processed_bar==self.last_decision_bar
                     and self.last_decision_bar is not None else
                    self._signal_reason(decision,self.market.get('status'),evaluation) if self.last_signal else
                    'Waiting for signal')),
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

    SHADOW_STATE_KEY='shadow_neural_state_v1'

    def _ensure_shadow_brain(self):
        """Independent shadow NeuralCore — same input dim, own weights (never LIVE)."""
        d=self.brain.core.d
        created=False
        if self.shadow_brain is None or self.shadow_brain.d!=d:
            alt_n=max(8,self.brain.core.n+4 if self.brain.core.n<48 else max(8,self.brain.core.n-4))
            # Derive from live init_seed so shadow search is not identical across clients.
            base=int(getattr(self.brain,'init_seed',7) or 7)
            shadow_seed=(base^(0xA5A5+alt_n))%(2**31-1) or (base+alt_n+1)
            saved=self.store.get(self.SHADOW_STATE_KEY) or {}
            try:saved_n=int(saved.get('n') or 0)
            except (TypeError,ValueError):saved_n=0
            try:saved_d=int(saved.get('d') or -1)
            except (TypeError,ValueError):saved_d=-1
            use_n=saved_n if (saved_n>=8 and saved_d==d) else alt_n
            self.shadow_brain=self.brain.core.clone_architecture(
                n=use_n,seed=shadow_seed,copy_weights=False)
            self.shadow_brain.model_version=0
            created=True
            self.store.event('shadow','Independent shadow neural core ready',
                {'neurons':self.shadow_brain.n,'dim':d,'live_neurons':self.brain.core.n,
                 'shadow_seed':shadow_seed})
        if created:
            self._restore_shadow_state()
        return self.shadow_brain

    def _restore_shadow_state(self):
        """Reload persisted shadow h/c + step counter after create/restart."""
        shadow=self.shadow_brain
        raw=self.store.get(self.SHADOW_STATE_KEY) or {}
        if not isinstance(raw,dict) or shadow is None:return False
        try:
            h=np.asarray(raw.get('h'),dtype=float)
            c=np.asarray(raw.get('c'),dtype=float)
        except (TypeError,ValueError):
            return False
        if h.shape!=(shadow.n,) or c.shape!=(shadow.n,):return False
        if not (np.isfinite(h).all() and np.isfinite(c).all()):return False
        if int(raw.get('d') or -1)!=shadow.d:return False
        shadow.h=h;shadow.c=c
        try:shadow.steps=int(raw.get('steps') or 0)
        except (TypeError,ValueError):shadow.steps=0
        try:shadow.model_version=int(raw.get('model_version') or 0)
        except (TypeError,ValueError):shadow.model_version=0
        tr=raw.get('last_transition')
        if isinstance(tr,dict) and tr.get('z') is not None:
            try:
                z=np.asarray(tr['z'],dtype=float)
                th=np.asarray(tr['h'],dtype=float);tc=np.asarray(tr['c'],dtype=float)
                if z.shape==(shadow.d,) and th.shape==(shadow.n,) and tc.shape==(shadow.n,):
                    ids=tr.get('neuron_ids')
                    shadow.last_transition={
                        'z':z,'h':th,'c':tc,
                        'dt':float(tr.get('dt') or 1.),
                        'neuron_ids':(np.asarray(ids,dtype=np.int64)
                                      if ids is not None else shadow.neuron_ids.copy()),
                        'bar_time':tr.get('bar_time')}
            except (TypeError,ValueError,KeyError):
                pass
        return True

    def _persist_shadow_state(self):
        """Persist shadow h/c and step counter after every completed-candle advance."""
        shadow=self.shadow_brain
        if shadow is None:return
        tr=getattr(shadow,'last_transition',None)
        payload={
            'h':np.asarray(shadow.h,dtype=float).tolist(),
            'c':np.asarray(shadow.c,dtype=float).tolist(),
            'steps':int(getattr(shadow,'steps',0) or 0),
            'n':int(shadow.n),'d':int(shadow.d),
            'model_version':int(getattr(shadow,'model_version',0) or 0),
            'neuron_ids':np.asarray(shadow.neuron_ids,dtype=np.int64).tolist(),
        }
        if isinstance(tr,dict) and tr.get('z') is not None:
            payload['last_transition']={
                'z':np.asarray(tr['z'],dtype=float).tolist(),
                'h':np.asarray(tr['h'],dtype=float).tolist(),
                'c':np.asarray(tr['c'],dtype=float).tolist(),
                'dt':float(tr.get('dt') or 1.),
                'neuron_ids':np.asarray(
                    tr.get('neuron_ids',shadow.neuron_ids),dtype=np.int64).tolist(),
                'bar_time':tr.get('bar_time')}
        self.store.set(self.SHADOW_STATE_KEY,payload)

    def _shadow_decision_transition(self, preferred=None):
        """Shadow's own decision-time state — never LIVE h/c."""
        shadow=self._ensure_shadow_brain()
        for cand in (preferred,getattr(shadow,'last_transition',None)):
            if not isinstance(cand,dict):continue
            z=cand.get('z')
            if z is None:continue
            z=np.asarray(z,dtype=float)
            if z.shape!=(shadow.d,):continue
            h=np.asarray(cand.get('h'),dtype=float)
            c=np.asarray(cand.get('c'),dtype=float)
            if h.shape!=(shadow.n,) or c.shape!=(shadow.n,):continue
            ids=cand.get('neuron_ids',shadow.neuron_ids)
            return {'z':z,'h':h,'c':c,'dt':float(cand.get('dt') or 1.),
                    'neuron_ids':np.asarray(ids,dtype=np.int64)}
        return None

    def _shadow_learn(self,transition,label,lr=None):
        """Train shadow on *its* decision-time h/c — never LIVE recurrent state."""
        shadow=self._ensure_shadow_brain()
        if label not in (0,1,2):return None
        own=self._shadow_decision_transition(
            transition if (isinstance(transition,dict)
                           and transition.get('h') is not None
                           and len(np.asarray(transition.get('h')))==shadow.n)
            else None)
        if own is None and isinstance(transition,dict):
            base=getattr(shadow,'last_transition',None)
            if isinstance(base,dict) and base.get('h') is not None:
                z=transition.get('z')
                if z is not None and len(np.asarray(z))==shadow.d:
                    own={'z':np.asarray(z,dtype=float),
                         'h':np.asarray(base['h'],dtype=float),
                         'c':np.asarray(base['c'],dtype=float),
                         'dt':float(base.get('dt') or transition.get('dt') or 1.),
                         'neuron_ids':np.asarray(
                             base.get('neuron_ids',shadow.neuron_ids),dtype=np.int64)}
        if own is None:return None
        from .core import OBJECTIVE_TRADE_PNL
        loss=shadow.learn(own,label,lr=lr,objective=OBJECTIVE_TRADE_PNL)
        self._persist_shadow_state()
        return loss

    def _shadow_scores_now(self,*,advance=False,bar_time=None):
        """Score with shadow weights on live z; optionally advance+persist h/c.

        advance=True on each completed M1 candle so shadow is a real parallel
        trajectory with its own decision-time state (not a LIVE h/c snapshot).
        """
        shadow=self._ensure_shadow_brain()
        z=self.brain.core.last_z
        if z is None or len(z)!=shadow.d:return None
        try:
            z=np.asarray(z,dtype=float)
            h0=shadow.h.copy();c0=shadow.c.copy()
            hn,cn,logits=shadow._forward(z,h0,c0,1.0)
            if advance:
                shadow.last_transition={
                    'z':z.copy(),'h':h0,'c':c0,'dt':1.0,
                    'neuron_ids':shadow.neuron_ids.copy(),
                    'bar_time':bar_time}
                shadow.h=hn;shadow.c=cn
                shadow.last_z=z.copy()
                shadow.steps=int(getattr(shadow,'steps',0) or 0)+1
                self._persist_shadow_state()
            prob=shadow._softmax(logits)
            return dict(zip(('BUY','SELL','HOLD'),map(float,prob)))
        except Exception:
            return None

    def _resource_pressure(self):
        """Lightweight host pressure probe — skip heavy self-dev when saturated."""
        try:
            load1,_,_=os.getloadavg()
        except (AttributeError,OSError):
            load1=0.
        try:
            # ru_maxrss is KiB on Linux.
            rss_mb=float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)/1024.0
        except Exception:
            rss_mb=0.
        cpus=os.cpu_count() or 1
        high_load=load1>=max(2.0,0.85*cpus)
        high_rss=rss_mb>=2048  # soft ceiling for discovery/validation work
        return {'load1':load1,'rss_mb':rss_mb,'cpus':cpus,
                'busy':bool(high_load or high_rss),
                'high_load':high_load,'high_rss':high_rss}

    def _run_self_development_cycle(self,candles,*,spread=0.,defer_if_busy=False):
        """Discover → validate → expand (decision-time data only). Never blocks orders.

        When defer_if_busy (open-position path), skip under host pressure so
        trail/protect polls stay responsive (audit #32).
        """
        from .historical_features import build_feature_matrix
        bar=self.market_context.get('bar_time')
        if not bar or bar==self._last_self_dev_bar:return
        pressure=self._resource_pressure()
        if defer_if_busy and pressure.get('busy'):
            self.store.event('self_dev','Self-dev deferred — open-position protect priority',
                {'bar_time':bar,**{k:pressure[k] for k in ('load1','rss_mb','high_load','high_rss')}})
            return
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
        # Advance+persist shadow h/c/steps on this completed candle first.
        last_bar_ohlc=window[-1] if window else None
        self._shadow_paper_step(
            mid=self.market_context.get('close'),
            spread=last_sp,bar_time=bar,
            high=(last_bar_ohlc[1] if last_bar_ohlc else None),
            low=(last_bar_ohlc[2] if last_bar_ohlc else None),
            candles=candles)
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
            from .instrument_isolation import label_backup_meta
            epic=self.mandate['epic'] if self.mandate else getattr(self.brain,'epic',None)
            self.model_validator.push_version(label_backup_meta({
                'model_version':self.brain.model_version,
                'feature_schema':self.brain.feature_schema,
                'ohlc_dim':self.brain.ohlc_dim,
                'fusion_dim':self.brain.fusion_dim,
                'approved':self.feature_registry.approved_ids(),
                'approved_specs':pre_specs,
                'weights_path':pre_path},epic))
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
            self.model_validator.push_version(label_backup_meta({
                'model_version':self.brain.model_version,
                'feature_schema':self.brain.feature_schema,
                'ohlc_dim':self.brain.ohlc_dim,
                'fusion_dim':self.brain.fusion_dim,
                'approved':self.feature_registry.approved_ids(),
                'approved_specs':post_specs,
                'weights_path':post_path},epic))
        self.policy.shadow_self_dev_tick(self)

    def _shadow_paper_step(self,*,mid,spread=0.,bar_time=None,high=None,low=None,
                           candles=None):
        """Advance shadow state + paper journal with LIVE-identical execution rules.

        Votes after advancing shadow h/c on this completed candle. Opens/closes
        via SL/TP/trail (same stop_take + trail math as LIVE) — never flip-exit.
        """
        scores=self._shadow_scores_now(advance=True,bar_time=bar_time)
        if not scores or mid is None:return None
        try:
            action=max(('BUY','SELL','HOLD'),key=lambda a:float(scores.get(a,0)))
        except (TypeError,ValueError):
            return None
        live=getattr(self.profit_learning,'history',[]) or []
        self.shadow_paper.update_cash_scale(live)
        shadow=self.shadow_brain
        shadow_tr=None
        if shadow is not None and getattr(shadow,'last_transition',None):
            shadow_tr=self.brain._json_transition(shadow.last_transition)
        tick=None;minimum=None;stop_d=None;profit_d=None;kappa=None;sl_scale=None
        try:
            mkt=self.market or {}
            tick=_num(mkt.get('tick'))
            minimum=_num(mkt.get('min_distance'))
            if tick is None or tick<=0:
                tick=max(abs(float(spread or 0))/2.,1e-4)
            if minimum is None or minimum<0:
                minimum=float(tick)
            rows=candles if candles is not None else (
                self.store.candles(self.mandate['epic'],n=120,historical_only=True)
                if self.mandate else [])
            zone=bracket_learn.zone_at_open(rows)
            rng=zone['range_distance'] or ohlc_range_distance(rows)
            buffer=max(2*float(tick),2*abs(float(spread or 0)))
            base_sl=max(float(minimum)+buffer,float(rng or 0))
            bp=bracket_learn.propose_for_open(
                self.store,ohlc_kappa=zone.get('kappa'),base_sl=base_sl,
                max_tp=zone.get('max_tp'),candles=rows)
            entry_proxy=float(mid)+abs(float(spread or 0))/2.
            brackets=stop_take(entry_proxy,'BUY',tick,spread,minimum,
                               kappa=bp['kappa'],sl_scale=bp['sl_scale'],
                               range_distance=float(rng) if rng is not None else 0)
            stop_d=float(brackets['stop_distance'])
            profit_d=float(brackets['profit_distance'])
            kappa=bp['kappa'];sl_scale=bp['sl_scale']
        except Exception:
            stop_d=profit_d=None
        closed=self.shadow_paper.on_bar(
            action,mid=mid,spread=spread,bar_time=bar_time,
            cash_scale=self.shadow_paper.cash_scale,
            high=high,low=low,tick=tick,min_distance=minimum,
            stop_distance=stop_d,profit_distance=profit_d,
            kappa=kappa,sl_scale=sl_scale,
            model_version=int(getattr(shadow,'model_version',0) or 0),
            slip_bps=0.,fill_delay_bars=0,
            shadow_transition=shadow_tr)
        if closed is not None and closed.get('exit_reason') in ('sl','tp','trail'):
            pnl=closed.get('paper_pnl')
            if pnl is None:pnl=closed.get('price_pnl')
            label=None
            if pnl is not None:
                try:
                    cash=float(pnl)
                    if cash>0:label={'BUY':0,'SELL':1}.get(closed.get('direction'))
                    elif cash<0:label=2
                except (TypeError,ValueError):
                    label=None
            if label is not None:
                self._shadow_learn(closed.get('shadow_transition'),label)
        return closed

    def restore_neural_weights(self, version_meta_or_path):
        """Rollback hook: restore feature registry + neural weights together.

        Accepts a full version_meta dict (preferred) or a bare weights path
        string for older call sites/tests. Fail closed: a corrupt weight file
        must not report success (audit #30).
        """
        if isinstance(version_meta_or_path,dict):
            meta=version_meta_or_path
            path=meta.get('weights_path')
            specs=meta.get('approved_specs')
            meta_epic=meta.get('epic') or meta.get('instrument')
        else:
            meta=None;path=version_meta_or_path;specs=None;meta_epic=None
        if meta_epic and self.brain.epic and str(meta_epic)!=str(self.brain.epic):
            self.store.event('error','Model snapshot refused — instrument mismatch',
                {'backup_epic':meta_epic,'live_epic':self.brain.epic,'path':path})
            return False
        # Registry first so layout matches the weight file about to load.
        if specs is not None:
            self.feature_registry.replace_approved(specs)
        ok=False
        if path and hasattr(self.brain,'restore_weights'):
            ok=bool(self.brain.restore_weights(
                path,registry=self.feature_registry,
                expected_ohlc_dim=(meta or {}).get('ohlc_dim') if isinstance(meta,dict) else None,
                expected_fusion_dim=(meta or {}).get('fusion_dim') if isinstance(meta,dict) else None,
                expected_feature_schema=(meta or {}).get('feature_schema') if isinstance(meta,dict) else None))
        if path and not ok:
            # Weight restore failed — do not claim success via registry-only path.
            self.store.event('error','Model snapshot restore failed closed',
                {'path':path,'registry_rolled':specs is not None})
            return False
        if ok or (specs is not None and not path):
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
                 'model_version':(meta or {}).get('model_version') if isinstance(meta,dict) else None,
                 'epic':self.brain.epic})
            return True
        return False


