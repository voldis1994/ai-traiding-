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
try:
    import resource  # Unix-only; absent on Windows
except ImportError:  # pragma: no cover - Windows
    resource=None
import sqlite3
import time
from collections import deque
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import numpy as np
from .core import NeuralCore
from .autonomy import AutoEvolution
from .math_rules import (stop_take, ohlc_range_distance,
                         distances_meet_broker_min, levels_meet_broker_min,
                         cash_risk, tick_value_from_instrument,
                         dealing_size_grid, snap_deal_size, snap_distance,
                         on_deal_size_grid, broker_wire_number)
from . import bracket_learn
from . import hard_risk
from . import unified_cycle
from .protection_ai import propose_broker_trail, trail_extreme_from_broker
from .storage import DATA
from .market_context import market_features, candle_bucket, candle_time, FEATURE_SCHEMA, FEATURE_DIM
from .market_fusion import (
    FUSION_SCHEMA, FUSION_DIM, fusion_features, fusion_dim_for, EXTERNAL_NAMES,
    fusion_gap_reason, fusion_liquidity_tail, fusion_peers_tail, fusion_news_tail,
)
from .feature_registry import FeatureRegistry
from .feature_discovery import FeatureDiscovery
from .feature_validation import (
    FeatureValidator, recommended_live_history_bars, min_bars_for_train_oos_split,
    BROKER_PRICE_HISTORY_MAX,
)
from .profit_learning import ProfitLearning
from .model_validation import ModelValidator
from . import hold_shadow
from . import learning_generation
from . import champion_library
from . import structure_shadow
from . import structure_edge
from . import situation_memory
from . import day_book

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
        try:
            from .self_params import get as sp_get
            online_need=int(sp_get(self.store,'min_online_labeled',64))
        except Exception:
            online_need=64
        if self.labeled < online_need:
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
                # legacy_quote_v2 is a real pre-OHLC schema — allow restore when
                # the live core is still on that layout (or None→legacy).
                live_fs=self.feature_schema or 'legacy_quote_v2'
                if fs not in (None,reg_os,reg_fs,FEATURE_SCHEMA,FUSION_SCHEMA,
                              'legacy_quote_v2',
                              meta.get('ohlc_schema'),meta.get('fusion_schema'),
                              live_fs):
                    # Allow exact match to schemas stored in the file itself.
                    if fs not in (meta.get('ohlc_schema'),meta.get('fusion_schema'),live_fs):
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
        prev_ohlc_dim,prev_fus_dim=self.ohlc_dim,self.fusion_dim
        prev_ohlc_s,prev_fus_s=self.ohlc_schema,self.fusion_schema
        ohlc_d=registry.ohlc_dim();fus_d=registry.fusion_dim()
        ohlc_s=registry.ohlc_schema();fus_s=registry.fusion_schema()
        grew=ohlc_d>self.ohlc_dim or fus_d>self.fusion_dim
        if self.feature_schema in (None,'legacy_quote_v2'):
            self.ohlc_dim=ohlc_d;self.fusion_dim=fus_d
            self.ohlc_schema=ohlc_s;self.fusion_schema=fus_s
            return False
        fs=self.feature_schema or ''
        # Resolve target from *proposed* registry dims before mutating self.
        on_fusion=(fs==fus_s or fs==FUSION_SCHEMA or 'intermarket_news' in fs
                   or fs==prev_fus_s)
        if (fs==ohlc_s or fs==FEATURE_SCHEMA or fs==prev_ohlc_s or
                (FEATURE_SCHEMA in fs and 'intermarket_news' not in fs)):
            target=ohlc_d;schema=ohlc_s;insert_at=None
        elif on_fusion:
            target=fus_d;schema=fus_s
            insert_at=old_ohlc if ohlc_d>old_ohlc else None
        else:
            return False
        if self.core.d==target and self.feature_schema==schema:
            self.ohlc_dim=ohlc_d;self.fusion_dim=fus_d
            self.ohlc_schema=ohlc_s;self.fusion_schema=fus_s
            return False
        if self.core.d>target:
            # Registry wiped DYNAMIC features but brain.npz stayed wide — that
            # used to skip_shrink and freeze LIVE with fusion gap 13!=14.
            # Drop DYNAMIC columns; keep BASE (+ EXTERNAL on fusion) weights.
            # Fusion layout: remove at new OHLC end (before EXTERNAL block).
            # OHLC-only: remove trailing DYNAMIC columns at `target`.
            remove_at=ohlc_d if on_fusion else target
            try:
                self.core.shrink_inputs(target,remove_at=remove_at)
            except (ValueError,AttributeError) as exc:
                self.store.event('warning',
                    'Feature registry smaller than neural inputs; shrink failed — keeping weights',
                    {'epic':self.epic,'brain_inputs':self.core.d,'registry_inputs':target,
                     'schema':schema,'error':str(exc)[:160],'action':'skip_shrink'})
                self.ohlc_dim=prev_ohlc_dim;self.fusion_dim=prev_fus_dim
                self.ohlc_schema=prev_ohlc_s;self.fusion_schema=prev_fus_s
                return False
            self.ohlc_dim=ohlc_d;self.fusion_dim=fus_d
            self.ohlc_schema=ohlc_s;self.fusion_schema=fus_s
            self.feature_schema=schema
            self.pending.clear()
            self.core.last_transition=None
            self.model_version+=1
            self.store.event('learning',
                'Neural inputs shrunk to match FeatureRegistry (dropped DYNAMIC columns)',
                {'epic':self.epic,'inputs':target,'schema':schema,
                 'remove_at':remove_at,'layout':'base|dynamic|external',
                 'model_version':self.model_version})
            self.save()
            return False
        self.ohlc_dim=ohlc_d;self.fusion_dim=fus_d
        self.ohlc_schema=ohlc_s;self.fusion_schema=fus_s
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

    def tick(self,mid,spread,tick_size,epic,dt=1.,features=None,bar_time=None,
             external_features=None,in_position=False):
        """One M1 step. ``in_position`` freezes entry-policy teachers.

        While a LIVE deal is open the brain may still *perceive* (step + audit
        signals), but must NOT:
          • train minute-direction into LIVE entry weights
          • enqueue pending that later backfills M1 learns after close
          • grow/prune the entry network mid-trade
        That is a different MDP (manage vs open) — off-policy contamination.
        """
        if (not isinstance(epic,str) or not epic or
            not all(map(math.isfinite,(mid,spread,tick_size))) or
            mid<=0 or tick_size<=0 or spread<0):return None
        self._activate(epic)
        train_entry=not bool(in_position)
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
        # In-trade: audit-label only — never ΔW on the entry policy.
        if len(self.pending)>=self.HORIZON:
            past=self.pending.popleft()
            if past['epic']==epic:
                label=self._label(float(past['mid']),mid,float(past['spread']),spread)
                transition=past.get('transition')
                if train_entry and transition is not None:
                    self.core.update_utility(past['h'],past['c'],label,
                        neuron_ids=past.get('neuron_ids'))
                    from .core import OBJECTIVE_MINUTE_DIRECTION
                    loss=self.core.learn(transition,label,
                                         objective=OBJECTIVE_MINUTE_DIRECTION)
                    if loss is not None and self.feature_schema==self.fusion_schema:
                        self.fusion_labels+=1
                    meta=getattr(self.core,'last_learn_meta',None) or {}
                    real_dw=(loss is not None and not meta.get('restored')
                             and float(meta.get('delta_norm') or 0)>1e-9)
                    if real_dw:
                        # Paaudze only on real ΔW — restored/no-op learns are theater.
                        if (self.minute_labeled+1)%10==0:
                            from . import learning_generation
                            learning_generation.bump(self.store,'m1_horizon_weight_update')
                            self.store.set('neural_adapt_reason',
                                f'M1 label updated LIVE weights (loss={float(loss):.4f})')
                        if (self.minute_labeled+1)%100==0:
                            self.store.event('learning','Horizon-label learning checkpoint',
                                {'loss':loss,'horizon':self.HORIZON,'epic':epic,
                                 'objective':'minute_direction',
                                 'objective_weight':self.MINUTE_OBJECTIVE_WEIGHT,
                                 'proven_improvement':False})
                if train_entry:
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
        # Structural growth is separate from learning; cap + cooldown stop
        # losing clients ballooning (196 vs 55) while Paaudze looks busy.
        # Freeze grow/prune while in-trade — architecture must not move under
        # an open deal (entry_brain z/neuron_ids would misalign at close).
        growth_interval=(self.FAST_GROWTH_INTERVAL if self.core.vbar>2.0
                         else self.GROWTH_INTERVAL)
        from .self_params import get as sp_get
        nmax=int(sp_get(self.store,'max_neurons',64))
        cooldown=int(sp_get(self.store,'birth_cooldown_steps',24))
        last_birth=int(self.store.get('last_neuron_birth_step') or -10**9)
        if train_entry and self.core.steps%growth_interval==0:
            z=np.asarray(result['z'])
            grow_th=float(sp_get(self.store,'grow_error_threshold',.55))
            cooled=(self.core.steps-last_birth)>=cooldown
            if cooled and self.core.grow_if_needed(z,threshold=grow_th,nmax=nmax):
                self.store.set('last_neuron_birth_step',self.core.steps)
                self.store.event('neural','Neuron automatically added',
                    {'neurons':self.core.n,'nmax':nmax})
        # Prune ~2× as often as birth attempts; trim excess over hard cap.
        prune_every=max(16,growth_interval*2)
        if train_entry and self.core.steps%prune_every==0:
            deleted=self.core.prune(
                min_age=int(sp_get(self.store,'prune_min_age',80)),
                threshold=float(sp_get(self.store,'prune_utility_threshold',-.01)))
            trimmed=self.core.prune_to_cap(nmax)
            removed=int(deleted)+int(trimmed)
            if removed:
                self.store.event('neural','Low utility neurons removed',
                    {'count':removed,'pruned':int(deleted),'trimmed_to_cap':int(trimmed),
                     'neurons':self.core.n,'nmax':nmax})
        cur=self.store.db.execute('''INSERT INTO signals
            (time,epic,action,scores,price,model_step,label,spread,tick_size,label_version)
            VALUES(?,?,?,?,?,?,NULL,?,?,?)''',
            (bar_time if features is not None else now(),epic,result['action'],json.dumps(result['scores']),mid,self.core.steps,
             spread,tick_size,self.LABEL_VERSION))
        self.store.db.commit()
        tr=self._json_transition(self.core.last_transition)
        # Only flat bars enter the entry-policy pending queue.
        if train_entry:
            self.pending.append({'epic':epic,'mid':mid,'spread':spread,'tick_size':tick_size,
                'change':change,'bar_time':bar_time,'transition':tr,'h':self.core.h.tolist(),
                'c':self.core.c.tolist(),'neuron_ids':self.core.neuron_ids.tolist(),
                'action':result['action'],'signal_id':cur.lastrowid})
            self.prior_record=self.pending[-1]
        self.history.append({'signal_id':cur.lastrowid,'price':mid,'scores':result['scores'],
            'action':result['action'],'label':None,'time':bar_time if features is not None else now(),
            'spread_bps':10000*spread/mid,'in_position':bool(in_position)})
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
        result['entry_train']=bool(train_entry)
        return result

    def reset_flat_state(self,*,reason='broker_flat'):
        """After close: drop in-trade RNN memory so next OPEN starts flat."""
        self.core.reset_recurrent_flat()
        self.pending.clear()
        self.last_mid=None
        self.last_change=0.
        self.store.event('learning','Recurrent state reset for flat OPEN',
            {'epic':self.epic,'reason':reason,'neurons':self.core.n})
        self.save()

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
        self._open_deal_ids=set()
        # OPEN-time BUY/HOLD/SELL locked for UI while a LIVE deal is managed.
        self._locked_entry_signal=None
        self._last_in_trade_protect_bar=None
        self.position_age=0;self.last_position_id=None;self.last_closed_step=-1
        self.last_entry_signal_step=-1;self.last_order_ref=None
        self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
        self.pending_order=False;self.pending_close=False;self.lock=asyncio.Lock()
        self._pending_entry_brain=None  # neural state at the decision that opened the trade
        # Adopt without a transition yet → bind once on first in-session perception.
        self._adopt_entry_brain_pending=set()
        self.last_time=None;self.last_account_tick=0;self.last_candle_tick=-20
        # Prefer explicit path; AutoEvolution also defaults next to store journal.
        self.policy=AutoEvolution(store,policy_path)
        self.tick_count=0;self.latest_close_id=None
        self.market_context={'ready':False,'reason':'Waiting for complete multi-timeframe candles'}
        self.latest_fusion_ready=False
        self.fusion_block_reason=None  # which EXTERNAL source blocks new fused entry
        # Last good EXTERNAL tails — carry forward when public volume/DOM/peers/news
        # lag the completed M1 so OPEN does not miss the move waiting on the feed.
        self._last_good_liquidity=None
        self._last_good_peers=None
        self._last_good_news=None
        self.last_policy_decision='HOLD';self.last_entry_reason='Awaiting a fresh signal'
        self.last_entry_audit_key=None
        # In-trade perception only (no entry ΔW) — used for reverse CLOSE.
        self._manage_signal=None
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
            try:
                self.brain.apply_registry(self.feature_registry)
            except Exception as exc:
                self.store.event('warning','Registry apply deferred at engine init',
                    {'type':type(exc).__name__,'detail':str(exc)[:160]})
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
        self.mandate=None;self.positions=[];self.last_position_id=None;self._open_deal_ids=set();self.last_closed_step=-1
        self.last_entry_signal_step=-1;self.position_age=0;self.last_time=None
        self.last_decision_bar=None;self.last_entry_processed_bar=None;self.last_exit_processed_bar=None
        self._seen_initial=False;self.protected=set();self.latest_close_id=None
        self._paused_for_multi=False;self._paused_for_mismatch=False;self._paused_for_read=False;self.operator_paused=False
        self.last_error=None;self.account={};self.last_entry_audit_key=None
        self.latest_fusion_ready=False
        self.fusion_block_reason=None
        self._last_good_liquidity=None
        self._last_good_peers=None
        self._last_good_news=None
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
        self._adopt_entry_brain_pending=set()
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
            # Never auto-pause. Keep reconcilable PENDING/UNCERTAIN (with refs)
            # for reconcile_outstanding — do not blind-SUPERSEDE on login.
            # submit() already refuses new OPEN while unresolved rows remain.
            self.store.event('warning',
                'Previous order outcome unresolved at login; staying live (no auto-pause)')
            self._ensure_live_unless_operator_paused()
    def _ensure_live_unless_operator_paused(self):
        """Only explicit operator Pause may freeze OPEN. Auto-pause paths are banned."""
        if self.operator_paused:
            self.armed=False
            self.execution_enabled=False
            return False
        self._paused_for_read=False
        self._paused_for_multi=False
        self._paused_for_mismatch=False
        self._unresolved_at_login=False
        self.armed=True
        self.execution_enabled=True
        return True

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
            self._locked_entry_signal=None;self._last_in_trade_protect_bar=None
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
            deal=p.get('dealId') if p.get('dealId') is not None else p.get('dealID')
            # Deal-less rows are not open positions — keeping them left Admin stuck
            # on "Existing open position" with last_position_id=None (no close path).
            if deal is None or str(deal).strip()=='':continue
            out.append({'dealId':str(deal).strip(),
                'epic':epic,
                'instrument_name':self._extract_instrument_name(p,m),
                'size':p.get('size'),'direction':direction,'level':p.get('level'),
                'stopLevel':p.get('stopLevel'),
                'profitLevel':p.get('profitLevel') if p.get('profitLevel') is not None else p.get('limitLevel'),
                'upl':p.get('upl'),'currency':p.get('currency'),'contractSize':p.get('contractSize')})
        return out

    def _claim_robot_open(self,p,*,reason='reconcile'):
        """Mark live deal as robot-owned and enroll adaptive trail.

        Capital often accepts OPEN without a usable confirm reference → journal
        OPEN stays UNCERTAIN, sync *adopts* the fill as if it were manual, and
        trail never enrolls (no robot_open / adaptive_protection). Any live
        fill that came from our OPEN path must claim trail rights here.
        """
        if not isinstance(p,dict):return False
        deal_id=p.get('dealId')
        if not deal_id:return False
        deal_s=str(deal_id)
        already=bool(self.store.get('robot_open:'+deal_s))
        self.store.set('robot_open:'+deal_s,True)
        self.store.set('latest_robot_deal_id',deal_id)
        # Bind pending submit snapshot if OPEN confirm never matched dealId.
        key='entry_brain:'+deal_s
        if self.store.get(key) is None and self._pending_entry_brain:
            pending=self._pending_entry_brain
            if (pending.get('epic') in (None,p.get('epic')) and
                pending.get('direction') in (None,p.get('direction'))):
                self.store.set(key,{**pending,
                    'entry':(p.get('level') if p.get('level') is not None
                             else pending.get('entry')),
                    'captured':pending.get('captured') or 'claim_robot_open'})
                self._pending_entry_brain=None
        enrolled=self._enroll_adaptive_protection(p,force=True)
        if not already:
            self.store.event('protection',
                'Robot OPEN fill claimed for adaptive trail',
                {'deal_id':deal_s,'reason':reason,'enrolled':bool(enrolled),
                 'epic':p.get('epic'),'direction':p.get('direction')})
        return True

    def _supersede_stale_open_orders(self,live_deal_id):
        """Drop journal OPEN rows once the broker already shows a live deal.

        Stale PENDING/UNCERTAIN OPEN (often without a usable confirm reference)
        otherwise keep execution_enabled=False and used to block mandate adoption,
        leaving the UI on WAITING FOR MOBILE TRADE while UPL already showed the trade.

        When we supersede our own OPEN against a live fill, claim robot_open +
        enroll trail — adopt must not leave protection as manual-only forever.
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
            # Live fill that cleared our OPEN journal → robot trail rights.
            if live_deal_id is not None:
                p=next((x for x in (self.positions or [])
                        if str(x.get('dealId'))==str(live_deal_id)),None)
                if p is not None:
                    self._claim_robot_open(p,reason='stale_open_superseded')
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

        OPEN: epic + direction + size among *all* positions, bound to confirm
        dealId (multi-pos safe). CLOSE: deal gone. PROTECT: dealId + levels
        among all positions (not only positions[0] with len==1).
        """
        if kind=='CLOSE':
            return not any(p.get('dealId')==deal_id for p in self.positions)
        if kind=='PROTECT':
            live=next((p for p in (self.positions or [])
                       if p.get('dealId')==deal_id),None)
            return live is not None and self._protection_matches(live,req or {})
        if kind!='OPEN' or not req or not self.mandate:
            return False
        conf=confirmation or {}
        conf_deal=conf.get('dealId') or conf.get('dealID')
        # Critical #3: OPEN must bind broker-confirmed dealId to a live position.
        if conf_deal is None:
            return False
        matched=None
        for p in (self.positions or []):
            if str(p.get('dealId'))!=str(conf_deal):
                continue
            try:
                size_ok=Decimal(str(p.get('size')))==Decimal(str(req.get('size')))
            except (ValueError,TypeError,ArithmeticError):
                size_ok=False
            epic_ok=(p.get('epic')==self.mandate.get('epic') and
                     (req.get('epic') is None or p.get('epic')==req.get('epic')))
            dir_ok=p.get('direction')==req.get('direction')
            if epic_ok and dir_ok and size_ok:
                matched=p
                break
        if matched is None:
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
            # Critical #3: full match incl. broker dealId — never epic/dir/size alone.
            prev_positions=self.positions
            prev_mandate=self.mandate
            self.positions=positions
            if kind=='OPEN' and not self.mandate and payload.get('epic'):
                self.mandate={'epic':payload.get('epic')}
            try:
                matched=self._order_fully_matched(
                    kind,payload,item.get('deal_id'),confirmed,reference)
            finally:
                self.positions=prev_positions
                self.mandate=prev_mandate
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

    def clear_unresolved_on_operator_resume(self):
        """Admin/client Resume: drop stale PENDING/UNCERTAIN so re-arm is not frozen.

        reconcile_outstanding() already confirms what Capital can prove. Leftover
        rows (common: PROTECT UNCERTAIN after trail TP Off, OPEN without ref)
        used to hard-block Resume forever. Explicit Resume means the operator
        accepts broker state and wants trading armed again.
        """
        leftover=list(self.store.unresolved())
        if not leftover:
            self._unresolved_at_login=False
            return []
        live_ids={str(p.get('dealId')) for p in (self.positions or []) if p.get('dealId')}
        cleared=[]
        for op in leftover:
            kind=op.get('kind')
            deal=str(op.get('deal_id') or '') or None
            ref=op.get('reference')
            if kind=='CLOSE' and deal and deal not in live_ids:
                status='CONFIRMED'
                why='CLOSE already flat on broker at operator resume'
            elif kind=='OPEN' and len(live_ids)==1:
                status='SUPERSEDED'
                why='OPEN journal stale — live broker position already present'
            elif kind=='PROTECT' and deal and deal in live_ids:
                status='SUPERSEDED'
                why='PROTECT uncertain — keep live SL/TP; operator resume accepts broker levels'
            elif kind=='PROTECT' and deal and deal not in live_ids:
                status='SUPERSEDED'
                why='PROTECT for deal no longer open'
            else:
                status='SUPERSEDED'
                why='Stale {0} cleared on operator resume'.format(kind or 'order')
            self.store.order_update(op['id'],status,ref)
            self.store.event('reconcile',why,{
                'order_id':op['id'],'kind':kind,'deal_id':deal,
                'previous_status':op.get('status'),'reference':ref,
                'new_status':status})
            cleared.append({'id':op['id'],'kind':kind,'status':status,'reason':why})
        self._unresolved_at_login=bool(self.store.unresolved())
        self.pending_order=False
        self.pending_close=False
        return cleared

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

    async def _record_vanished_deal(self,prev_id,*,exit_guess=None):
        """Persist close + queue/learn Capital £ when a tracked deal is gone."""
        self.last_closed_step=self.brain.core.steps
        self.latest_close_id=prev_id
        if exit_guess is None:
            exit_guess=_num((self.market or {}).get('mid'))
        # If our CLOSE confirm is in flight, do NOT learn yet — sync() runs
        # before confirm profit/level and would train on mid + clear entry_brain.
        if self.pending_close:
            self.store.close_trade(prev_id,exit_guess,source='pending_close_sync')
            # Still queue cash binding — confirm may fail after vanish; without
            # this, Admin keeps filtering pending_close_sync (null £) forever.
            self._queue_pending_pnl(prev_id,exit_level=exit_guess)
            self.store.event('trade','Deal closed pending broker confirm P&L',
                             {'dealId':prev_id,'exit_guess':exit_guess})
            return None,'pending_close_sync',None
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
        # SL/TP close often races a trail PROTECT PUT → UNCERTAIN + Execution paused.
        # Broker is flat: clear that journal noise and re-arm for the next entry.
        self._release_execution_after_flat(prev_id)
        return pnl,src,ccy

    def _release_execution_after_flat(self,deal_id=None):
        """After broker flat: drop journal leftovers and re-arm for the next OPEN.

        SL/TP fills often race trail PROTECT PUTs (UNCERTAIN) and used to leave
        Execution paused until manual Resume. When Capital is flat we also drop
        stale OPEN rows — leftover `_unresolved_at_login` must not keep freezing
        every close.
        """
        deal=str(deal_id) if deal_id is not None else None
        flat=not self.positions
        cleared=0
        for op in list(self.store.unresolved()):
            kind=op.get('kind')
            op_deal=str(op.get('deal_id') or '') or None
            if kind in ('PROTECT','CLOSE'):
                if deal is not None and op_deal not in (None,deal):
                    continue
            elif kind=='OPEN' and flat:
                pass  # flat book: stale OPEN cannot be live
            else:
                continue
            self.store.order_update(op['id'],'SUPERSEDED',op.get('reference'))
            cleared+=1
            self.store.event('reconcile','Cleared stale {0} after broker flat'.format(kind),
                {'order_id':op['id'],'deal_id':op_deal or deal,
                 'previous_status':op.get('status')})
        self._unresolved_at_login=bool(self.store.unresolved())
        self.pending_close=False
        self.pending_order=False
        was_frozen=not (self.armed and self.execution_enabled)
        self._ensure_live_unless_operator_paused()
        if self.positions:
            return cleared
        if (was_frozen or cleared) and not self.operator_paused:
            self.store.event('reconcile',
                'Re-armed after broker flat (auto-pause removed)',
                {'deal_id':deal,'cleared':cleared})
        # Drop in-trade RNN memory before the next flat OPEN vote.
        try:
            if self.brain is not None:
                self.brain.reset_flat_state(reason='broker_flat')
        except Exception as exc:
            self.store.event('warning','Flat recurrent reset skipped',
                            {'type':type(exc).__name__,'deal_id':deal})
        self._locked_entry_signal=None
        self._last_in_trade_protect_bar=None
        self._manage_signal=None
        # Flat + mandate close: unlock LIVE OPEN if cold prep holdout trapped us.
        try:
            self._maybe_release_cold_start_prep()
        except Exception as exc:
            self.store.event('warning','Cold-start prep release skipped',
                             {'type':type(exc).__name__,'deal_id':deal})
        return cleared

    async def _position_still_open(self,deal_id):
        """Fresh broker check — True only if this deal is still listed open."""
        if not deal_id or not self.broker:
            return False
        deal=str(deal_id)
        try:
            live=self._map_positions(await self.broker.positions())
        except Exception:
            live=list(self.positions or [])
        return any(str(p.get('dealId'))==deal for p in live)

    async def _settle_deal_from_history_cash(self,deal,*,source='history_settle'):
        """Bind Capital cash for one deal — never wipe sibling multi-pos rows."""
        deal=str(deal)
        # Exact dealId only — fuzzy tx refs must not wipe a live open.
        found=await self._pnl_from_broker_history(deal,exact_deal=True)
        if not found:
            return False
        pnl,ccy=found
        exit_guess=_num((self.market or {}).get('mid'))
        if exit_guess is None:
            snap=self.store.get('last_upl:'+deal) or {}
            exit_guess=_num(snap.get('exit_hint'))
        # Multi-pos safe: drop only this dealId from the local book.
        self.positions=[p for p in (self.positions or [])
                        if str(p.get('dealId'))!=deal]
        self._open_deal_ids={str(p['dealId']) for p in self.positions
                             if p.get('dealId') is not None}
        if not self.positions:
            self.position_age=0
            self.last_position_id=None
        else:
            head=self.positions[0].get('dealId')
            self.last_position_id=str(head) if head is not None else None
            self.position_age=0
        self.last_closed_step=self.brain.core.steps
        self.latest_close_id=deal
        conf={'level':exit_guess,'profit':float(pnl),'pnl_currency':self._cash_currency(ccy),
              'source':source}
        self._learn_closed_trade(deal,conf)
        self.store.event('reconcile',
            'Open deal settled from Capital history cash',
            {'deal_id':deal,'pnl':float(pnl),'source':source,
             'remaining_open':len(self.positions)})
        self._release_execution_after_flat(deal)
        return True

    async def _force_close_if_history_settled(self,deal):
        """If positions() still lists deal but history already paid cash — ghost open."""
        if not deal:return False
        try:
            return await self._settle_deal_from_history_cash(
                deal,source='stale_open_history_settled')
        except Exception as exc:
            self.store.event('warning','Stale-open history check failed',
                {'deal_id':str(deal),'type':type(exc).__name__})
            return False

    async def _recover_ghost_open_after_positions_error(self,exc):
        """positions() REST failed — Capital may already be flat on open dealIds.

        Leaving self.positions unchanged freezes Admin on 'Existing open position',
        blocks orphan £ reconcile, and clears neural scores while ON.
        Multi-pos: try every known open dealId (not only positions[0]).
        """
        candidates=[]
        seen=set()
        def _add(d):
            if d is None:return
            s=str(d)
            if s in seen:return
            seen.add(s);candidates.append(s)
        _add(self.last_position_id)
        for d in (getattr(self,'_open_deal_ids',None) or set()):
            _add(d)
        for p in (self.positions or []):
            _add(p.get('dealId'))
        if not candidates:
            return False
        settled=[]
        for deal in candidates:
            ok=await self._settle_deal_from_history_cash(
                deal,source='ghost_positions_error_recover')
            if ok:
                settled.append(deal)
        if not settled:
            self.store.event('warning','Positions sync failed; ghost open not confirmed closed',
                {'deal_ids':candidates,'type':type(exc).__name__})
            return False
        self.store.event('reconcile',
            'Cleared ghost open after positions API error; Capital history has cash close',
            {'deal_ids':settled,'remaining_open':len(self.positions or []),
             'error':type(exc).__name__})
        # Any settled deal means we made progress — do not re-raise the positions()
        # error (sibling opens stay until the next successful broker refresh).
        return True

    async def sync(self):
        if not self.broker:raise RuntimeError('Not connected')
        try:
            raw=await self.broker.positions()
        except Exception as exc:
            if await self._recover_ghost_open_after_positions_error(exc):
                return self.positions
            raise
        latest=self._map_positions(raw)
        latest=await self._fill_missing_epic(latest)
        prev_ids=set(getattr(self,'_open_deal_ids',None) or set())
        if not prev_ids and self.last_position_id is not None:
            prev_ids={str(self.last_position_id)}
        self.positions=latest
        new_ids={str(p['dealId']) for p in latest if p.get('dealId') is not None}
        # Day book may hold several same-epic deals. Only flag multi when a
        # foreign epic appears or count exceeds robot max — never freeze forever.
        mandate_epic=(self.mandate or {}).get('epic')
        foreign=False
        if mandate_epic:
            foreign=any(p.get('epic') and str(p.get('epic'))!=str(mandate_epic)
                        for p in latest)
        max_pos=int(day_book.load_params(self.store).get('max_positions') or 3)
        multi_conflict=bool(foreign or (len(latest)>max_pos))
        if multi_conflict:
            if not self._paused_for_multi:
                self.store.event('warning',
                    'Position book conflict (foreign epic or over max); new OPEN skipped',
                    {'open':len(latest),'max':max_pos,'foreign':foreign})
            self._paused_for_multi=True
        elif self._paused_for_multi:
            self._paused_for_multi=False
            self.store.event('reconcile','Multi-position book conflict resolved')
        vanished=prev_ids-new_ids
        for deal in sorted(vanished):
            await self._record_vanished_deal(deal)
        self._open_deal_ids=set(new_ids)
        current=None
        if len(latest)==1:
            current=str(latest[0]['dealId']) if latest[0].get('dealId') is not None else None
        elif len(latest)>1:
            # Prefer newest / first for legacy single-id callers.
            current=str(latest[0]['dealId']) if latest[0].get('dealId') is not None else None
        if latest:
            prev_id=str(self.last_position_id) if self.last_position_id is not None else None
            self.position_age=(self.position_age+1 if current==prev_id and len(latest)==1 else 0)
            for p in latest:
                self.store.upsert_trade(p)
                self._snapshot_open_upl(p)
            # Capital sometimes keeps a stale open while history already has cash.
            if len(latest)==1 and current and self.position_age>=2 and await self._force_close_if_history_settled(current):
                latest=[];current=None
                self.positions=[];self.position_age=0;self._open_deal_ids=set()
            elif len(latest)>1:
                # Multi-pos: age+settle each ghost deal independently.
                settled_ids=[]
                for p in list(latest):
                    deal=p.get('dealId')
                    if deal is None:
                        continue
                    deal_s=str(deal)
                    age_key='ghost_sync_age:'+deal_s
                    age=int(self.store.get(age_key) or 0)+1
                    self.store.set(age_key,age)
                    # age≥3 ≈ two re-syncs after first sight (same as single position_age≥2).
                    if age>=3 and await self._force_close_if_history_settled(deal_s):
                        settled_ids.append(deal_s)
                        self.store.set(age_key,None)
                if settled_ids:
                    latest=list(self.positions or [])
                    new_ids={str(p['dealId']) for p in latest if p.get('dealId') is not None}
                    self._open_deal_ids=set(new_ids)
                    current=(str(latest[0]['dealId'])
                             if latest and latest[0].get('dealId') is not None else None)
                for deal_s in list(prev_ids-new_ids):
                    self.store.set('ghost_sync_age:'+deal_s,None)
        else:
            self.position_age=0
            self._open_deal_ids=set()
            # Flat book: harvest any SQLite/pending cash every sync, not only on retry.
            try:
                await self._retry_pending_broker_pnl()
            except Exception as exc:
                self.store.event('warning','Flat-book pending P&L sync failed',
                                 {'type':type(exc).__name__})
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
                self._locked_entry_signal=None;self._last_in_trade_protect_bar=None
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
                # Capture entry neural state for THIS manual adopt so the close
                # can train LIVE weights — without it, closes only log £ and skip learn.
                key='entry_brain:'+str(deal_id)
                tr=getattr(self.brain.core,'last_transition',None)
                saved_brain=False
                if self.store.get(key) is None and isinstance(tr,dict) and tr.get('h') is not None:
                    try:
                        hlen=len(np.asarray(tr.get('h')))
                    except Exception:
                        hlen=-1
                    if hlen==getattr(self.brain.core,'n',None):
                        tick_sz=_num((self.market or {}).get('tick'))
                        self.store.set(key,{
                            'epic':p['epic'],'direction':p.get('direction'),
                            'entry':p.get('level'),'tick_size':tick_sz,
                            'entry_spread':_num((self.market or {}).get('spread')),
                            'transition':self.brain._json_transition(tr),
                            'scores':(self.last_signal or {}).get('scores'),
                            'shadow_scores':self._shadow_scores_now(),
                            'captured':'manual_adopt'})
                        saved_brain=True
                if not saved_brain and self.store.get(key) is None:
                    # Bind once on first in-session perception (after schema ready).
                    self._adopt_entry_brain_pending.add(str(deal_id))
                else:
                    self._adopt_entry_brain_pending.discard(str(deal_id))
                if fresh_session and open_in_flight:
                    self._supersede_stale_open_orders(deal_id)
                # Robot OPEN often lands as adopt (UNCERTAIN confirm). Claim trail
                # when this fill matches our submit / pending brain / entry_brain.
                pending=self._pending_entry_brain if isinstance(self._pending_entry_brain,dict) else None
                brain=self.store.get(key) if isinstance(self.store.get(key),dict) else {}
                robot_fill=(
                    bool(pending and pending.get('epic') in (None,p.get('epic'))
                         and pending.get('direction') in (None,p.get('direction')))
                    or (brain.get('captured') in (
                        'submit','claim_robot_open','manual_adopt_first_perception')
                        and brain.get('direction')==p.get('direction'))
                    or bool(self.store.get('robot_open:'+str(deal_id)))
                    or str(self.store.get('latest_robot_deal_id') or '')==str(deal_id)
                )
                if robot_fill:
                    self._claim_robot_open(p,reason='adopt_robot_fill')
                self.store.event('adoption','Live broker position adopted for this session',
                    {'deal_id':deal_id,'epic':p['epic'],'size':str(p['size']),
                     'direction':p.get('direction'),
                     'previous_epic':old['epic'] if old else None,
                     'fresh_session':fresh_session,
                     'cleared_stale_open':bool(fresh_session and open_in_flight),
                     'entry_brain_saved':bool(self.store.get(key)),
                     'robot_open_claimed':bool(self.store.get('robot_open:'+str(deal_id)))})
            elif self.mandate is None and latest:
                # Surface why WAITING FOR MOBILE TRADE persists despite an open deal.
                self.store.event('warning','Open broker position not adopted yet',
                    {'deal_id':deal_id,'epic':p.get('epic'),
                     'instrument_name':p.get('instrument_name'),
                     'direction':p.get('direction'),'size':p.get('size'),
                     'valid':valid,'own_order_marker':own_order,
                     'open_in_flight':open_in_flight})
        # Mid-session: robot OPEN UNCERTAIN blocks adopt, but Capital already
        # shows the fill — claim robot_open + trail without waiting for Resume.
        if latest and (self._pending_entry_brain or any(
                op.get('kind')=='OPEN' for op in (self.store.unresolved() or []))):
            pending=self._pending_entry_brain if isinstance(self._pending_entry_brain,dict) else {}
            for p in latest:
                did=p.get('dealId')
                if not did or self.store.get('robot_open:'+str(did)):
                    continue
                if pending:
                    if pending.get('epic') not in (None,p.get('epic')):continue
                    if pending.get('direction') not in (None,p.get('direction')):continue
                elif not any(op.get('kind')=='OPEN' for op in (self.store.unresolved() or [])):
                    continue
                self._claim_robot_open(p,reason='sync_open_in_flight_fill')
                self._supersede_stale_open_orders(did)
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
                self.store.event('warning',
                    'Position instrument/size differs from adopted trade; OPEN skipped (not paused)')
            self._paused_for_mismatch=True
        elif self._paused_for_mismatch:
            self._paused_for_mismatch=False
            self.store.event('reconcile','Position mismatch resolved')
        # Operator Pause is the ONLY freeze — every sync re-arms otherwise.
        self._ensure_live_unless_operator_paused()
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
    def _historical_candle_count(self,epic):
        row=self.store.db.execute(
            "SELECT COUNT(*) FROM candles WHERE epic=? AND source='HISTORICAL'",
            (epic,)).fetchone()
        return int(row[0] or 0) if row else 0

    def _oldest_historical_at(self,epic):
        row=self.store.db.execute(
            "SELECT at FROM candles WHERE epic=? AND source='HISTORICAL' "
            "ORDER BY at ASC LIMIT 1",(epic,)).fetchone()
        return row[0] if row else None

    def _ingest_broker_price_rows(self,epic,prices):
        """Persist completed broker M1 bars as HISTORICAL (never the forming bucket)."""
        current_bucket=candle_bucket(now())
        n=0
        for p in prices or []:
            def midprice(name,row=p):
                item=row.get(name) or {};b=_num(item.get('bid'));a=_num(item.get('ask'))
                return (a+b)/2 if a is not None and b is not None else None
            at=candle_bucket(p.get('snapshotTimeUTC') or p.get('snapshotTime'))
            if at is None or (current_bucket and at>=current_bucket):continue
            close_item=p.get('closePrice') or {}
            cb=_num(close_item.get('bid'));ca=_num(close_item.get('ask'))
            bar_spread=(ca-cb) if ca is not None and cb is not None and ca>=cb else None
            bar={'at':at,'open':midprice('openPrice'),'high':midprice('highPrice'),
                 'low':midprice('lowPrice'),'close':midprice('closePrice'),
                 'spread':bar_spread}
            if bar['at'] and all(v is not None for k,v in bar.items() if k not in ('at','spread')):
                self.store.candle(epic,bar,source='HISTORICAL');n+=1
        return n

    async def _bootstrap_historical_candles(self,epic,*,need=None):
        """Pull broker history in one shot (page older if needed) — no live wait.

        Capital.com returns up to 1000 M1 bars per request. We ask for the
        robot-computed need immediately, then keep appending live closed bars.
        """
        if not epic or not hasattr(self.broker,'prices'):return 0
        need=int(need if need is not None else recommended_live_history_bars(self.store))
        need=max(1,min(need,BROKER_PRICE_HISTORY_MAX))
        before=self._historical_candle_count(epic)
        try:
            source=await self.broker.prices(epic,resolution='MINUTE',maximum=need)
            self._ingest_broker_price_rows(epic,source.get('prices'))
        except Exception as exc:
            self.store.event('warning','Candle bootstrap unsuccessful',
                             {'type':type(exc).__name__,'epic':epic})
            return 0
        # Page older windows while still short (from/to), up to a few hops.
        pages=0
        while self._historical_candle_count(epic)<need and pages<4:
            oldest=self._oldest_historical_at(epic)
            if not oldest:break
            try:
                end=datetime.fromisoformat(str(oldest).replace('Z','+00:00'))
            except ValueError:
                break
            if end.tzinfo is None:end=end.replace(tzinfo=timezone.utc)
            to_dt=end-timedelta(minutes=1)
            from_dt=to_dt-timedelta(minutes=need)
            to_s=to_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            from_s=from_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            try:
                older=await self.broker.prices(
                    epic,resolution='MINUTE',maximum=need,
                    from_time=from_s,to_time=to_s)
                added=self._ingest_broker_price_rows(epic,older.get('prices'))
            except Exception as exc:
                self.store.event('warning','Candle history page unsuccessful',
                                 {'type':type(exc).__name__,'epic':epic,'page':pages})
                break
            pages+=1
            if added<=0:break
        after=self._historical_candle_count(epic)
        self.store.event('history',
            'Historical M1 bootstrap from broker' if after>=need
            else 'Historical M1 bootstrap partial — continuing with live bars',
            {'epic':epic,'need':need,'have':after,'gained':after-before,'pages':pages+1})
        return after

    async def _account_and_candles(self):
        if self.tick_count-self.last_account_tick>=10:
            self.last_account_tick=self.tick_count
            try:
                self.account=await self.broker.accounts()
                # Once account currency is known, fix GOLD→USD cash mislabels.
                self.repair_broker_pnl_currencies()
            except Exception as exc:self.store.event('warning','Account refresh unsuccessful',{'type':type(exc).__name__})
        if not self.mandate or not hasattr(self.broker,'prices'):return
        need=recommended_live_history_bars(self.store)
        have=self._historical_candle_count(self.mandate['epic'])
        # Eager: if history is thin, pull broker archive now — do not wait for M1 clock.
        due=(self.tick_count-self.last_candle_tick>=20) or (have<need)
        if not due:return
        self.last_candle_tick=self.tick_count
        await self._bootstrap_historical_candles(self.mandate['epic'],need=need)
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
            # Broker protection before slow secondary data.
            # Missing SL → full bracket. Missing TP alone → restore TP unless trail
            # intentionally cleared it (Capital often confirms OPEN with SL on / TP Off).
            for _p in list(self.positions or []):
                if _p.get('stopLevel') is None:
                    await self.protect_position(_p)
                elif _p.get('profitLevel') is None and self._needs_profit_restore(_p):
                    await self.protect_position(_p)
            if any(p.get('stopLevel') is None for p in (self.positions or [])):
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
            # LIVE context: include closed QUOTE minutes too. PK(epic,at) means a
            # minute written as QUOTE during the forming bar is excluded forever by
            # historical_only=True until Capital rewrite — that punched holes in the
            # last 60m and froze BUY/HOLD/SELL at 0%. Forming minute still excluded
            # by market_features cutoff. Window size from val_* (not fixed 190/400).
            hist_n=recommended_live_history_bars(self.store)
            candles=self.store.candles(
                self.mandate['epic'],n=hist_n,historical_only=False)
            self.feature_registry.load()
            if hasattr(self.brain,'apply_registry'):
                try:
                    self.brain.apply_registry(self.feature_registry)
                except Exception as exc:
                    self.store.event('warning','Registry apply deferred in poll',
                        {'type':type(exc).__name__,'detail':str(exc)[:160]})
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
                    liquidity_path=self.liquidity_path,
                    liquidity_fallback=self._last_good_liquidity,
                    peers_fallback=self._last_good_peers,
                    news_fallback=self._last_good_news)
                # After restart with an open position, peers/calendar/liquidity stay
                # background-only — fusion never becomes ready and admin shows BUY/
                # HOLD/SELL 0% forever. One blocking warm-up restores the first vote.
                if (fused is None and self.last_signal is None
                        and self.brain.feature_schema==self.brain.fusion_schema):
                    try:
                        await self._refresh_calendar_if_due()
                        await self._refresh_liquidity_if_due()
                        await self.intermarket.refresh(
                            self.broker,self.mandate['epic'],inst,candles)
                    except Exception as exc:
                        self.store.event('warning','Fusion warm-up refresh failed',
                            {'type':type(exc).__name__,'epic':self.mandate['epic']})
                    fused=fusion_features(self.market_context,self.intermarket.state,
                        self.mandate['epic'],self.news_calendar_path,
                        self.instrument_profile['currencies'],
                        now=clock(),expected_ohlc_dim=self.brain.ohlc_dim,
                        fusion_schema=self.brain.fusion_schema,
                        liquidity_path=self.liquidity_path,
                        liquidity_fallback=self._last_good_liquidity,
                        peers_fallback=self._last_good_peers,
                        news_fallback=self._last_good_news)
                self.latest_fusion_ready=fused is not None
                if fused is not None:
                    self.fusion_block_reason=None
                    # Remember live (or carried) EXTERNAL so a lagging feed
                    # cannot freeze the next M1 OPEN while the move finishes.
                    odim=self.brain.ohlc_dim
                    tail=fusion_liquidity_tail(fused,ohlc_dim=odim)
                    if tail is not None:
                        self._last_good_liquidity=tail
                    peers=fusion_peers_tail(fused,ohlc_dim=odim)
                    if peers is not None:
                        self._last_good_peers=peers
                    news=fusion_news_tail(fused,ohlc_dim=odim)
                    if news is not None:
                        self._last_good_news=news
                else:
                    # Name the EXTERNAL source that blocks a new fused decision.
                    self.fusion_block_reason=fusion_gap_reason(
                        self.market_context,self.intermarket.state,
                        self.mandate['epic'],self.news_calendar_path,
                        self.instrument_profile['currencies'],
                        now=clock(),expected_ohlc_dim=self.brain.ohlc_dim,
                        liquidity_path=self.liquidity_path,
                        liquidity_fallback=self._last_good_liquidity,
                        peers_fallback=self._last_good_peers,
                        news_fallback=self._last_good_news)
                # Prefer fusion when ready. After fusion-model transition, a gap
                # must NOT drive a NEW entry from a stale prior signal. Open
                # position protection continues below regardless of fusion.
                # Pre-fusion OHLC models may still tick on local features alone.
                in_pos=bool(self.positions)
                bar_t=self.market_context.get('bar_time')
                if in_pos:
                    # Admin/UI stays pinned to the OPEN vote (no mid-trade churn).
                    # Separately *perceive* each new M1 with in_position=True so a
                    # market reverse can trigger CLOSE — without entry ΔW / pending.
                    self._lock_entry_signal_for_open()
                    if bar_t is not None and bar_t!=self._last_in_trade_protect_bar:
                        self._last_in_trade_protect_bar=bar_t
                        new_closed_bar=True
                        manage=None
                        # Prefer fused features; if EXTERNAL lags, still perceive on
                        # OHLC so reverse CLOSE is not frozen by a peer/news gap.
                        if fused is not None:
                            manage=self.brain.tick(
                                self.market_context['close'],ask-bid,tick,
                                self.mandate['epic'],dt=60,features=fused,
                                bar_time=bar_t,external_features=True,
                                in_position=True)
                        elif self.market_context.get('features') is not None:
                            # OHLC manage path — works even when brain is on fusion
                            # schema for flat OPEN (tick returns None on fusion+ohlc
                            # mismatch). Use a one-shot OHLC forward if needed.
                            manage=self._manage_perceive_ohlc(
                                mid=self.market_context['close'],spread=ask-bid,
                                tick=tick,epic=self.mandate['epic'],bar_t=bar_t,
                                features=self.market_context['features'])
                        if manage is not None:
                            self._manage_signal={
                                'scores':dict(manage.get('scores') or {}),
                                'action':manage.get('action'),
                                'bar_time':bar_t,
                                'entry_train':False,'in_position':True,
                                'trend':self.market_context.get('trend'),
                                'regime':self.market_context.get('regime')}
                            # Adopt / restart often clears last_signal — seed the
                            # Admin freeze lock from first manage vote so UI is
                            # not stuck at 0% / "waiting for first M1 neural vote".
                            if (self._locked_entry_signal is None
                                    and isinstance(self._manage_signal.get('scores'),dict)
                                    and any(self._manage_signal['scores'].values())):
                                import copy
                                seeded=copy.deepcopy(self._manage_signal)
                                seeded['scores_frozen']=True
                                seeded['seeded_from_manage']=True
                                self._locked_entry_signal=seeded
                                self.last_signal=copy.deepcopy(seeded)
                    next_signal=None
                elif fused is not None:
                    next_signal=self.brain.tick(self.market_context['close'],ask-bid,tick,
                        self.mandate['epic'],dt=60,features=fused,
                        bar_time=bar_t,external_features=True,
                        in_position=False)
                elif not on_fusion:
                    next_signal=self.brain.tick(self.market_context['close'],ask-bid,tick,
                        self.mandate['epic'],dt=60,features=self.market_context['features'],
                        bar_time=bar_t,in_position=False)
                else:
                    next_signal=None
                if next_signal is not None and not in_pos:
                    next_signal=dict(next_signal,
                        model_version=self.brain.model_version,
                        feature_schema=self.brain.feature_schema,
                        market_bar=bar_t,
                        trend=self.market_context.get('trend'),
                        regime=self.market_context.get('regime'),
                        entry_train=True,
                        in_position=False)
                    self.last_signal=next_signal
                    self.last_decision_bar=bar_t
                    new_closed_bar=True
                    # Audit #32: never run neural growth / feature search before
                    # open-position protection. Flat books may self-dev now;
                    # open books defer until after trail/protect below.
                    self._run_self_development_cycle(candles,spread=ask-bid)
                elif on_fusion and fused is None and not self.positions:
                    # Flat + fusion gap: stale signal must not authorize entry.
                    # Keep last_signal for UI audit only when a position is open.
                    self.last_signal=None
                self.brain.last_regime=self.market_context['trend']
            else:
                self.latest_fusion_ready=False
                self.fusion_block_reason='ohlc: market context not ready'
                # Flat: never authorize entry on a stale signal. Open book: keep
                # last scores for Admin (same policy as fusion-gap + open above);
                # protection still runs from broker levels, not this signal.
                if not self.positions:
                    self.last_signal=None
                elif self.positions:
                    self._lock_entry_signal_for_open()
            # Bind the decision-time neural state (captured in submit) to the deal.
            # Fallback: only if we never captured at entry (e.g. manual adopt).
            if self.positions:
                for p in list(self.positions):
                  deal_s=str(p.get('dealId')) if p.get('dealId') is not None else None
                  if not deal_s:continue
                  key='entry_brain:'+deal_s
                  if self.store.get(key) is None and self._pending_entry_brain:
                    pending=self._pending_entry_brain
                    # Multi-pos: only bind to the deal we just opened — never an older sibling.
                    latest_open=self.store.get('latest_robot_deal_id')
                    if latest_open is not None and str(latest_open)!=deal_s:
                        continue
                    if pending.get('direction')==p.get('direction') and pending.get('epic')==p.get('epic'):
                        self.store.set(key,{**pending,'entry':p.get('level') if p.get('level') is not None else pending.get('entry'),
                                           'tick_size':pending.get('tick_size') or tick,
                                           'entry_spread':pending.get('entry_spread') if pending.get('entry_spread') is not None else ask-bid})
                        self._pending_entry_brain=None
                        self._adopt_entry_brain_pending.discard(deal_s)
                  elif self.store.get(key) is None and deal_s in self._adopt_entry_brain_pending:
                    # One-shot bind on first perception after adopt (not mid-trade churn).
                    tr=getattr(self.brain.core,'last_transition',None)
                    if isinstance(tr,dict) and tr.get('h') is not None:
                        try:hlen=len(np.asarray(tr.get('h')))
                        except Exception:hlen=-1
                        if hlen==getattr(self.brain.core,'n',None):
                            self.store.set(key,{
                                'epic':p.get('epic'),'direction':p.get('direction'),
                                'entry':p.get('level'),'tick_size':tick,
                                'entry_spread':ask-bid,
                                'transition':self.brain._json_transition(tr),
                                'scores':((self._manage_signal or {}).get('scores')
                                          or (self.last_signal or {}).get('scores')),
                                'shadow_scores':self._shadow_scores_now(),
                                'captured':'manual_adopt_first_perception'})
                            self._adopt_entry_brain_pending.discard(deal_s)
                  elif self.store.get(key) is None:
                    # Never bind mid-trade h/c as entry_brain — that poisons the
                    # closed-trade teacher with manage-state memory.
                    self.store.event('warning',
                        'entry_brain missing; refused poll_fallback mid-trade bind',
                        {'deal_id':p.get('dealId'),'epic':p.get('epic')})
                  else:
                    # Fill missing tick/spread on manual-adopt snapshots so close can learn.
                    saved=self.store.get(key)
                    if isinstance(saved,dict) and (
                            not saved.get('tick_size') or saved.get('entry_spread') is None
                            or not saved.get('transition')):
                        patch=dict(saved)
                        if not patch.get('tick_size'):patch['tick_size']=tick
                        if patch.get('entry_spread') is None:patch['entry_spread']=ask-bid
                        if not patch.get('transition') and self.brain.core.last_transition:
                            tr=self.brain.core.last_transition
                            if len(tr['h'])==self.brain.core.n:
                                patch['transition']=self.brain._json_transition(tr)
                        if patch.get('entry') is None:patch['entry']=p.get('level')
                        if not patch.get('shadow_scores'):
                            patch['shadow_scores']=self._shadow_scores_now()
                        self.store.set(key,patch)
            # Cosmetic policy.py search — OFF by default (LIVE entry = neural argmax).
            if new_closed_bar and self.brain.labeled:
                try:
                    from .self_params import get as sp_get
                    if int(sp_get(self.store,'policy_evolve_enabled',1)):
                        self.policy.evolve(
                            self.brain.correct/self.brain.labeled,
                            self.brain.labeled,list(self.brain.history))
                except Exception:
                    pass
            # Audit #22: historical candle load → chronological train → holdout eval
            # before autonomous LIVE OPEN. Separate from the live tick above.
            if (not self.positions and self.market_context.get('ready') and
                hasattr(self.brain,'ensure_prep_eval') and not self.operator_paused):
                try:
                    # Already flat after first manual close but prep still blocked?
                    # Unlock cold-start OPEN on poll — do not wait for another trade.
                    self._maybe_release_cold_start_prep()
                    rd=self.brain.trading_readiness()
                    from . import model_readiness as _mr
                    # Cold-start with 0 bars must still run chronological prep
                    # when candles exist — ready alone no longer skips it.
                    if _mr.needs_historical_prep(rd):
                        self.brain.ensure_prep_eval(
                            candles,tick_size=tick or 0.01,spread_fallback=ask-bid)
                except Exception as exc:
                    self.store.event('warning','Prep/eval stage failed',
                                    {'type':type(exc).__name__})
            if not self.armed:return
            # Open-risk path: protect + trail even when NEW entry execution is
            # paused. Trailing must not die because opens are frozen.
            if self.positions:
                # Manage every open deal (multi-pos day book) — reverse CLOSE + trail.
                manage_bar=(self._manage_signal or {}).get('bar_time')
                exit_bar=manage_bar or self.market_context.get('bar_time')
                scores=((self._manage_signal or {}).get('scores')
                        if isinstance((self._manage_signal or {}).get('scores'),dict)
                        else None) or (
                    (self.last_signal or {}).get('scores')
                    if isinstance(self.last_signal,dict) else None)
                if (self.execution_enabled and new_closed_bar and scores and exit_bar and
                    self.last_exit_processed_bar!=exit_bar):
                    self.last_exit_processed_bar=exit_bar
                    vote=self.policy.neural_decision(scores)
                    for p in list(self.positions):
                        if p.get('stopLevel') is None:
                            await self.protect_position(p)
                            continue
                        if p.get('profitLevel') is None and self._needs_profit_restore(p):
                            await self.protect_position(p)
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
                            self.store.event('exit',
                                'Neural CLOSE on confirmed reverse — market exit',
                                {'deal_id':p['dealId'],'direction':p['direction'],
                                 'vote':vote,'streak':streak,
                                 'returns_bps':self.market_context.get('returns_bps'),
                                 'manage_bar':manage_bar,
                                 'neural_market_exit':True})
                            await self.exit_position(p['dealId'])
                            return
                for p in list(self.positions):
                    if p.get('stopLevel') is None:
                        await self.protect_position(p)
                        continue
                    if p.get('profitLevel') is None and self._needs_profit_restore(p):
                        await self.protect_position(p)
                    await self.adapt_protection(p)
                # Fall through: day book may ADD another position on a new bar.
            if not self.execution_enabled:return
            # HOLD-shadow teach is an entry-policy teacher — only when flat.
            # In-trade resolves would ΔW the same LIVE weights mid-manage.
            if new_closed_bar and not self.positions:
                try:self._resolve_hold_shadows()
                except Exception as exc:
                    self.store.event('warning','hold_shadow resolve skipped',
                                    {'type':type(exc).__name__})
            # Multi-pos ADD edge — anti-HOLD + trend_aligned (same as snapshot gate).
            _poll_scores,_poll_edge,_poll_aligned=self._day_book_add_edge()
            book_allows=day_book.allows_new_open(
                store=self.store,equity=self._account_equity(),
                positions=self.positions,
                epic=(self.mandate or {}).get('epic'),
                mandate_epic=(self.mandate or {}).get('epic'),
                score_edge=_poll_edge,trend_aligned=_poll_aligned).get('allowed')
            if (book_allows and (_poll_scores or self.last_signal) and
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
                         'reason':self.last_entry_reason,'model_step':self.brain.core.steps,
                         'day_phase':(evaluation.get('day_book') or {}).get('phase'),
                         'open_count':len(self.positions or [])})
                    # HOLD with a directional proposal → shadow opposite path later
                    # (same brackets) so abstention does not collapse to forever-HOLD.
                    if action=='HOLD' and evaluation.get('proposal') in ('BUY','SELL'):
                        self._enqueue_hold_shadow(evaluation)
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

    def _maybe_release_cold_start_prep(self):
        """After first mandate close, do not leave LIVE OPEN trapped on failed holdout.

        Zero-start: manual open → close authorizes size; prep/eval holdout often
        fails on choppy GOLD and used to block every later robot OPEN forever.
        """
        if not self.mandate or self.last_closed_step<0:
            return None
        if not hasattr(self.brain,'trading_readiness'):
            return None
        rd=self.brain.trading_readiness()
        from . import model_readiness
        if model_readiness.allows_live_open(rd) and not model_readiness.needs_historical_prep(rd):
            return rd
        if model_readiness.allows_live_open(rd) and rd.get('cold_start_open'):
            # Already granted — keep seeking real bars via ensure_prep_eval.
            return rd
        return model_readiness.grant_cold_start_open(
            self.store,self.brain,
            reason=('Cold-start OPEN after first manual close — '
                    'historical prep not completed; LIVE allowed while candles train'))

    def live_open_gates(self,action=None):
        """Shared LIVE OPEN helper for poll/_entry_evaluation/submit (audit #22–#23).

        Direction stays neural argmax; this only reports whether readiness +
        utility gates currently allow submitting an OPEN. Other agents editing
        submit should call this before placing an order.
        """
        readiness=self.brain.trading_readiness() if hasattr(self.brain,'trading_readiness') else {'ready':False,'reason':'No readiness API'}
        from . import model_readiness as _mr
        if not _mr.allows_live_open(readiness):
            return {'allowed':False,'reason':readiness.get('reason') or 'Model prep/eval not ready',
                    'readiness':readiness,'economic_utility':None,'hard_risk':None}
        # Hard-risk limits are informational only — they never gate LIVE OPEN.
        hr=hard_risk.preflight(self.store)
        if action is not None and action not in ('BUY','SELL'):
            return {'allowed':False,'reason':'No directional action','readiness':readiness,
                    'economic_utility':None,'hard_risk':hr}
        return {'allowed':True,'reason':'Readiness passed — utility checked in evaluate_neural_entry',
                'readiness':readiness,'economic_utility':None,'hard_risk':hr}

    def _entry_vote_scores(self):
        """Scores for flat OPEN or multi-pos ADD.

        While a deal is live, Admin keeps the frozen OPEN vote — but ADD must
        use the fresh manage perceive scores. Frozen HOLD-tilt OPEN votes were
        blocking every multi-pos add (add_needs_stronger_edge forever).
        """
        if self.positions and isinstance(getattr(self,'_manage_signal',None),dict):
            ms=self._manage_signal.get('scores')
            if isinstance(ms,dict) and self.policy and self.policy._safe_scores(ms):
                return dict(ms),'manage'
        if isinstance(self.last_signal,dict):
            raw=(self.last_signal.get('scores_raw')
                 if isinstance(self.last_signal.get('scores_raw'),dict) else None)
            if raw and self.policy and self.policy._safe_scores(raw):
                return dict(raw),'entry_raw'
            sc=self.last_signal.get('scores')
            if isinstance(sc,dict) and self.policy and self.policy._safe_scores(sc):
                return dict(sc),'entry'
        return {},'none'

    def _day_book_add_edge(self):
        """Score edge for ADD gates — apply clear-trend anti-HOLD first.

        Admin used to compute edge on raw HOLD-dominated manage votes, so
        ``add_needs_stronger_edge`` stuck forever during visible TREND_UP.
        """
        scores,src=self._entry_vote_scores()
        ctx=self.market_context or {}
        structure=(ctx.get('structure')
                   or (self._live_ohlc_structure()[0] if self.mandate else None))
        phase=None
        try:
            snap=day_book.snapshot(self.store,equity=self._account_equity(),
                                   positions=self.positions)
            phase=snap.get('phase')
        except Exception:
            phase=day_book.PHASE_HUNT
        nudged,meta=day_book.soft_nudge_conviction(
            scores or {'BUY':0.,'SELL':0.,'HOLD':1.},
            phase=phase or day_book.PHASE_HUNT,store=self.store,
            live_trend=ctx.get('trend'),structure=structure,
            returns_bps=ctx.get('returns_bps'))
        clear=day_book._clear_trend_side(
            live_trend=ctx.get('trend'),structure=structure,
            returns_bps=ctx.get('returns_bps'))
        aligned=bool(
            self.positions and clear and all(
                str(p.get('direction') or '').upper()==clear
                for p in self.positions if p.get('direction')))
        edge=None
        if self.policy and hasattr(self.policy,'score_edge') and nudged:
            try:edge=self.policy.score_edge(nudged)
            except Exception:edge=None
        return nudged,edge,aligned

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
        if not self.mandate or self.brain.epic!=self.mandate['epic']:
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'Waiting for same-instrument neural signal','evidence':None}
        base_scores,vote_src=self._entry_vote_scores()
        if not base_scores:
            return {'action':'HOLD','proposal':'HOLD','threshold':self._adaptive_threshold(),
                    'reason':'Waiting for same-instrument neural signal','evidence':None}
        ctx=self.market_context or {}
        readiness=self.brain.trading_readiness() if hasattr(self.brain,'trading_readiness') else None
        history=list(self.brain.history)
        # Adaptive structure edge: soft-nudge scores from learned EV by frame×side.
        # Never a hard veto — neurons still vote; £ teacher corrects after closes.
        # Always nudge from raw vote scores — never re-nudge already-nudged copies.
        if (isinstance(self.last_signal,dict) and not self.positions
                and not self.last_signal.get('scores_raw')):
            self.last_signal=dict(self.last_signal,scores_raw=dict(base_scores))
        edge_advice=self._structure_edge_advice()
        nudged_scores,nudge_meta=structure_edge.soft_nudge_scores(
            base_scores,edge_advice,store=self.store,
            live_trend=ctx.get('trend'))
        # Situation memory: scene×£ soft tilt after structure edge (ready loop).
        sit_advice=self._situation_memory_advice(
            structure=(edge_advice or {}).get('structure'))
        nudged_scores,sit_nudge=situation_memory.soft_nudge_scores(
            nudged_scores,sit_advice,store=self.store)
        # Day book: anti-HOLD when move is clear; HOLD tilt when day already banked.
        equity=self._account_equity()
        db_snap=day_book.snapshot(self.store,equity=equity,positions=self.positions)
        phase=db_snap.get('phase') or day_book.PHASE_HUNT
        live_structure=(ctx.get('structure')
                        or (self._live_ohlc_structure()[0] if self.mandate else None))
        nudged_scores,conv_meta=day_book.soft_nudge_conviction(
            nudged_scores,phase=phase,store=self.store,
            live_trend=ctx.get('trend'),structure=live_structure,
            returns_bps=ctx.get('returns_bps'))
        nudged_scores,phase_meta=day_book.soft_nudge_day_phase(
            nudged_scores,phase=phase,store=self.store)
        # Flat only: publish nudged scores to last_signal. In-trade keep OPEN freeze.
        if isinstance(self.last_signal,dict) and not self.positions:
            self.last_signal=dict(
                self.last_signal,scores=dict(nudged_scores),
                structure_edge=edge_advice,structure_nudge=nudge_meta,
                situation_memory=sit_advice,situation_nudge=sit_nudge,
                day_book=db_snap,day_conviction=conv_meta,day_phase_nudge=phase_meta)
        result=self.policy.evaluate_neural_entry(
            nudged_scores,
            returns_bps=ctx.get('returns_bps'),
            trend=ctx.get('trend'),
            regime=ctx.get('regime'),
            history=history,
            readiness=readiness,
            spread_bps=self._spread_bps(),
            structure=live_structure)
        result=dict(result,structure_edge=edge_advice,structure_nudge=nudge_meta,
                    situation_memory=sit_advice,situation_nudge=sit_nudge,
                    day_book=db_snap,day_conviction=conv_meta,
                    day_phase_nudge=phase_meta,scores=nudged_scores,
                    vote_source=vote_src)
        if result['action'] in ('BUY','SELL'):
            gates=self.live_open_gates(result['action'])
            if not gates['allowed']:
                return dict(result,action='HOLD',reason=gates['reason'],
                            readiness=gates.get('readiness'))
            safety=self._external_entry_check(result['action'])
            if not safety['entry_allowed']:
                return dict(result,action='HOLD',reason=safety['reason'],external_veto=safety['status'])
            # Robot day book: multi-pos / day phase capacity (equity risk).
            score_edge=result.get('score_edge')
            if score_edge is None and hasattr(self.policy,'score_edge'):
                score_edge=self.policy.score_edge(nudged_scores)
            _clear=day_book._clear_trend_side(
                live_trend=ctx.get('trend'),structure=live_structure,
                returns_bps=ctx.get('returns_bps'))
            _aligned=bool(
                self.positions and _clear and all(
                    str(p.get('direction') or '').upper()==_clear
                    for p in self.positions if p.get('direction')))
            book=day_book.allows_new_open(
                store=self.store,equity=equity,positions=self.positions,
                epic=self.mandate.get('epic'),mandate_epic=self.mandate.get('epic'),
                score_edge=score_edge,trend_aligned=_aligned)
            result=dict(result,day_book_gate=book,trend_aligned=_aligned)
            if not book.get('allowed'):
                return dict(result,action='HOLD',reason=book.get('reason') or 'day_book_blocked')
            # One machine: after £ loss streak, weak neural edge → HOLD (same gate as submit).
            edge=unified_cycle.edge_allows_open(
                nudged_scores,result,
                unified_cycle.load_state(self.store),store=self.store)
            if not edge['allowed']:
                return dict(result,action='HOLD',reason=edge['reason'],
                            unified_cycle=edge)
            result=dict(result,unified_cycle=edge)
            # Structure shadow evidence only (UI / why) — NEVER veto LIVE OPEN.
            # LIVE executes neural vote; shadow teaches weights after SL closes.
            result=dict(result,structure_shadow=self._structure_shadow_advice(
                result['action']))
        else:
            result=dict(result,unified_cycle=unified_cycle.edge_allows_open(
                nudged_scores,result,
                unified_cycle.load_state(self.store),store=self.store))
        return result

    def _live_ohlc_structure(self):
        """Current OHLC layout label (TREND_UP/…/CHOP) for edge + situation memory."""
        if not self.mandate:
            return 'CHOP', None
        epic=self.mandate.get('epic')
        try:
            candles=self.store.candles(epic,n=90,historical_only=True) if epic else []
        except Exception:
            candles=[]
        chron=sorted(candles or [],key=lambda r:r.get('at') or '')
        n_path=int(getattr(structure_shadow,'ADVISE_PATH_BARS',30))
        n_pre=int(getattr(structure_shadow,'ADVISE_PRE_BARS',40))
        path=chron[-n_path:] if len(chron)>n_path else chron
        pre=chron[-(n_path+n_pre):-n_path] if len(chron)>n_path else chron[:max(0,len(chron)//2)]
        mid=_num(self.market.get('mid'))
        frame=structure_shadow.frame_from_ohlc(path,entry=mid,exit_level=None,pre_bars=pre)
        structure=(frame or {}).get('structure') or 'CHOP'
        return structure, frame

    def _structure_edge_advice(self):
        """BUY vs SELL EV in the live OHLC frame — soft evidence, not a gate."""
        if not self.mandate:
            return {'advice':None,'reason':'no_mandate'}
        epic=self.mandate.get('epic')
        structure,_frame=self._live_ohlc_structure()
        advice=structure_edge.compare_frame(self.store,epic=epic,structure=structure)
        structure_edge.persist_advice(self.store,advice)
        return advice

    def _situation_memory_advice(self, *, structure=None):
        """Scene×£ advice for the live situation — soft evidence, not a ban."""
        if not self.mandate:
            return {'advice':None,'reason':'no_mandate'}
        ctx=self.market_context or {}
        epic=self.mandate.get('epic')
        if not structure:
            structure,_=self._live_ohlc_structure()
        sit=situation_memory.fingerprint(
            epic=epic,market_context=ctx,structure=structure,
            returns_bps=ctx.get('returns_bps'),trend=ctx.get('trend'))
        advice=situation_memory.compare_situation(self.store,situation=sit)
        situation_memory.persist_advice(self.store,advice)
        return advice

    def _situation_fingerprint_now(self, *, structure=None):
        ctx=self.market_context or {}
        epic=(self.mandate or {}).get('epic')
        if not structure:
            structure,_=self._live_ohlc_structure()
        return situation_memory.fingerprint(
            epic=epic,market_context=ctx,structure=structure,
            returns_bps=ctx.get('returns_bps'),trend=ctx.get('trend'))

    def _structure_shadow_advice(self,proposal):
        """Evidence/UI: does current OHLC match prior SL graves? Not a LIVE gate."""
        if proposal not in ('BUY','SELL') or not self.mandate:
            return {'action':None,'reason':'no_proposal'}
        epic=self.mandate.get('epic')
        try:
            candles=self.store.candles(epic,n=90,historical_only=True) if epic else []
        except Exception:
            candles=[]
        chron=sorted(candles or [],key=lambda r:r.get('at') or '')
        # Evidence lookback only (not a closed-trade path): recent path + prior.
        n_path=int(getattr(structure_shadow,'ADVISE_PATH_BARS',30))
        n_pre=int(getattr(structure_shadow,'ADVISE_PRE_BARS',40))
        path=chron[-n_path:] if len(chron)>n_path else chron
        pre=chron[-(n_path+n_pre):-n_path] if len(chron)>n_path else chron[:max(0,len(chron)//2)]
        return structure_shadow.advise(
            self.store,epic=epic,proposal=proposal,bars=path,pre_bars=pre)

    def _record_structure_shadow_loss(self,deal_id,saved,*,epic,direction,
                                      entry,exit_level,exit_reason,broker_pnl,
                                      spread,transition=None):
        """Freeze SL path, frame why, teach LIVE neurons the shadow suggestion.

        Path = entry M1 → exit M1 (inclusive). Pre = candles before entry
        (breakout context only). Does not place orders or veto the next OPEN.
        """
        bar_time=(saved or {}).get('bar_time')
        try:
            candles=self.store.candles(epic,n=190,historical_only=True) if epic else []
        except Exception:
            candles=[]
        chron=sorted(candles or [],key=lambda r:r.get('at') or '')
        # Pre-entry context (before decision bar).
        pre=[]
        if bar_time:
            pre=[c for c in chron if c.get('at') is not None and str(c.get('at'))<str(bar_time)]
        else:
            mid=max(3,len(chron)//2)
            pre=chron[:mid]
        max_pre=int(getattr(structure_shadow,'MAX_PRE_BARS',40))
        if len(pre)>max_pre:pre=pre[-max_pre:]
        # In-trade path: entry bar → exit bar (cut post-close; keep SL candle).
        path=structure_shadow.trim_path_entry_to_exit(
            chron,direction=direction,entry=entry,exit_level=exit_level,
            entry_bar_time=bar_time,exit_bar_time=None,
            limit=int(getattr(structure_shadow,'MAX_BARS_PER_SNAP',60)))
        rec=structure_shadow.record_loss(
            self.store,epic=epic,direction=direction,entry=entry,
            exit_level=exit_level,exit_reason=exit_reason,
            broker_pnl=broker_pnl,bars=path,pre_bars=pre,
            deal_id=deal_id,spread=spread)
        if not rec:
            return None
        # Explain why (audit) — do NOT second-teach opposite/HOLD here.
        # Understanding comes from structure channels already in entry ``z``;
        # the single trade-PnL teacher (label_from_profit) updates those weights
        # and now also listens to library_prefers_hold + path MFE.
        frame=(rec.get('snapshot') or {}).get('frame') or {}
        teach=structure_shadow.suggest_teach(
            frame=frame,direction=direction,broker_pnl=broker_pnl)
        self.store.event('structure_shadow',
            'Shadow framed why SL happened (structure already in OPEN features)',{
                'deal_id':deal_id,'key':rec.get('key'),
                'structure':(teach or {}).get('structure') or frame.get('structure'),
                'why':(teach or {}).get('why'),
                'suggest':(teach or {}).get('suggest'),
                'taught_weights':False,
                'reason':'single_teacher_via_structure_features'})
        rec=dict(rec,teach=teach,taught=False)
        return rec

    def _record_structure_shadow_win(self,deal_id,saved,*,epic,direction,
                                     entry,exit_level,exit_reason,broker_pnl,
                                     spread):
        """Bank a winning OHLC frame so structure memory knows what pays."""
        bar_time=(saved or {}).get('bar_time')
        try:
            candles=self.store.candles(epic,n=190,historical_only=True) if epic else []
        except Exception:
            candles=[]
        chron=sorted(candles or [],key=lambda r:r.get('at') or '')
        pre=[]
        if bar_time:
            pre=[c for c in chron if c.get('at') is not None and str(c.get('at'))<str(bar_time)]
        else:
            mid=max(3,len(chron)//2)
            pre=chron[:mid]
        max_pre=int(getattr(structure_shadow,'MAX_PRE_BARS',40))
        if len(pre)>max_pre:pre=pre[-max_pre:]
        path=structure_shadow.trim_path_entry_to_exit(
            chron,direction=direction,entry=entry,exit_level=exit_level,
            entry_bar_time=bar_time,exit_bar_time=None,
            limit=int(getattr(structure_shadow,'MAX_BARS_PER_SNAP',60)))
        return structure_shadow.record_win(
            self.store,epic=epic,direction=direction,entry=entry,
            exit_level=exit_level,exit_reason=exit_reason,
            broker_pnl=broker_pnl,bars=path,pre_bars=pre,
            deal_id=deal_id,spread=spread)

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
            # PROTECT rejected because SL/TP already closed the deal → not a hang.
            if kind=='PROTECT' and deal_id and not await self._position_still_open(deal_id):
                self.store.order_update(order_id,'SUPERSEDED',ref)
                self.store.event('reconcile','PROTECT rejected after broker already flat',
                    {'deal_id':deal_id,'reference':ref})
                self._release_execution_after_flat(deal_id)
                return True
            return False
        if status!='ACCEPTED':raise RuntimeError('Broker confirmation not final')
        # Capture pre-sync deal ids so OPEN enroll binds the *new* deal, not
        # an older multi-pos row at positions[0].
        before_ids={str(p.get('dealId')) for p in (self.positions or [])
                    if p.get('dealId') is not None}
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
        if not matched:
            # Trail/protect raced broker SL/TP: deal already flat — not a freeze.
            if kind=='PROTECT' and deal_id and not any(
                    str(p.get('dealId'))==str(deal_id) for p in (self.positions or [])):
                self.store.order_update(order_id,'SUPERSEDED',ref)
                self.store.event('reconcile','PROTECT accepted but deal already flat',
                    {'deal_id':deal_id,'reference':ref})
                self._release_execution_after_flat(deal_id)
                return True
            raise RuntimeError('Accepted request not reconciled with broker positions')
        self.store.order_update(order_id,'CONFIRMED',ref)
        if kind=='OPEN' and self.positions:
            conf_deal=confirmation.get('dealId') or confirmation.get('dealID')
            p=None
            if conf_deal is not None:
                p=next((x for x in self.positions
                        if str(x.get('dealId'))==str(conf_deal)),None)
            if p is None:
                newcomers=[x for x in self.positions
                           if str(x.get('dealId')) not in before_ids]
                if len(newcomers)==1:
                    p=newcomers[0]
            if p is not None:
                # Persist origin + enroll trail (shared with adopt/supersede path).
                self._claim_robot_open(p,reason='open_confirmed')
        if kind=='PROTECT' and self.positions:
            p=next((x for x in self.positions if x.get('dealId')==deal_id),None)
            if p is not None and self.store.get('robot_open:'+str(deal_id)):
                # SL/TP may arrive on a later PUT — enroll trail then if OPEN lacked TP.
                self._enroll_adaptive_protection(p)
        if kind=='CLOSE':
            # Confirm often omits profit on CLOSE; fill from history before learn.
            # Never promote last UPL to realized cash (audit #1).
            # Still teach from exit level / entry_brain when cash is pending —
            # late broker £ refines the journal without a second ΔW.
            pnl,src,_ccy=await self._resolve_broker_pnl(deal_id,confirmation)
            if pnl is not None and src=='broker':
                if _num(confirmation.get('profit')) is None and _num(confirmation.get('pnl')) is None:
                    confirmation={**confirmation,'profit':pnl}
                if _ccy and not confirmation.get('pnl_currency'):
                    confirmation={**confirmation,'pnl_currency':_ccy}
            else:
                exit_lvl=_num(confirmation.get('level'))
                self._queue_pending_pnl(deal_id,exit_level=exit_lvl)
                self.store.event('warning','Close confirmed; broker cash P&L pending re-request',
                                 {'deal_id':deal_id,'reference':ref,'pnl_source':src})
            self._learn_closed_trade(deal_id,confirmation)
            # Confirmed close must leave the robot armed for the next OPEN.
            self._release_execution_after_flat(deal_id)
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
        """Best-effort deployable account £ for sizing + drawdown gating.

        Prefer ``available`` then ``balance`` then ``equity`` so OPEN packs
        size to cash left in the account (e.g. £20 → use £20), not a stale
        higher equity figure after open UPL.
        """
        account=self.account.get('accounts',[]) if isinstance(self.account,dict) else []
        active=next((a for a in account if a.get('preferred') or
                     a.get('accountId')==self.account.get('currentAccountId')),
                    account[0] if account else {})
        bal=active.get('balance') if isinstance(active,dict) else None
        if isinstance(bal,dict):
            for key in ('available','balance','equity'):
                v=_num(bal.get(key))
                if v is not None and v>0:return float(v)
        return _num(bal) if not isinstance(bal,dict) else None

    def _infer_exit_reason(self,deal_id,confirmation,*,entry=None,direction=None,
                           exit_level=None,tick=None,bars=None):
        """Classify SL / TP / trail / vanish / manual for bracket learning (audit #19).

        Live bug: broker often omits closeReason → everything logged ``unknown``
        (sl_count stayed 0). Fall back to OHLC path vs the deal's own stop/TP.
        """
        conf=confirmation or {}
        raw=(conf.get('exit_reason') or conf.get('reason') or conf.get('closeReason')
             or conf.get('actionType') or conf.get('activityType'))
        if raw:
            got=bracket_learn.normalize_exit_reason(raw)
            if got!='unknown':return got
        src=str(conf.get('source') or '')
        if src=='position_disappeared':
            # Try level proximity / path before falling back to vanish.
            pass
        elif src in ('manual','user'):
            return 'manual'
        meta=self.store.get('adaptive_protection:'+str(deal_id)) or {}
        saved=self.store.get('entry_brain:'+str(deal_id)) or {}
        entry_n=_num(entry);exit_n=_num(exit_level);tick_n=_num(tick)
        stop_dist=_num(meta.get('original_risk')) or _num(saved.get('stop_distance'))
        profit_dist=_num(saved.get('profit_distance')) or _num(meta.get('original_profit'))
        trail_active=bool(meta.get('owner')=='AUTO' and meta.get('trail_extreme') is not None)
        sign={'BUY':1,'SELL':-1}.get(direction)
        if entry_n is not None and exit_n is not None and sign is not None:
            move=sign*(exit_n-entry_n)
            tol=max(float(tick_n or 0)*1.5,abs(float(stop_dist or 0))*0.15,1e-9)
            if stop_dist and move<=-(float(stop_dist)-tol):
                return 'trail' if trail_active else 'sl'
            if profit_dist and move>=(float(profit_dist)-tol):
                return 'tp'
            if trail_active and move<0:
                return 'trail'
        # OHLC path: which planned level was tagged first (fixes unknown graves).
        path_bars=bars
        if not path_bars:
            epic=saved.get('epic') or (self.mandate or {}).get('epic')
            try:
                path_bars=self._trade_path_bars(
                    deal_id,saved,epic=epic,entry=entry_n,exit_level=exit_n,
                    direction=direction)
            except Exception:
                path_bars=None
        from_path=bracket_learn.infer_exit_reason_from_path(
            direction=direction,entry=entry_n,exit_level=exit_n,bars=path_bars,
            stop_distance=stop_dist,profit_distance=profit_dist,tick=tick_n,
            trail_active=trail_active)
        if from_path!='unknown':
            return from_path
        if src=='position_disappeared':
            return 'vanish'
        return 'unknown'

    def _trade_path_bars(self,deal_id,saved,*,epic,entry,exit_level,direction=None):
        """Entry→exit inclusive OHLC for CF / structure / exit inference.

        Cuts post-close candles so opposite CF cannot invent flips from noise
        after the broker fill.
        """
        bar_time=(saved or {}).get('bar_time')
        try:
            candles=self.store.candles(epic,n=190,historical_only=True) if epic else []
        except Exception:
            candles=[]
        chron=sorted(candles or [],key=lambda r:r.get('at') or '')
        # Prefer structure trim (keeps SL candle; drops post-exit).
        path=structure_shadow.trim_path_entry_to_exit(
            chron,direction=direction or (saved or {}).get('direction'),
            entry=entry,exit_level=exit_level,entry_bar_time=bar_time,
            exit_bar_time=None,
            limit=max(int(getattr(structure_shadow,'MAX_BARS_PER_SNAP',60)),120))
        out=[]
        for c in path:
            mid=_num(c.get('close'));hi=_num(c.get('high'));lo=_num(c.get('low'))
            if mid is None:continue
            out.append({
                'open':_num(c.get('open')),'mid':mid,
                'high':hi if hi is not None else mid,
                'low':lo if lo is not None else mid,
                'close':mid,'at':c.get('at'),
                'spread':_num(c.get('spread')),
            })
        return out

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
        epic=saved.get('epic') or (self.mandate or {}).get('epic')
        path_bars=None
        try:
            path_bars=self._trade_path_bars(
                deal_id,saved,epic=epic,entry=entry,exit_level=exit_level,
                direction=direction)
        except Exception:
            path_bars=None
        exit_reason=self._infer_exit_reason(
            deal_id,confirmation,entry=entry,direction=direction,
            exit_level=exit_level,tick=tick,bars=path_bars)
        # Recent M1 median range as vol context at close.
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
            # Prefer true MAE from path when available.
            if path_bars:
                from .profit_learning import path_excursion
                mae=path_excursion(path_bars,direction,entry).get('mae')
                if mae is not None:
                    adverse=float(mae)
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
        from .self_params import load as load_self_params, DEFAULTS as SP_DEFAULTS
        band=float(load_self_params(self.store).get(
            'scratch_pnl_abs',SP_DEFAULTS['scratch_pnl_abs']))
        if val>band:
            self.store.set('scratch_streak',0)
            return
        streak=int(self.store.get('scratch_streak') or 0)+1
        self.store.set('scratch_streak',streak)
        if streak<2:return
        self.store.event('entry','Repeated scratch closes (neural still decides next entry)',
                         {'pnl':pnl_value,'deal_id':deal_id,'streak':streak,
                          'scratch_pnl_abs':band})

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
        """Prefer trade opened (covers whole life); never use bare close-queue time.

        queued_at is when WE noticed the vanish — often a second after Capital
        stamped the cash row. Using it as `from` can miss the close entirely.
        """
        opened=None
        try:
            opened=(self.store.db.execute(
                'SELECT opened FROM trades WHERE deal_id=?',(str(deal_id),)
                ).fetchone() or [None])[0]
        except Exception:
            opened=None
        raw=opened or queued_at
        from_time=None
        if raw:
            try:
                from_time=str(raw).replace(' ','T')
                if from_time.endswith('+00:00'):from_time=from_time[:-6]+'Z'
                # If only queue time is known, back up 6h so close cash is inside window.
                if not opened and queued_at:
                    try:
                        dt=datetime.fromisoformat(str(queued_at).replace('Z','+00:00'))
                        from_time=(dt.astimezone(timezone.utc).replace(microsecond=0)
                                   -timedelta(hours=6)
                                   ).strftime('%Y-%m-%dT%H:%M:%SZ')
                    except Exception:
                        from_time=None
            except Exception:
                from_time=None
        # 24h window covers overnight SL/TP after restart when timestamps missing.
        return {'from_time':from_time,'last_period':None if from_time else 86400}

    @staticmethod
    def _row_cash_pnl(row,details=None):
        details=details if isinstance(details,dict) else {}
        # Capital TRADE rows expose pnl/cash as strings; activity uses details.profit.
        for key in ('profit','pnl','profitAndLoss','profitLoss','pl',
                    'guaranteedStopProfit','realizedPnl','cash'):
            val=_num(details.get(key))
            if val is None:val=_num(row.get(key))
            if val is not None and math.isfinite(val):
                return float(val)
        return None

    @staticmethod
    def _parse_broker_ts(raw):
        if raw in (None,''):
            return None
        try:
            text=str(raw).replace('Z','+00:00')
            dt=datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).timestamp()
        except Exception:
            return None

    def _transaction_matches_deal(self,row,deal,*,exact=False):
        """Capital tx.reference is often a short id, not the UUID dealId."""
        deal=str(deal)
        for key in ('reference','transactionId','dealId','dealReference','note'):
            ref=str(row.get(key) or '')
            if not ref:continue
            if exact:
                if ref==deal:return True
            elif ref==deal or deal in ref or ref in deal:
                return True
        return False

    def _activity_matches_deal(self,row,deal,*,exact=False):
        """Activity may put dealId on the row or inside details."""
        deal=str(deal)
        details=row.get('details') if isinstance(row.get('details'),dict) else {}
        for key in ('dealId','dealID','closingDealId','openingDealId','affectedDealId',
                    'dealReference','reference'):
            ref=str(row.get(key) or details.get(key) or '')
            if not ref:continue
            if exact:
                if ref==deal:return True
            elif ref==deal or deal in ref or ref in deal:
                return True
        return False

    def _history_cooldown_active(self):
        """True while Capital history is cooling down after errors / 429 storm."""
        until=_num(self.store.get('broker_history_cooldown_until'))
        return until is not None and time.time()<float(until)

    def _arm_history_cooldown(self,*,seconds=60,exc=None,deal_id=None):
        """Back off history API after failures so we do not mint 10k+ warning events."""
        prev=_num(self.store.get('broker_history_cooldown_until')) or 0.0
        until=max(float(prev),time.time()+max(5,int(seconds)))
        self.store.set('broker_history_cooldown_until',until)
        # Throttle warning spam: at most one event per cooldown window.
        last_log=_num(self.store.get('broker_history_fail_logged_at')) or 0.0
        if time.time()-float(last_log)<float(seconds):
            return
        self.store.set('broker_history_fail_logged_at',time.time())
        detail={'cooldown_s':int(seconds),'until':until}
        if exc is not None:
            detail['type']=type(exc).__name__
            detail['error']=str(exc)[:180]
        if deal_id is not None:
            detail['deal_id']=str(deal_id)
        self.store.event('warning','Broker history P&L lookup cooling down',detail)

    def _match_pnl_in_activity_rows(self,rows,deal,*,exact_deal=False):
        for row in rows or []:
            if not self._activity_matches_deal(row,deal,exact=exact_deal):
                continue
            details=row.get('details') if isinstance(row.get('details'),dict) else {}
            val=self._row_cash_pnl(row,details)
            if val is not None:
                return float(val),self._cash_currency(
                    row.get('currency') or details.get('currency'))
        return None

    def _match_pnl_in_transaction_rows(self,rows,deal,*,exact_deal=False):
        for row in rows or []:
            if not self._transaction_matches_deal(row,deal,exact=exact_deal):
                continue
            val=self._row_cash_pnl(row)
            if val is not None:
                return float(val),self._cash_currency(row.get('currency'))
        return None

    async def _pnl_from_broker_history(self,deal_id,*,queued_at=None,exact_deal=False,
                                       shared_activity=None,shared_transactions=None,
                                       allow_widen=True):
        """Pull closed-trade cash from Capital history/activity (not mid×size).

        ``shared_*`` payloads let one poll reuse a single 24h fetch across many
        pending deals — avoids the 86k failed-request storm under 429/backoff.
        """
        if not self.broker or not deal_id:return None
        if self._history_cooldown_active():
            return None
        deal=str(deal_id)
        look=self._history_lookback(deal,queued_at=queued_at)
        # Prefer shared window first (one fetch for the whole pending queue).
        if shared_activity is not None:
            hit=self._match_pnl_in_activity_rows(
                shared_activity.get('activities') if isinstance(shared_activity,dict)
                else shared_activity,deal,exact_deal=exact_deal)
            if hit:return hit
        if shared_transactions is not None:
            hit=self._match_pnl_in_transaction_rows(
                shared_transactions.get('transactions') if isinstance(shared_transactions,dict)
                else shared_transactions,deal,exact_deal=exact_deal)
            if hit:return hit
        # Activity often names the dealId on SL/TP/USER closes.
        if hasattr(self.broker,'activity') and shared_activity is None:
            try:
                kwargs=dict(detailed=True,deal_id=deal)
                if look['from_time']:
                    kwargs['from_time']=look['from_time'];kwargs['last_period']=None
                else:
                    kwargs['last_period']=look['last_period']
                payload=await self.broker.activity(**kwargs)
                rows=list(payload.get('activities') or [])
                # Filtered dealId+from_time can return empty on lag; widen once.
                if not rows and look['from_time'] and allow_widen:
                    payload=await self.broker.activity(
                        detailed=True,deal_id=deal,last_period=86400)
                    rows=list(payload.get('activities') or [])
                # Still empty: Capital sometimes ignores dealId filter — scan window.
                if not rows and allow_widen:
                    wide=dict(detailed=True,last_period=86400)
                    payload=await self.broker.activity(**wide)
                    rows=list(payload.get('activities') or [])
                hit=self._match_pnl_in_activity_rows(rows,deal,exact_deal=exact_deal)
                if hit:return hit
            except Exception as exc:
                self._arm_history_cooldown(seconds=90,exc=exc,deal_id=deal)
        if hasattr(self.broker,'transactions') and shared_transactions is None:
            try:
                kwargs=dict(tx_type='TRADE')
                if look['from_time']:
                    kwargs['from_time']=look['from_time'];kwargs['last_period']=None
                else:
                    kwargs['last_period']=look['last_period']
                payload=await self.broker.transactions(**kwargs)
                rows=list(payload.get('transactions') or [])
                if not rows and look['from_time'] and allow_widen:
                    payload=await self.broker.transactions(
                        last_period=86400,tx_type='TRADE')
                    rows=list(payload.get('transactions') or [])
                hit=self._match_pnl_in_transaction_rows(rows,deal,exact_deal=exact_deal)
                if hit:return hit
            except Exception as exc:
                self._arm_history_cooldown(seconds=90,exc=exc,deal_id=deal)
        return None

    def _requeue_unsettled_cash_closes(self):
        """Pull journal rows stuck on upl_estimate/pending back into the harvest queue."""
        try:
            rows=self.store.db.execute(
                "SELECT deal_id,exit,opened,closed,pnl_source FROM trades "
                "WHERE closed IS NOT NULL AND (pnl_source IS NULL OR pnl_source IN "
                "('pending_broker_pnl','pending_close_sync','upl_estimate',"
                "'ticks_only','price_size','position_disappeared')) "
                "ORDER BY closed DESC LIMIT 80").fetchall()
        except Exception:
            return 0
        n=0
        for deal_id,exit_lvl,_opened,_closed,src in rows:
            deal=str(deal_id)
            if self.store.get('broker_cash_banked:'+deal):
                continue
            self._queue_pending_pnl(deal,exit_level=_num(exit_lvl))
            n+=1
        return n

    @staticmethod
    def _tx_fingerprint(tx):
        """Stable id for a Capital TRADE row — used to consume fuzzy binds once."""
        if not isinstance(tx,dict):
            return None
        for key in ('reference','transactionId','dealId','dealReference','id'):
            ref=tx.get(key)
            if ref not in (None,''):
                return str(key)+':'+str(ref)
        # Last resort: cash+instrument+size so blank-ref opens cannot collide forever.
        val=SingleTradeEngine._row_cash_pnl(tx)
        blob='|'.join(str(tx.get(k) or '') for k in
                      ('instrumentName','marketName','epic','size','quantity','date','dateUtc'))
        if val is None and not blob.strip('|'):
            return None
        return 'fallback:'+str(val)+':'+blob

    def _fuzzy_tx_consumed(self):
        raw=self.store.get('fuzzy_pnl_tx_consumed') or []
        return set(str(x) for x in raw) if isinstance(raw,list) else set()

    def _mark_fuzzy_tx_consumed(self,fingerprint,*,deal_id=None):
        fp=str(fingerprint or '')
        if not fp:
            return
        used=list(self.store.get('fuzzy_pnl_tx_consumed') or [])
        if fp not in used:
            used.append(fp)
        if len(used)>2000:
            used=used[-2000:]
        self.store.set('fuzzy_pnl_tx_consumed',used)
        if deal_id is not None:
            self.store.set('fuzzy_pnl_tx_for:'+str(deal_id),fp)

    async def _pnl_fuzzy_instrument_cash(self,deal_id,*,queued_at=None):
        """When dealId refs miss, bind one unused closing TRADE for this deal.

        Capital often omits instrumentName and uses signed size. With many GOLD
        closes, ``len(hits)==1`` never fires — pick the closest unused TRADE by
        close time / openLevel instead of staying on upl_estimate forever.
        """
        if not self.broker or not hasattr(self.broker,'transactions'):return None
        deal=str(deal_id)
        try:
            row=self.store.db.execute(
                'SELECT epic,size,direction,opened,closed,entry,exit FROM trades '
                'WHERE deal_id=?',(deal,)).fetchone()
        except Exception:
            row=None
        if not row or not row[0]:return None
        epic,size,direction,opened,closed,entry,exit_lvl=(
            row[0],str(row[1] or ''),str(row[2] or ''),row[3],row[4],
            _num(row[5]),_num(row[6]))
        look=self._history_lookback(deal,queued_at=queued_at or opened)
        try:
            kwargs=dict(tx_type='TRADE')
            if look['from_time']:
                kwargs['from_time']=look['from_time'];kwargs['last_period']=None
            else:
                kwargs['last_period']=look['last_period'] or 86400
            payload=await self.broker.transactions(**kwargs)
            # Widen once — Capital from_time windows often miss the close cash.
            if not (payload.get('transactions') or []) and look['from_time']:
                payload=await self.broker.transactions(
                    last_period=86400,tx_type='TRADE')
        except Exception as exc:
            self.store.event('warning','Fuzzy transaction P&L lookup failed',
                             {'type':type(exc).__name__,'deal_id':deal})
            return None
        hits=[]
        epic_u=epic.upper()
        consumed=self._fuzzy_tx_consumed()
        already=self.store.get('fuzzy_pnl_tx_for:'+deal)
        target_ts=(self._parse_broker_ts(closed) or self._parse_broker_ts(queued_at)
                   or self._parse_broker_ts(opened))
        for tx in payload.get('transactions') or []:
            if self._transaction_matches_deal(tx,deal):
                # Prefer exact deal match path — caller already tried that.
                continue
            fp=self._tx_fingerprint(tx)
            if fp and fp in consumed and fp!=already:
                continue  # another deal already owns this TRADE row
            val=self._row_cash_pnl(tx)
            if val is None:continue
            # Opening TRADE / flat fill is not closing cash — never bank £0 fuzzy.
            if abs(float(val))<1e-12:
                continue
            blob=' '.join(str(tx.get(k) or '') for k in
                          ('instrumentName','marketName','epic','description','note')).upper()
            has_instrument=bool(blob.strip())
            # GOLD often shows as "Gold" / "Gold Spot"; keep XAU as alias.
            # Capital TRADE schema often OMIT instrumentName — then size/time decide.
            instrument_ok=(not has_instrument) or (
                epic_u in blob or (epic_u=='GOLD' and ('GOLD' in blob or 'XAU' in blob)))
            if not instrument_ok:
                continue
            # Size: Capital uses signed lots — compare absolute values.
            tx_size_raw=tx.get('size') if tx.get('size') not in (None,'') else tx.get('quantity')
            size_ok=True
            if tx_size_raw not in (None,'') and size not in (None,''):
                try:
                    size_ok=(abs(Decimal(str(tx_size_raw)))==abs(Decimal(str(size))))
                except (ValueError,TypeError,ArithmeticError):
                    size_ok=False
            if not size_ok:
                continue
            tx_ts=(self._parse_broker_ts(tx.get('dateUtc'))
                   or self._parse_broker_ts(tx.get('dateUTC'))
                   or self._parse_broker_ts(tx.get('date')))
            open_lvl=_num(tx.get('openLevel') or tx.get('open_level'))
            close_lvl=_num(tx.get('closeLevel') or tx.get('close_level'))
            # Score: lower is better. Prefer openLevel≈entry, then time, then name.
            score=0.0
            if has_instrument:
                score-=10.0
            if entry is not None and open_lvl is not None:
                diff=abs(float(entry)-float(open_lvl))
                if diff<=1e-6:
                    score-=1000.0  # exact open level → this deal's close cash
                else:
                    score+=min(200.0,diff*20.0)
            elif entry is not None and open_lvl is None:
                score+=25.0
            if exit_lvl is not None and close_lvl is not None:
                cdiff=abs(float(exit_lvl)-float(close_lvl))
                if cdiff<=1e-6:
                    score-=50.0
                else:
                    score+=min(40.0,cdiff*5.0)
            if target_ts is not None and tx_ts is not None:
                score+=abs(tx_ts-target_ts)/60.0  # minutes apart
            elif target_ts is not None and tx_ts is None:
                score+=500.0
            hits.append((score,float(val),self._cash_currency(tx.get('currency')),fp,tx_ts))
        if not hits:
            return None
        hits.sort(key=lambda h:h[0])
        best=hits[0]
        # Require a clear winner when several unused GOLD rows exist.
        if len(hits)>1 and (hits[1][0]-best[0])<0.5:
            self.store.event('warning','Fuzzy P&L ambiguous; waiting for clearer Capital row',
                             {'deal_id':deal,'candidates':len(hits),
                              'best_score':best[0],'second_score':hits[1][0]})
            return None
        _score,pnl,ccy,fp,_ts=best
        if fp:
            self._mark_fuzzy_tx_consumed(fp,deal_id=deal)
        return pnl,ccy

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
        """True when this deal is claimed or fully learned (audit #2 / critical #5)."""
        meta=self.store.get('learned_trade:'+str(deal_id))
        return bool(isinstance(meta,dict) and (
            meta.get('learned') or meta.get('claimed')))

    def _claim_trade_learned(self,deal_id):
        """Durable claim before weight mutation — crash cannot double-train (#5)."""
        key='learned_trade:'+str(deal_id)
        cur=self.store.get(key)
        if isinstance(cur,dict) and (cur.get('learned') or cur.get('claimed')):
            return False
        self.store.set(key,{
            'claimed':True,'learned':False,'at':now()})
        return True

    def _mark_trade_learned(self,deal_id,*,mode=None,pnl=None,source=None):
        self.store.set('learned_trade:'+str(deal_id),{
            'claimed':True,'learned':True,'at':now(),
            'mode':mode,'pnl':pnl,'source':source})

    def _queue_pending_pnl(self,deal_id,*,exit_level=None):
        """Queue Capital £ harvest. Cap 500 — never drop unresolved new closes."""
        pending=list(self.store.get('pending_broker_pnl_deals') or [])
        deal=str(deal_id)
        ids=[str(x.get('deal_id') if isinstance(x,dict) else x) for x in pending]
        if deal not in ids:
            pending.append({'deal_id':deal,'exit_level':exit_level,'queued_at':now()})
        if len(pending)>500:
            def _done(item):
                d=str(item.get('deal_id') if isinstance(item,dict) else item)
                return bool(self.store.get('broker_cash_banked:'+d))
            open_q=[x for x in pending if not _done(x)]
            done_q=[x for x in pending if _done(x)]
            # Prefer keeping unresolved; drop already-banked first, then oldest open.
            if len(open_q)<=500:
                pending=open_q
            else:
                pending=open_q[-500:]
            del done_q  # discarded when over cap
        self.store.set('pending_broker_pnl_deals',pending)

    def _bank_broker_cash_side_effects(self,deal_id,pnl,*,currency=None,action=None,
                                       exit_reason=None,scratch_band=None):
        """Once-only day-book / hard-risk / cycle / scratch bank from broker £.

        Must run even when neural already learned (late Capital cash). Keyed by
        ``broker_cash_banked:{deal}`` so ticks-then-£ and retry paths cannot
        double-count the same deal. A prior £0 poison bind may be corrected.
        """
        if pnl is None or deal_id is None:
            return False
        try:
            val=float(pnl)
        except (TypeError,ValueError):
            return False
        if not math.isfinite(val):
            return False
        deal=str(deal_id)
        key='broker_cash_banked:'+deal
        prev=self.store.get(key)
        if isinstance(prev,dict):
            prev_pnl=_num(prev.get('pnl'))
            # Allow one correction when a £0 open TRADE poisoned the bank key.
            if not (prev_pnl is not None and abs(prev_pnl)<1e-12 and abs(val)>=1e-12):
                return False
        # Side effects first — marker only after success so a crash can retry.
        hard_risk.record_realized_pnl(self.store,val)
        equity=self._account_equity()
        # Phase DONE vs PROTECT: count siblings still open — never this deal.
        open_n=len([p for p in (self.positions or [])
                    if str(p.get('dealId'))!=deal])
        day_book.record_close(
            self.store,pnl=val,equity=equity,open_count=open_n,deal_id=deal)
        # Situation memory: bank £ into the OPEN-time scene×action bucket.
        saved=self.store.get('entry_brain:'+deal) or {}
        direction=action or saved.get('direction') or self._trade_direction(deal)
        sit=saved.get('situation')
        if not sit:
            sit=situation_memory.fingerprint(
                epic=saved.get('epic') or (self.mandate or {}).get('epic'),
                structure=saved.get('structure'),
                trend=saved.get('trend'),
                returns_bps=saved.get('returns_bps'),
                market_context={'trend':saved.get('trend'),
                                'returns_bps':saved.get('returns_bps')})
        if direction in ('BUY','SELL'):
            situation_memory.record_outcome(
                self.store,situation=sit,action=direction,pnl=val,
                epic=saved.get('epic') or (self.mandate or {}).get('epic'),
                deal_id=deal)
        self._settle_unified_cycle(
            pnl=val,action=action,exit_reason=exit_reason,deal_id=deal,
            scratch_band=scratch_band)
        self._note_scratch_close(val,deal)
        self.store.set(key,{
            'at':now(),'pnl':val,'currency':currency,'action':action})
        return True

    def _trade_direction(self,deal_id):
        try:
            row=self.store.db.execute(
                'SELECT direction FROM trades WHERE deal_id=?',
                (str(deal_id),)).fetchone()
            if row and row[0] in ('BUY','SELL'):
                return row[0]
        except Exception:
            pass
        return None

    def _stop_ticks_by_deal(self,tick):
        """Per-deal stop distance in ticks for day-book committed risk."""
        out={}
        tick_f=_num(tick)
        if tick_f is None or tick_f<=0:
            return out
        for p in (self.positions or []):
            deal=p.get('dealId')
            if deal is None:
                continue
            meta=self.store.get('adaptive_protection:'+str(deal)) or {}
            risk=_num(meta.get('original_risk'))
            if risk is None:
                entry=_num(p.get('level'));stop=_num(p.get('stopLevel'))
                if entry is not None and stop is not None:
                    risk=abs(entry-stop)
            if risk is None or risk<=0:
                continue
            out[str(deal)]=float(risk)/float(tick_f)
        return out

    def _late_broker_teacher_update(self,deal_id,saved,*,broker_pnl,currency,
                                    exit_level=None,action=None):
        """Feed profit_learning + bracket_learn when Capital £ arrives after ticks."""
        if broker_pnl is None or not math.isfinite(float(broker_pnl)):
            return False
        saved=saved if isinstance(saved,dict) else self.store.get('entry_brain:'+str(deal_id))
        direction=(action or (saved or {}).get('direction')
                   or self._trade_direction(deal_id))
        epic=(saved or {}).get('epic') or (self.mandate or {}).get('epic')
        entry=_num((saved or {}).get('entry'))
        try:
            row=self.store.db.execute(
                'SELECT epic,entry,direction FROM trades WHERE deal_id=?',
                (str(deal_id),)).fetchone()
        except Exception:
            row=None
        if row:
            if not epic and row[0]:
                epic=row[0]
            if entry is None:
                entry=_num(row[1])
            if direction not in ('BUY','SELL') and row[2] in ('BUY','SELL'):
                direction=row[2]
        exit_lvl=_num(exit_level)
        spread=_num((saved or {}).get('entry_spread')) or 0.
        if direction in ('BUY','SELL') and epic and entry is not None:
            try:
                self.profit_learning.record_closed(
                    deal_id=deal_id,epic=epic,direction=direction,entry=entry,
                    exit_level=exit_lvl if exit_lvl is not None else entry,
                    broker_pnl=float(broker_pnl),currency=currency,spread=spread,
                    require_broker_pnl=True)
            except Exception as exc:
                self.store.event('warning','Late profit_learning skipped',
                                 {'deal_id':deal_id,'type':type(exc).__name__})
        try:
            from . import bracket_learn
            bk=(saved or {}).get('bracket_kappa')
            bs=(saved or {}).get('bracket_sl_scale')
            if bk is None or bs is None:
                cur=bracket_learn.load_params(self.store);bk,bs=cur['kappa'],cur['sl_scale']
            bracket_learn.record_outcome(
                self.store,kappa=bk,sl_scale=bs,pnl=float(broker_pnl),
                currency=currency,deal_id=deal_id)
        except Exception as exc:
            self.store.event('warning','Late bracket_learn skipped',
                             {'deal_id':deal_id,'type':type(exc).__name__})
        try:
            from .self_params import evolve_from_feedback, maybe_commit_long_horizon
            evolve_from_feedback(self.store,broker_pnl=float(broker_pnl),commit=False)
            maybe_commit_long_horizon(self.store,self.profit_learning.broker_pnls())
        except Exception:
            pass
        return True

    def _pending_pnl_due(self,base,*,now_ts=None):
        """Exponential backoff so unsettled deals do not hammer Capital every poll."""
        now_ts=time.time() if now_ts is None else float(now_ts)
        attempts=int(base.get('attempts') or 0)
        last=_num(base.get('last_attempt_at'))
        if last is None or attempts<=0:
            return True
        # UPL already on Admin — shared history fetch is free this poll; do not
        # leave £ stuck on upl_estimate behind a 256s backoff.
        if base.get('upl_shown'):
            return True
        # 0→immediate, then 8,16,32,64,128,256,300s cap.
        delay=min(300.0,float(2**min(max(attempts,1)+2,8)))
        return (now_ts-float(last))>=delay

    async def _retry_pending_broker_pnl(self):
        """Re-request pending cash P&L; refine records; learn at most once (audit #1–2)."""
        if not self.broker:return
        # Live Admin stuck on [upl_estimate]: re-arm journal rows every poll.
        try:
            self._requeue_unsettled_cash_closes()
        except Exception as exc:
            self.store.event('warning','Unsettled cash requeue failed',
                             {'type':type(exc).__name__})
        pending=list(self.store.get('pending_broker_pnl_deals') or [])
        if not pending:return
        now_ts=time.time()
        # Global cool-down after history errors — stop minting failure events.
        if self._history_cooldown_active():
            self.store.set('pending_broker_pnl_deals',pending)
            return
        # One shared 24h window for the whole queue (N deals ≠ N×API storms).
        shared_act=shared_tx=None
        try:
            if hasattr(self.broker,'activity'):
                shared_act=await self.broker.activity(
                    detailed=True,last_period=86400)
            if hasattr(self.broker,'transactions'):
                shared_tx=await self.broker.transactions(
                    last_period=86400,tx_type='TRADE')
        except Exception as exc:
            self._arm_history_cooldown(seconds=120,exc=exc)
            self.store.set('pending_broker_pnl_deals',pending)
            return
        remaining=[]
        # Cap work per poll so a long queue cannot starve protect/trail.
        due_budget=5
        for item in pending:
            base=item if isinstance(item,dict) else {'deal_id':item}
            deal=str(base.get('deal_id'))
            queued=base.get('queued_at')
            exit_level=base.get('exit_level')
            if not self._pending_pnl_due(base,now_ts=now_ts) or due_budget<=0:
                remaining.append(dict(base) if isinstance(item,dict) else base)
                continue
            due_budget-=1
            attempts=int(base.get('attempts') or 0)+1
            # Shared payloads first; per-deal widen only early attempts.
            found=await self._pnl_from_broker_history(
                deal,queued_at=queued,shared_activity=shared_act,
                shared_transactions=shared_tx,allow_widen=(attempts<=2))
            # Fuzzy epic/size match early — Capital often uses short tx references.
            if not found and attempts<=6 and not self._history_cooldown_active():
                try:
                    found=await self._pnl_fuzzy_instrument_cash(deal,queued_at=queued)
                except Exception as exc:
                    self._arm_history_cooldown(seconds=60,exc=exc,deal_id=deal)
            # Exhausted history: bind last live UPL for Admin display ONLY —
            # never teach from UPL. Keep the deal in pending so a later real
            # broker cash row can overwrite the estimate (do not drop forever).
            # Faster UPL display (attempt≥3) so Admin is not stuck at £0 for hours.
            if not found and attempts>=3 and not base.get('upl_shown'):
                snap=self.store.get('last_upl:'+deal) or {}
                upl=_num(snap.get('upl'))
                if upl is not None and math.isfinite(upl):
                    ccy_upl=self._cash_currency(snap.get('currency'))
                    self.store.close_trade(
                        deal,exit_level,float(upl),currency=ccy_upl,
                        source='upl_estimate',overwrite=True)
                    self.store.event('trade',
                        'Pending cash shown from last UPL; still waiting for broker',
                        {'deal_id':deal,'upl':float(upl),'attempts':attempts})
                    nxt=dict(base);nxt['attempts']=attempts;nxt['deal_id']=deal
                    nxt['upl_shown']=True;nxt['last_attempt_at']=now_ts
                    remaining.append(nxt)
                    continue
            if not found:
                nxt=dict(base);nxt['attempts']=attempts;nxt['deal_id']=deal
                nxt['last_attempt_at']=now_ts
                remaining.append(nxt)
                continue
            pnl,hist_ccy=found
            ccy=self._cash_currency(hist_ccy)
            # Always through learn path — already_learned banks £ + late teachers once.
            conf={'level':exit_level,'profit':pnl,'pnl_currency':ccy,
                  'source':'pending_pnl_retry'}
            self._learn_closed_trade(deal,conf)
            self.store.set('last_upl:'+deal,None)
        self.store.set('pending_broker_pnl_deals',remaining)
        # Flat book: also harvest Capital cash for open SQLite rows whose deal vanished
        # before we queued pending (restart / missed sync).
        if not self.positions and not self._history_cooldown_active():
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
                self._bank_broker_cash_side_effects(
                    deal,pnl,currency=ccy,action=self._trade_direction(deal))
            else:
                self._learn_closed_trade(deal,conf)
            self.store.event('reconcile','Orphan open trade closed from Capital history',
                             {'deal_id':deal,'pnl':pnl,'pnl_currency':ccy})
            self.last_closed_step=max(self.last_closed_step,self.brain.core.steps)

    def _counterfactual_context(self,deal_id,saved,*,epic,entry,exit_level,tick,spread):
        """Bars + SL/TP for path counterfactual (audit #15); empty → endpoint CF.

        Bars are trimmed entry→exit (inclusive). Post-close candles used to invent
        false opposite wins after the fill.
        """
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
        direction=(saved or {}).get('direction')
        bars=self._trade_path_bars(
            deal_id,saved,epic=epic,entry=entry,exit_level=exit_level,
            direction=direction)
        # Attach spread fallback for cost-aware path sim.
        if bars and spread is not None:
            for b in bars:
                if b.get('spread') is None:
                    b['spread']=spread
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

    def _structure_teacher_context(self,deal_id,saved,*,epic,direction,entry,
                                   exit_level,bars=None):
        """Structure memory + path MFE for the single trade-PnL teacher."""
        path=bars or []
        pre=[]
        bar_time=(saved or {}).get('bar_time')
        try:
            candles=self.store.candles(epic,n=190,historical_only=True) if epic else []
        except Exception:
            candles=[]
        chron=sorted(candles or [],key=lambda r:r.get('at') or '')
        if bar_time:
            pre=[c for c in chron if c.get('at') is not None and str(c.get('at'))<str(bar_time)]
        max_pre=int(getattr(structure_shadow,'MAX_PRE_BARS',40))
        if len(pre)>max_pre:pre=pre[-max_pre:]
        frame=structure_shadow.frame_from_ohlc(
            path,entry=entry,exit_level=exit_level,pre_bars=pre)
        prefer=structure_shadow.library_prefers_hold(
            self.store,epic=epic,structure=frame.get('structure'),
            direction=direction)
        from .profit_learning import path_excursion
        exc=path_excursion(path,direction,entry)
        # Adaptive BUY/SELL/HOLD from structure×£ EV (opposite when it pays).
        frame_advice=structure_edge.compare_frame(
            self.store,epic=epic,structure=frame.get('structure') or 'CHOP')
        return {
            'frame':frame,
            'structure_prefer_hold':bool(prefer.get('prefer_hold')),
            'structure_key':prefer.get('key'),
            'structure_reason':prefer.get('reason'),
            'structure_frame_advice':frame_advice,
            'path_mfe':exc.get('mfe'),
            'path_mae':exc.get('mae'),
            'gave_then_lost':bool(exc.get('gave_then_lost')),
            'pre_bars':pre,
        }

    def _scratch_band(self):
        from .self_params import load as load_self_params, DEFAULTS as SP_DEFAULTS
        try:
            return float(load_self_params(self.store).get(
                'scratch_pnl_abs',SP_DEFAULTS['scratch_pnl_abs']))
        except (TypeError,ValueError):
            return float(SP_DEFAULTS['scratch_pnl_abs'])

    def _settle_unified_cycle(self,*,pnl,action=None,exit_reason=None,deal_id=None,
                              scratch_band=None):
        """Fold broker £ into the single LIVE cycle bus (aggression / streaks)."""
        if scratch_band is None:
            scratch_band=self._scratch_band()
        cycle=unified_cycle.settle(
            self.store,pnl=pnl,action=action,exit_reason=exit_reason,
            scratch_band=scratch_band)
        self.store.event('cycle','Unified cycle settled from broker £',{
            'deal_id':deal_id,'pnl':pnl,'action':action,
            'exit_reason':exit_reason,'scratch':bool(cycle.get('last_scratch')),
            'aggression':cycle.get('aggression'),
            'loss_streak':cycle.get('loss_streak'),
            'win_streak':cycle.get('win_streak')})
        return cycle

    def _would_be_brackets(self,proposal):
        """SL/TP distances OPEN would use for proposal — for hold-shadow only."""
        if proposal not in ('BUY','SELL') or not self.mandate:
            return None
        epic=self.mandate.get('epic')
        mid=self.market.get('mid')
        tick=self.market.get('tick')
        spread=self.market.get('spread')
        minimum=self.market.get('min_distance')
        if None in (mid,tick,spread) or not epic:
            return None
        try:
            hist_n=recommended_live_history_bars(self.store)
            candles=self.store.candles(epic,n=hist_n,historical_only=False)
        except Exception:
            candles=[]
        if len(candles)<20:
            return None
        zone=bracket_learn.zone_at_open(candles,multi_tf=True)
        rng=zone['range_distance'] or ohlc_range_distance(candles)
        buffer=max(2*float(tick),2*float(spread))
        min_d=float(minimum) if minimum is not None else float(tick)
        base_sl=max(min_d+buffer,float(rng or 0))
        learned=bracket_learn.load_multipliers(self.store)
        cycle_state=unified_cycle.load_state(self.store)
        scores=(self.last_signal or {}).get('scores')
        # Force evaluation action so compose_open still yields κ/sl when edge
        # was the reason LIVE HOLDed (we need distances, not another HOLD).
        edge=self.policy.score_edge(scores) if scores else 0.5
        # Use a strong synthetic edge for bracket *shape* only — not for LIVE.
        pkg=unified_cycle.compose_open(
            action=proposal,scores=scores or {
                'BUY':0.7 if proposal=='BUY' else 0.15,
                'SELL':0.7 if proposal=='SELL' else 0.15,'HOLD':0.15},
            evaluation={'action':proposal,'score_edge':max(0.2,float(edge or 0))},
            zone=zone,learned=learned,cycle_state=cycle_state,base_sl=base_sl,
            max_tp=zone.get('max_tp'),store=self.store)
        # Even if edge gate HOLDs LIVE, shadow still needs OHLC-decided distances.
        tighten=float(pkg.get('tighten') or min(1.0,float(
            (cycle_state or {}).get('aggression') or 1.0)))
        entry=float(mid)
        decided=bracket_learn.decide_brackets_from_ohlc(
            zone,tick=tick,spread=spread,min_distance=min_d,tighten=tighten,
            store=self.store)
        return {
            'entry':entry,'tick':float(tick),'spread':float(spread),
            'min_distance':min_d,
            'stop_distance':float(decided['stop_distance']),
            'profit_distance':float(decided['profit_distance']),
            'bracket_source':'ohlc_zone',
            'ohlc_kappa':decided.get('ohlc_kappa'),
            'epic':epic,
        }

    def _enqueue_hold_shadow(self,evaluation):
        """When LIVE HOLDs a BUY/SELL proposal, queue opposite-path shadow."""
        proposal=evaluation.get('proposal')
        if proposal not in ('BUY','SELL'):
            return None
        if not self.brain.core.last_transition:
            return None
        brackets=self._would_be_brackets(proposal)
        if not brackets:
            return None
        tr=self.brain._json_transition(self.brain.core.last_transition)
        from .self_params import load as load_self_params
        slip_bps=float(load_self_params(self.store).get('exec_slip_bps') or 0.)
        row=hold_shadow.enqueue(
            self.store,proposal=proposal,
            reason=evaluation.get('reason') or self.last_entry_reason,
            bar_time=self.last_decision_bar or self.market_context.get('bar_time'),
            epic=brackets['epic'],entry=brackets['entry'],
            spread=brackets['spread'],
            stop_distance=brackets['stop_distance'],
            profit_distance=brackets['profit_distance'],
            tick=brackets['tick'],min_distance=brackets['min_distance'],
            transition=tr,scores=(self.last_signal or {}).get('scores'),
            slip_bps=slip_bps)
        if row:
            self.store.event('hold_shadow','Queued opposite-path shadow for HOLD abstention',{
                'proposal':proposal,'opposite':row.get('opposite'),
                'bar_time':row.get('bar_time'),
                'stop_distance':row.get('stop_distance'),
                'profit_distance':row.get('profit_distance')})
        return row

    def _hold_shadow_candles_after(self,bar_time,epic):
        bars=[]
        try:
            candles=self.store.candles(epic,n=190,historical_only=True) if epic else []
        except Exception:
            candles=[]
        started=not bool(bar_time)
        for c in candles:
            at=c.get('at')
            if not started:
                if at is None or str(at)<str(bar_time):
                    continue
                started=True
            mid=_num(c.get('close'));hi=_num(c.get('high'));lo=_num(c.get('low'))
            if mid is None:
                continue
            bars.append({
                'mid':mid,'high':hi if hi is not None else mid,
                'low':lo if lo is not None else mid,
                'spread':_num(c.get('spread')),'at':at})
        return bars

    def _resolve_hold_shadows(self):
        """Resolve HOLD shadows ASAP: opposite TP, proposal TP, or both-lost→HOLD."""
        from .self_params import load as load_self_params
        def _learn(label,transition,mode,detail):
            # Light teach — not full trade-PnL cannon (was overwriting M1).
            sp=load_self_params(self.store)
            lr=float(sp.get('learn_lr',0.28))
            repeats=max(1,min(4,int(sp.get('hold_shadow_repeats',2))))
            taught=False
            for i in range(repeats):
                step=self.brain.learn_from_trade(
                    transition,int(label),lr=lr,won=False,count_sample=(i==0))
                meta=getattr(self.brain.core,'last_learn_meta',None) or {}
                if (step is not None and not meta.get('restored')
                        and float(meta.get('delta_norm') or 0)>1e-9):
                    taught=True
            if taught:
                if 'opposite_tp' in str(mode):
                    learning_generation.bump(self.store,'hold_shadow_opposite_tp')
                    try:
                        from .self_params import evolve_from_feedback
                        evolve_from_feedback(
                            self.store,hold_shadow_taught=True,commit=False,
                            reason='hold_shadow opposite TP teach')
                    except Exception:
                        pass
                elif 'proposal_tp' in str(mode):
                    learning_generation.bump(self.store,'hold_shadow_proposal_tp')
                else:
                    learning_generation.bump(self.store,'hold_shadow_hold')
            msg=('Taught opposite after missed TP on HOLD' if 'opposite_tp' in str(mode)
                 else ('Taught proposal after missed TP on HOLD' if 'proposal_tp' in str(mode)
                       else 'Taught HOLD after both sides lost on abstention'))
            self.store.event('hold_shadow',msg,{
                'mode':mode,'label':int(label),
                'opposite':detail.get('opposite'),
                'proposal':detail.get('proposal'),
                'opposite_pnl':detail.get('opposite_pnl'),
                'proposal_pnl':detail.get('proposal_pnl'),
                'learn_repeats':repeats,'weights_moved':taught,
                'path_bars':detail.get('path_bars') or
                (detail.get('opposite_sim') or {}).get('bars_held')})
        outcomes=hold_shadow.resolve_ready(
            self.store,candles_after=self._hold_shadow_candles_after,
            learn_callback=_learn,horizon=hold_shadow.HORIZON_BARS)
        return outcomes

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
        # Still bank £ once — neural-once-only must not skip day_book / hard_risk /
        # profit_learning / bracket_learn teachers.
        if already_learned:
            if broker_pnl is not None:
                currency=self._cash_currency(hint_ccy) or self._account_currency()
                self.store.close_trade(deal_id,exit_level,broker_pnl,currency=currency,
                                       source='broker',overwrite=True)
                action=self._trade_direction(deal_id) or (
                    saved.get('direction') if isinstance(saved,dict) else None)
                banked=self._bank_broker_cash_side_effects(
                    deal_id,broker_pnl,currency=currency,action=action)
                teacher_key='late_cash_teachers:'+str(deal_id)
                if banked and not self.store.get(teacher_key):
                    self._late_broker_teacher_update(
                        deal_id,saved,broker_pnl=broker_pnl,currency=currency,
                        exit_level=exit_level,action=action)
                    self.store.set(teacher_key,{'at':now(),'pnl':broker_pnl})
                self.store.event('learning','Late broker P&L refined; neural learn skipped (once-only)',
                                 {'deal_id':deal_id,'pnl':broker_pnl,'banked':banked})
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
                if pnl_source=='broker' and pnl_value is not None:
                    self._bank_broker_cash_side_effects(
                        deal_id,pnl_value,currency=currency,action=row[1])
                else:
                    self._queue_pending_pnl(deal_id,exit_level=exit_level)
                    self.store.close_trade(deal_id,exit_level,source='pending_broker_pnl',
                                           overwrite=True)
            elif exit_level is not None or broker_pnl is not None:
                currency=hint_ccy or (self._account_currency() if broker_pnl is not None else None)
                self.store.close_trade(deal_id,exit_level,broker_pnl,currency=currency,
                                       source='broker' if broker_pnl is not None else None,
                                       overwrite=overwrite)
                if broker_pnl is not None:
                    self._bank_broker_cash_side_effects(
                        deal_id,broker_pnl,currency=currency,
                        action=self._trade_direction(deal_id))
                else:
                    self._queue_pending_pnl(deal_id,exit_level=exit_level)
                    self.store.close_trade(deal_id,exit_level,source='pending_broker_pnl',
                                           overwrite=True)
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
                if pnl_source=='broker' and pnl_value is not None:
                    self._bank_broker_cash_side_effects(
                        deal_id,pnl_value,currency=currency,action=direction)
                else:
                    self._queue_pending_pnl(deal_id,exit_level=exit_level)
                    self.store.close_trade(deal_id,exit_level,source='pending_broker_pnl',
                                           overwrite=True)
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
        # Profit-centred labels — no entry cooldown. Losses use label_from_profit
        # (opposite ∪ HOLD with spread floor). flip_after_loss is audit suffix only.
        prev_out=self.store.get('last_trade_outcome') or {}
        flip_after_loss=bool(
            pnl_sign<0 and isinstance(prev_out,dict) and
            int(prev_out.get('pnl_sign') or 0)<0 and
            prev_out.get('direction') in ('BUY','SELL') and
            prev_out.get('direction')!=direction)
        # Missing entry spread → live market spread (never 0: that taught false flips).
        spread_cost=_num(saved.get('entry_spread'))
        if spread_cost is None or spread_cost<=0:
            live_sp=_num((self.market or {}).get('spread'))
            if live_sp is not None and live_sp>0:
                spread_cost=live_sp
            else:
                spread_cost=0.
        shadow_action=None
        if self.shadow_brain is not None and saved.get('shadow_scores'):
            sc=saved['shadow_scores']
            try:
                shadow_action=max(('BUY','SELL','HOLD'),key=lambda a:float(sc.get(a,0)))
            except (TypeError,ValueError):
                shadow_action=None
        # Audit #15: path CF with same SL/TP/trail/costs (not entry→exit delta alone).
        # Bars trimmed entry→exit so post-close noise cannot invent flips.
        cf=self._counterfactual_context(deal_id,saved,epic=epic,entry=entry,
                                        exit_level=exit_level,tick=tick,
                                        spread=spread_cost)
        # Single teacher listens to structure memory + path MFE (no second teach).
        struct_ctx=self._structure_teacher_context(
            deal_id,saved,epic=epic,direction=direction,entry=entry,
            exit_level=exit_level,bars=cf.get('bars'))
        teach_extra=dict(
            structure_prefer_hold=struct_ctx.get('structure_prefer_hold'),
            path_mfe=struct_ctx.get('path_mfe'),
            path_mae=struct_ctx.get('path_mae'),
            structure_frame_advice=struct_ctx.get('structure_frame_advice'))
        # Profit ledger: ONLY broker-confirmed £ — never ±1 tick fakes.
        if pnl_source=='broker' and pnl_value is not None:
            decision=self.profit_learning.record_closed(
                deal_id=deal_id,epic=epic,direction=direction,entry=entry,
                exit_level=exit_level,broker_pnl=pnl_value,
                currency=currency,spread=spread_cost,flip_after_loss=flip_after_loss,
                model_version=getattr(self.brain,'model_version',0),
                feature_schema=getattr(self.brain,'feature_schema',None),
                shadow_action=shadow_action,require_broker_pnl=True,
                **cf,**teach_extra)
            label=decision['label'];mode=decision['mode']
            if label is not None:label=int(label)
        else:
            # Tick path must use the SAME loss gate as broker £ (opposite ∪ HOLD
            # with spread floor). Raw best_action could re-pick the loser / false flip.
            from .profit_learning import label_from_profit
            tick_pnl=(1.0 if pnl_sign>0 else (-1.0 if pnl_sign<0 else 0.0))
            decision=label_from_profit(
                direction,tick_pnl,flip_after_loss=flip_after_loss,
                entry=entry,exit_level=exit_level,spread=spread_cost,
                **cf,**teach_extra)
            label=decision.get('label')
            if label is not None:label=int(label)
            mode=str(decision.get('mode') or 'ticks_no_counterfactual')
            if mode and not mode.endswith('_ticks'):
                mode=mode+'_ticks'
            self.profit_learning.record_closed(
                deal_id=deal_id,epic=epic,direction=direction,entry=entry,
                exit_level=exit_level,broker_pnl=None,
                currency=currency,spread=spread_cost,flip_after_loss=flip_after_loss,
                model_version=getattr(self.brain,'model_version',0),
                feature_schema=getattr(self.brain,'feature_schema',None),
                shadow_action=shadow_action,require_broker_pnl=True,
                **cf,**teach_extra)
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
        # Penny losses used to skip entirely (repeats=0) while still claiming the
        # deal — same bad entry repeated with no ΔW. Teach light HOLD instead.
        scratch_band=float(sp['scratch_pnl_abs'])
        scratch_hold=False
        if pnl_value is not None:
            try:
                cash=float(pnl_value)
                scratch_hold=(-scratch_band<=cash<0.)
            except (TypeError,ValueError):
                scratch_hold=False
        if scratch_hold:
            # Penny closes: do NOT teach HOLD into the core (HOLD-trap) and do
            # NOT burn the once-only learn slot — brackets/cycle still settle.
            label=None
            repeats=0
            mode=str(mode)+'_scratch_no_neural'
        learn_lr=float(sp['learn_lr'])
        transition=saved['transition']
        losses=[]
        # Critical #5: claim BEFORE weight mutation. Only claim when we will
        # actually train — null/zero-repeat must not burn once-only learn.
        claimed_neural=False
        if repeats>0 and label is not None:
            claimed_neural=bool(self._claim_trade_learned(deal_id))
            if not claimed_neural:
                # Already claimed/learned elsewhere — skip weight mutation.
                repeats=0
        # Audit #17: trade-PnL via AdaptiveBrain.learn_from_trade — separate
        # counters/weights from minute-direction; does not bump minute labeled.
        last_delta=None
        any_real_dw=False
        for i in range(repeats):
            step_loss=self.brain.learn_from_trade(
                transition,label,lr=learn_lr,won=pnl_sign>0,count_sample=(i==0))
            if step_loss is not None:losses.append(step_loss)
            meta=getattr(self.brain.core,'last_learn_meta',None) or {}
            dn=meta.get('delta_norm')
            if dn is not None:
                last_delta=dn
                if (not meta.get('restored')) and float(dn or 0)>1e-9:
                    any_real_dw=True
        # ΔW already updated neural scores (entry quality). Entry floor stays
        # fixed at 0.06 — note_weight_update is a pin/no-op for the gate.
        if repeats>0 and label is not None and claimed_neural:
            unified_cycle.note_weight_update(
                self.store,won=pnl_sign>0,delta_norm=last_delta)
        # Independent shadow core trains on same label using *its* decision-time h/c.
        if repeats>0 and label is not None and claimed_neural:
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
        # ticks_only / missing cash must still hunt Capital history — else Admin
        # shows 13 closes with £0 forever while Capital already paid.
        if pnl_source!='broker' or pnl_value is None:
            self._queue_pending_pnl(deal_id,exit_level=exit_level)
            if pnl_source in ('ticks_only',None,''):
                self.store.close_trade(deal_id,exit_level,source='pending_broker_pnl',
                                       overwrite=True)
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
            # Audit #21 + day book + cycle: once-only bank (also used on late £).
            self._bank_broker_cash_side_effects(
                deal_id,pnl_value,currency=currency,action=direction,
                exit_reason=path_ctx.get('exit_reason'),
                scratch_band=float(sp.get('scratch_pnl_abs') or 0.05))
            equity=self._account_equity()
            if equity is not None:
                hard_risk.check_drawdown_halt(self.store,equity)
            promoted=bracket_learn.evolve_from_history(self.store)
            if promoted:
                self.store.event('bracket','SL/TP shape promoted from cash P&L history',promoted)
                learning_generation.bump(self.store,'bracket_shape_promoted')
        if repeats>0 and self.brain.core.last_z is not None:
            grow_th=float(sp['grow_error_threshold'])
            nmax=int(sp.get('max_neurons',64))
            cooldown=int(sp.get('birth_cooldown_steps',24))
            last_birth=int(self.store.get('last_neuron_birth_step') or -10**9)
            cooled=(self.brain.core.steps-last_birth)>=cooldown
            if cooled and self.brain.core.grow_if_needed(
                    np.asarray(self.brain.core.last_z),threshold=grow_th,nmax=nmax):
                self.store.set('last_neuron_birth_step',self.brain.core.steps)
                self.store.event('neural','Neuron added after closed-trade learning',
                                 {'neurons':self.brain.core.n,'deal_id':deal_id,
                                  'nmax':nmax})
            trimmed=self.brain.core.prune_to_cap(nmax)
            if trimmed:
                self.store.event('neural','Trimmed neurons to max_neurons after trade',
                                 {'trimmed':trimmed,'neurons':self.brain.core.n,'nmax':nmax})
        # Real adaptation proof: LIVE ΔW (not restored/no-op) → Paaudze.
        weights_moved=bool(any_real_dw)
        if weights_moved:
            learning_generation.bump(self.store,'closed_trade_weight_update')
            self.store.set('neural_adapt_reason',
                f'Closed {direction} £{pnl_value} → LIVE learn×{repeats} mode={mode}')
            self.brain.model_version=int(getattr(self.brain,'model_version',0) or 0)+1
        # Critical #5: mark learned ONLY when we claimed + trained (or claimed
        # with no ΔW). Never burn the once-only slot on label=None / scratch skip
        # — that blocked later CF retry and left the same bad entry untaught.
        if claimed_neural and (repeats>0 or any_real_dw):
            self._mark_trade_learned(deal_id,mode=mode,pnl=pnl_value,source=pnl_source)
        elif claimed_neural:
            # Claimed but no train steps — still finalize so crash cannot double-claim.
            self._mark_trade_learned(deal_id,mode=mode,pnl=pnl_value,source=pnl_source)
        self.brain.save()
        weights_updated=bool(repeats>0 and weights_moved and claimed_neural)
        if weights_updated:
            self.store.event('learning','Closed-trade P&L updated LIVE neural weights',
                {'deal_id':deal_id,'epic':epic,'direction':direction,'mode':mode,
                 'pnl':pnl_value,'pnl_source':pnl_source,'pnl_currency':currency,
                 'broker_pnl':broker_pnl,
                 'realized_directional_ticks':realized_ticks,'target':label,
                 'learn_repeats':repeats,'scratch_hold_skip':scratch_hold,
                 'last_loss':losses[-1] if losses else None,
                 'weights_updated':True,'tick_size':tick,
                 'learning_generation':learning_generation.current(self.store),
                 'bracket':bracket_learn.load_params(self.store)})
        else:
            # Honest: claim burned, weights unchanged (scratch / null label).
            self.store.event('learning','Closed-trade recorded without weight update',
                {'deal_id':deal_id,'epic':epic,'direction':direction,'mode':mode,
                 'pnl':pnl_value,'pnl_source':pnl_source,'pnl_currency':currency,
                 'learn_repeats':repeats,'scratch_hold_skip':scratch_hold,
                 'target':label,'weights_updated':False,
                 'learning_generation':learning_generation.current(self.store)})
        # Structure shadow: bank loss AND win OHLC frames (memory of what works).
        # Teach label itself comes from label_from_profit (listens to library/MFE).
        try:
            exit_reason_tag=self._infer_exit_reason(
                deal_id,confirmation,entry=entry,direction=direction,
                exit_level=exit_level,tick=tick,bars=cf.get('bars'))
            if (str(exit_reason_tag or '').lower()=='sl'
                    or (pnl_value is not None and pnl_value<0)):
                rec=self._record_structure_shadow_loss(
                    deal_id,saved,epic=epic,direction=direction,
                    entry=entry,exit_level=exit_level,
                    exit_reason=exit_reason_tag or 'loss',
                    broker_pnl=pnl_value,spread=spread_cost,
                    transition=saved.get('transition') if isinstance(saved,dict) else None)
                if rec and not rec.get('taught'):
                    self.store.event('structure_shadow',
                        'Recorded loss OHLC frame for shadow memory',{
                            'deal_id':deal_id,'key':rec.get('key'),
                            'structure':(rec.get('snapshot') or {}).get('frame',{}).get('structure'),
                            'direction':direction,'pnl':pnl_value,
                            'bars':len((rec.get('snapshot') or {}).get('bars') or []),
                            'exit_reason':exit_reason_tag,
                            'teacher_mode':mode,
                            'structure_prefer_hold':struct_ctx.get('structure_prefer_hold'),
                            'path_mfe':struct_ctx.get('path_mfe'),
                            'suggest':(rec.get('teach') or {}).get('suggest'),
                            'why':(rec.get('teach') or {}).get('why')})
            elif pnl_value is not None and pnl_value>0:
                win_rec=self._record_structure_shadow_win(
                    deal_id,saved,epic=epic,direction=direction,
                    entry=entry,exit_level=exit_level,
                    exit_reason=exit_reason_tag or 'tp',
                    broker_pnl=pnl_value,spread=spread_cost)
                if win_rec:
                    self.store.event('structure_shadow',
                        'Recorded win OHLC frame for shadow memory',{
                            'deal_id':deal_id,'key':win_rec.get('key'),
                            'structure':(win_rec.get('snapshot') or {}).get('frame',{}).get('structure'),
                            'direction':direction,'pnl':pnl_value,
                            'exit_reason':exit_reason_tag,
                            'bars':len((win_rec.get('snapshot') or {}).get('bars') or [])})
        except Exception as exc:
            self.store.event('warning','Structure shadow record skipped',
                             {'type':type(exc).__name__,'deal_id':deal_id})
        # Multi-champion library: tag broker £ by OPEN context; save if proven.
        if pnl_source=='broker' and pnl_value is not None:
            try:
                ctx=(saved.get('champion_context') if isinstance(saved,dict) else None)
                if not isinstance(ctx,dict):
                    ctx=champion_library.context_from(
                        epic=epic,
                        trend=(self.market_context or {}).get('trend'),
                        regime=(self.market_context or {}).get('regime'),
                        news=self._news_risk())
                champion_library.append_trade(
                    self.store,context=ctx,pnl=pnl_value,deal_id=deal_id,
                    model_version=getattr(self.brain,'model_version',0),
                    direction=direction)
                champion_library.consider_save(self.store,self,context=ctx)
            except Exception as exc:
                self.store.event('warning','Champion library update skipped',
                                 {'type':type(exc).__name__,'deal_id':deal_id})
        # Shadow vs LIVE promote check right after a real weight update.
        if weights_updated:
            try:self.policy.shadow_self_dev_tick(self)
            except Exception as exc:
                self.store.event('warning','Post-trade shadow self-dev skipped',
                                 {'type':type(exc).__name__,'deal_id':deal_id})
        try:
            # Cosmetic policy.py search — gated off LIVE hot path by default.
            # Do not overwrite neural_adapt_reason with "No relative edge…".
            before_reason=self.store.get('neural_adapt_reason')
            from .self_params import get as sp_get
            if weights_updated and int(sp_get(self.store,'policy_evolve_enabled',1)):
                self.policy.evolve(self.brain.correct/max(1,self.brain.labeled),
                                   self.brain.labeled,list(self.brain.history),
                                   force=False)
            if before_reason:
                self.store.set('neural_adapt_reason',before_reason)
        except Exception as exc:
            self.store.event('warning','Trade-timed evolution skipped',
                             {'type':type(exc).__name__,'deal_id':deal_id})
        # Immediately after a real close: flush any HOLD shadows that already
        # hit TP/SL on available candles — do not wait for a later poll cycle.
        try:
            self._resolve_hold_shadows()
        except Exception as exc:
            self.store.event('warning','Post-close hold_shadow resolve skipped',
                             {'type':type(exc).__name__,'deal_id':deal_id})
        self.store.set(key,None)

    def _trail_structure_label(self,p,candles=None):
        """OHLC structure tag for adaptive trail arm (TREND/BREAKOUT/…)."""
        epic=p.get('epic')
        entry=_num(p.get('level'))
        try:
            rows=candles if candles is not None else (
                self.store.candles(epic,n=90,historical_only=True) if epic else [])
        except Exception:
            rows=[]
        chron=sorted(rows or [],key=lambda r:r.get('at') or '')
        n_path=30;n_pre=40
        path=chron[-n_path:] if len(chron)>n_path else chron
        pre=chron[-(n_path+n_pre):-n_path] if len(chron)>n_path else chron[:max(0,len(chron)//2)]
        frame=structure_shadow.frame_from_ohlc(
            path,entry=entry,exit_level=None,pre_bars=pre)
        return (frame or {}).get('structure') or 'CHOP'

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
            if not existing.get('structure'):
                meta=dict(existing)
                meta['structure']=self._trail_structure_label(p)
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
            'structure':self._trail_structure_label(p),
            'opened':(existing or {}).get('opened') or now()}
        self.store.set(key,meta)
        self.store.event('protection','Robot-opened position enrolled in adaptive SL/TP',
            {'deal_id':deal_id,'initial_risk':meta['original_risk'],
             'initial_profit':meta['original_profit'],'tp_optional':tp is None,
             'structure':meta.get('structure')})
        return True

    async def adapt_protection(self,p):
        """Trail SL behind 1m OHLC structure — ROBOT only; continues past BE.

        Pre-BE gap = original_risk; after BE stop follows recent completed 1m
        swing lows/highs (fallback: median M1 range), not a huge % of R.
        Trail self-arms from MFE / structure EV (not a fixed human 1R law).
        """
        key='adaptive_protection:'+str(p['dealId'])
        # Late enroll if OPEN lacked TP / meta was never written.
        if self.store.get('robot_open:'+str(p.get('dealId'))):
            self._enroll_adaptive_protection(p)
        meta=self.store.get(key)
        if not meta or meta.get('owner')!='AUTO':
            self.protection_brief={'status':'Manual position or earlier bot order: existing SL/TP left unchanged'}
            return
        if self.pending_order or self.pending_close:return
        # Deal-scoped unresolved only. Sibling OPEN UNCERTAIN / other-deal
        # leftovers must not freeze THIS deal's trail forever.
        deal_s=str(p.get('dealId'))
        blocking=[]
        for item in (self.store.unresolved() or []):
            if str(item.get('deal_id') or '')!=deal_s:
                continue
            kind=item.get('kind')
            if kind=='PROTECT':
                self.store.order_update(item['id'],'SUPERSEDED')
                self.store.event('reconcile','Stale PROTECT superseded so trail can continue',{
                    'deal_id':deal_s,'order_id':item.get('id')})
            else:
                blocking.append(item)
        if blocking:return
        if self.market.get('status')!='TRADEABLE':return
        if self.market.get('tick') is None or self.market.get('min_distance') is None:return
        opened=candle_time(meta.get('opened')) if meta.get('opened') else None
        # Include closed QUOTE minutes — HISTORICAL-only punched holes and froze
        # post-BE trail on missing recent closes (same bug as live features).
        # Drop the forming bucket so anchors stay on completed 1m closes.
        from .protection_ai import completed_1m_candles
        raw_candles=self.store.candles(p['epic'],n=120,historical_only=False)
        candles=completed_1m_candles(raw_candles)
        zone_range=ohlc_range_distance(candles)  # lookback from data
        extreme=trail_extreme_from_broker(
            p['direction'],self.market.get('bid'),self.market.get('ask'),
            candles=candles,opened_at=opened,prior_extreme=meta.get('trail_extreme'))
        if extreme is not None or zone_range is not None:
            meta=dict(meta);meta['trail_extreme']=extreme
            if zone_range is not None:meta['zone_range']=float(zone_range)
            self.store.set(key,meta)
        # Adaptive trail arm: MFE / structure EV / BE lock — not a frozen 1R rule.
        entry=_num(p.get('level'));sign={'BUY':1,'SELL':-1}.get(p.get('direction'))
        mfe=None
        if entry is not None and sign is not None and extreme is not None:
            mfe=sign*(float(extreme)-float(entry))
        elif entry is not None and sign is not None:
            quote=self.market.get('bid') if sign==1 else self.market.get('ask')
            if quote is not None:
                mfe=sign*(float(quote)-float(entry))
        stop_now=_num(p.get('stopLevel'))
        be_locked=(entry is not None and stop_now is not None and sign is not None
                   and sign*(float(stop_now)-float(entry))>=-1e-12)
        structure=meta.get('structure') or self._trail_structure_label(p,candles)
        arm=structure_edge.trail_arm_decision(
            self.store,epic=p.get('epic'),structure=structure,
            direction=p.get('direction'),mfe=mfe,
            original_risk=meta.get('original_risk'),be_locked=be_locked)
        if not arm.get('arm'):
            self.protection_brief={
                'status':'Trail waiting for adaptive arm evidence',
                'deal_id':p['dealId'],'extreme':extreme,'trail_arm':arm,
                'phase':'watch'}
            return
        mark=self.market.get('time') or self.market_context.get('bar_time')
        proposal=propose_broker_trail(p,self.market,meta,extreme=extreme,mark_time=mark,
                                      zone_range=float(zone_range) if zone_range is not None else None,
                                      store=self.store,arm_decision=arm,candles=candles)
        self.protection_brief={'status':'Broker trail proposal' if proposal else 'No broker-native trail improvement',
                               'deal_id':p['dealId'],'extreme':extreme,'proposal':proposal,
                               'trail_arm':arm,
                               'phase':(proposal or {}).get('phase') or ('armed' if arm.get('arm') else 'watch')}
        if not proposal:return
        runtime=self.store.get('adaptive_runtime:'+str(p['dealId']),{}) or {}
        now_mono=time.monotonic()
        # After BE, ratchet SL promptly as each 1m close swing advances —
        # physical stop location must move on the broker, not only in memory.
        be_now=bool(proposal.get('be_locked') or proposal.get('phase') in (
            'trail','be_lock'))
        if runtime.get('status')=='REJECTED':
            wait=12.0
        elif be_now:
            wait=1.5
        else:
            wait=3.0
        last_mono=runtime.get('mono')
        if last_mono is not None and (now_mono-float(last_mono))<wait:return
        # Skip no-op PUTs that would leave SL at the same price.
        prev_stop=_num(runtime.get('stop'))
        if (prev_stop is not None and abs(float(proposal['stop_level'])-float(prev_stop))
                < float(self.market.get('tick') or 0)*0.5):
            return
        stop=proposal['stop_level']
        # Always re-send live TP on trail. Omitting profitLevel on Capital PUT
        # turns Take Profit Off — TP mark can be touched with no fill left.
        tp=proposal.get('profit_level')
        if tp is None:tp=_num(p.get('profitLevel'))
        # Trail moves STOP only. TP near market must not freeze SL protection —
        # validate stop vs bid/ask; still re-send TP so Capital keeps it on.
        if not levels_meet_broker_min(p.get('direction'),stop,None,
                                      self.market.get('bid'),self.market.get('ask'),
                                      self.market.get('min_distance')):
            self.store.event('warning','Trail skipped: SL below broker min distance',{
                'deal_id':p.get('dealId'),'stop':stop,'profit':tp,
                'min_distance':self.market.get('min_distance'),
                'tick':self.market.get('tick'),'bid':self.market.get('bid'),
                'ask':self.market.get('ask'),'stop_only_check':True})
            self.protection_brief={'status':'Trail skipped: SL below broker min',
                                   'deal_id':p['dealId'],'proposal':proposal}
            return
        oid=self.store.order('PROTECT',p['dealId'],{'stop':stop,'profit':tp,
            'tick':self.market['tick'],'reason':'BROKER_TRAIL','extreme':extreme,
            'min_distance':self.market.get('min_distance'),'preserve_tp':tp is not None,
            'trail_arm':arm.get('reason'),'phase':proposal.get('phase')})
        try:
            receipt=await self.broker.update_position(p['dealId'],stop_level=stop,profit_level=tp)
            if await self._confirm_reconcile(receipt,oid,'PROTECT',p['dealId']):
                self.store.set('adaptive_runtime:'+str(p['dealId']),{
                    'mono':now_mono,'stop':stop,'profit':tp,'extreme':extreme,'status':'CONFIRMED',
                    'mark':mark,'phase':proposal.get('phase')})
                self.protection_brief['status']='Broker-confirmed trail update'
                self.store.event('protection','Trail SL verified by broker',{
                    'deal_id':p['dealId'],'new_stop':stop,'profit_level':tp,'extreme':extreme,
                    'phase':proposal.get('phase'),
                    'min_distance':self.market.get('min_distance'),'tick':self.market.get('tick')})
            else:
                self.protection_brief['status']='Broker rejected trail; old levels retained'
                self.store.set('adaptive_runtime:'+str(p['dealId']),{
                    'mono':now_mono,'status':'REJECTED','mark':mark})
                self.store.event('warning','Trail SL rejected; original levels retained',
                                 {'deal_id':p['dealId'],'stop':stop,'extreme':extreme,
                                  'min_distance':self.market.get('min_distance'),'tick':self.market.get('tick')})
        except Exception as exc:
            # Common: SL/TP filled while trail PUT was in flight.
            if not await self._position_still_open(p.get('dealId')):
                self.store.order_update(oid,'SUPERSEDED')
                self.protection_brief={'status':'Trail skipped — deal already flat on broker'}
                self.store.event('reconcile','Trail PUT raced broker close; not suspending',{
                    'deal_id':p.get('dealId'),'type':type(exc).__name__})
                self._release_execution_after_flat(p.get('dealId'))
                return
            # Keep live SL/TP; never freeze the whole robot on a trail glitch.
            self.store.order_update(oid,'SUPERSEDED')
            self.protection_brief['status']='Trail uncertain — kept live broker levels (not paused)'
            self.store.event('warning','Trail protection uncertain; keeping live levels without pause',{
                'deal_id':p['dealId'],'type':type(exc).__name__,
                'broker_status':getattr(getattr(exc,'response',None),'status_code',None),
                'broker_error':_safe_broker_error(exc)})

    def _needs_profit_restore(self,p):
        """True when robot OPEN never got a TP (Capital SL-on / TP-Off).

        Trail may intentionally clear profitLevel — do not fight that every poll.
        Only restore when we still have planned profit_distance and trail has
        not marked TP Off / BE-TRAIL active.
        """
        if not p or p.get('profitLevel') is not None:
            return False
        deal=str(p.get('dealId') or '')
        if not deal:
            return False
        brain=self.store.get('entry_brain:'+deal) or {}
        if not isinstance(brain,dict):
            return False
        # Only robot-owned / planned brackets — never invent TP on pure manual.
        if not (brain.get('robot_open') or brain.get('profit_distance')
                or brain.get('bracket_source')=='ohlc_zone'):
            return False
        prot=self.store.get('adaptive_protection:'+deal) or {}
        if isinstance(prot,dict) and prot.get('tp_cleared_for_trail'):
            return False
        # If trail already advanced past BE, Capital TP Off is intentional.
        if isinstance(prot,dict) and (prot.get('be_locked') or prot.get('trail_active')
                                       or prot.get('phase') in ('TRAIL','BE','ARMED')):
            return False
        pd=brain.get('profit_distance')
        try:
            if pd is not None and float(pd)<=0:
                return False
        except (TypeError,ValueError):
            pass
        return True

    async def protect_position(self,p):
        if self.pending_order or self.pending_close:return
        if p.get('stopLevel') is not None and p.get('profitLevel') is not None:
            self.protected.add(p['dealId']);return
        # Deal-scoped unresolved — same policy as adapt_protection.
        deal_s=str(p.get('dealId'))
        blocking=[]
        for item in (self.store.unresolved() or []):
            if str(item.get('deal_id') or '')!=deal_s:
                continue
            if item.get('kind')=='PROTECT':
                self.store.order_update(item['id'],'SUPERSEDED')
            else:
                blocking.append(item)
        if blocking:return
        min_dist=self.market.get('min_distance');tick=self.market.get('tick');mid=self.market.get('mid')
        if min_dist is None or not tick or not mid:
            # Skip this tick — do NOT disable execution (that froze OPEN after every close).
            self.store.event('warning','Protection deferred: unsupported broker distance/price rule')
            return
        candles=self.store.candles(p.get('epic') or self.mandate.get('epic'),n=120,
                                   historical_only=True)
        zone=bracket_learn.zone_at_open(candles)
        rng=zone['range_distance'] or ohlc_range_distance(candles)
        spread=float(self.market.get('spread') or 0)
        buffer=max(2*float(tick),2*spread)
        base_sl=max(float(min_dist)+buffer,float(rng or 0))
        bp=bracket_learn.propose_for_open(self.store,ohlc_kappa=zone.get('kappa'),
                                         base_sl=base_sl,max_tp=zone.get('max_tp'),
                                         candles=candles,tick=tick,spread=spread,
                                         min_distance=min_dist)
        # Use OHLC-decided distances — do not re-scale range via stop_take×sl_mult
        # (that re-opened the win-ratchet path after 3 wins).
        decided_sl=_num(bp.get('stop_distance'))
        decided_tp=_num(bp.get('profit_distance'))
        if decided_sl and decided_tp and decided_sl>0 and decided_tp>0:
            sign=1 if p.get('direction')=='BUY' else -1
            level=float(p['level'] or mid)
            result={
                'stop_distance':float(decided_sl),'profit_distance':float(decided_tp),
                'stop_level':level-sign*float(decided_sl),
                'profit_level':level+sign*float(decided_tp),
                'kappa':bp.get('kappa'),'sl_scale':1.0,
            }
        else:
            result=stop_take(p['level'] or mid,p['direction'],tick,self.market['spread'],min_dist,
                             kappa=bp['kappa'],sl_scale=1.0,
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
                if not await self._position_still_open(p.get('dealId')):
                    self.store.order_update(order_id,'SUPERSEDED',receipt.get('dealReference'))
                    self._release_execution_after_flat(p.get('dealId'))
                    return
                # Rejected while still open — keep existing levels, do not pause.
                self.store.order_update(order_id,'SUPERSEDED',receipt.get('dealReference'))
                self.store.event('warning','Protection rejected; kept live levels without pause',
                    {'deal_id':p.get('dealId'),'reference':receipt.get('dealReference'),
                     'requested_stop':str(stop),'requested_profit':str(profit)})
        except Exception as exc:
            if not await self._position_still_open(p.get('dealId')):
                self.store.order_update(order_id,'SUPERSEDED')
                self.store.event('reconcile','PROTECT raced broker close; not suspending',{
                    'deal_id':p.get('dealId'),'type':type(exc).__name__})
                self._release_execution_after_flat(p.get('dealId'))
                return
            self.store.order_update(order_id,'SUPERSEDED')
            self.store.event('warning','Position protection uncertain; kept live levels without pause',
                {'type':type(exc).__name__,
                 'broker_status':getattr(getattr(exc,'response',None),'status_code',None),
                 'broker_error':_safe_broker_error(exc),'deal_id':p.get('dealId'),
                 'stop_level':str(stop),'take_profit_level':str(profit)})
    async def exit_position(self,deal_id):
        if self.pending_order or self.pending_close or not self.execution_enabled:return
        self.pending_close=True
        oid=self.store.order('CLOSE',deal_id,{'deal_id':deal_id})
        try:
            receipt=await self.broker.close_position(deal_id)
            if await self._confirm_reconcile(receipt,oid,'CLOSE',deal_id):
                self._release_execution_after_flat(deal_id)
            elif not await self._position_still_open(deal_id):
                self.store.order_update(oid,'SUPERSEDED',receipt.get('dealReference'))
                self._release_execution_after_flat(deal_id)
            else:
                self.store.order_update(oid,'SUPERSEDED',receipt.get('dealReference'))
                self.store.event('warning','CLOSE not reconciled while still open; not freezing robot',
                    {'deal_id':deal_id,'reference':receipt.get('dealReference')})
        except Exception as exc:
            if not await self._position_still_open(deal_id):
                self.store.order_update(oid,'SUPERSEDED')
                self.store.event('reconcile','CLOSE raced broker flat; not suspending',
                    {'deal_id':deal_id,'type':type(exc).__name__})
                self._release_execution_after_flat(deal_id)
            else:
                self.store.order_update(oid,'SUPERSEDED')
                self.store.event('warning','Exit outcome uncertain while still open; not freezing robot',
                    {'type':type(exc).__name__,'deal_id':deal_id})
        finally:self.pending_close=False
    async def submit(self,action,broker_market=None):
        if self.pending_order or self.pending_close or not self.armed or not self.execution_enabled or not self.mandate:return
        if action not in ('BUY','SELL') or self.last_closed_step<0:return
        # Uncertain prior OPEN/CLOSE must not spawn a second ticket.
        if self.store.unresolved():return
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
        # Load profitable version only when current market task matches its context.
        try:
            ctx=champion_library.context_from(
                epic=self.mandate.get('epic'),
                trend=(self.market_context or {}).get('trend')
                      or (self.last_signal or {}).get('trend'),
                regime=(self.market_context or {}).get('regime')
                       or (self.last_signal or {}).get('regime'),
                news=self._news_risk())
            champion_library.maybe_activate(self.store,self,context=ctx)
        except Exception as exc:
            self.store.event('warning','Champion activate skipped',
                             {'type':type(exc).__name__})
        self.pending_order=True
        try:
            before_ids={str(p['dealId']) for p in (self.positions or []) if p.get('dealId')}
            await self.sync()
            if not self._external_entry_check(action)['entry_allowed']:return
            market=broker_market or await self.broker.market(self.mandate['epic'])
            snap=market.get('snapshot',{});tick=self._tick(market)
            mid=self.market.get('mid');spread=self.market.get('spread')
            if snap.get('marketStatus') not in (None,'TRADEABLE') or not tick or mid is None or spread is None:return
            minimum=self._min_distance(market,mid)
            if minimum is None:
                self.store.event('warning','Unrecognized instrument SL/TP distance; skipped order')
                return
            entry=self.market.get('ask') if action=='BUY' else self.market.get('bid')
            # OHLC zone decides SL/TP across M1/M5/M15/H1; cycle only gates edge.
            # Use the same history need as feature validation (≥260), not a frozen 190.
            hist_n=recommended_live_history_bars(self.store)
            candles=self.store.candles(self.mandate['epic'],n=hist_n,historical_only=False)
            zone=bracket_learn.zone_at_open(candles,multi_tf=True)
            rng=zone['range_distance'] or ohlc_range_distance(candles)
            buffer=max(2*float(tick),2*float(spread))
            base_sl=max(float(minimum)+buffer,float(rng or 0))
            learned=bracket_learn.load_multipliers(self.store)
            cycle_state=unified_cycle.load_state(self.store)
            scores,_vote_src=self._entry_vote_scores()
            if not scores and isinstance(self.last_signal,dict):
                scores=self.last_signal.get('scores')
            score_edge=(self.policy.score_edge(scores) if scores else None)
            evaluation={'action':action,'score_edge':score_edge,'vote_source':_vote_src}
            pkg=unified_cycle.compose_open(
                action=action,scores=scores,evaluation=evaluation,zone=zone,
                learned=learned,cycle_state=cycle_state,base_sl=base_sl,
                max_tp=zone.get('max_tp'),store=self.store)
            if pkg.get('blocked') or pkg.get('action') not in ('BUY','SELL'):
                self.last_entry_reason=pkg.get('reason') or 'Unified cycle HOLD'
                self.store.event('entry','OPEN skipped — unified cycle edge gate',{
                    'reason':self.last_entry_reason,'decision':pkg.get('decision'),
                    'aggression':pkg.get('aggression'),'min_margin':pkg.get('min_margin')})
                return
            decided=bracket_learn.decide_brackets_from_ohlc(
                zone,tick=tick,spread=spread,min_distance=minimum,
                tighten=float(pkg.get('tighten') or 1.0),store=self.store)
            # SL/TP from OHLC only — no hardcoded RR reshape.
            sign=1 if action=='BUY' else -1
            sd=float(decided['stop_distance']);pd=float(decided['profit_distance'])
            brackets={
                'stop_distance':sd,'profit_distance':pd,
                'stop_level':float(entry)-sign*sd,
                'profit_level':float(entry)+sign*pd,
                'stop_ticks':decided.get('stop_ticks'),
                'profit_ticks':decided.get('profit_ticks'),
                'kappa':decided.get('kappa'),'sl_scale':1.0,
                'raised_to_broker_min':decided.get('raised_to_broker_min'),
                'bracket_source':'ohlc_zone',
            }
            bp={'kappa':decided['kappa'],'sl_scale':1.0,'explored':False,
                'kappa_mult':learned.get('kappa'),'sl_mult':learned.get('sl_scale'),
                'median_up':zone.get('median_up'),'median_down':zone.get('median_down'),
                'lookback':zone.get('lookback'),
                'nudge_samples':len(self.store.get('bracket_outcome_history',[]) or []),
                'aggression':pkg.get('aggression'),'edge_scale':pkg.get('edge_scale'),
                'tighten':decided.get('tighten'),'bracket_source':'ohlc_zone'}
            bracket_learn.save_params(
                self.store,bp['kappa'],1.0,
                reason=(f"ohlc_zone κ={decided.get('ohlc_kappa')} "
                        f"sl={decided.get('ohlc_sl')} tp={decided.get('ohlc_tp')} "
                        f"tighten={float(decided.get('tighten') or 1):.3f}"))
            bracket_learn.note_open_bracket_meta(self.store,decided)
            # Audit #9: after coeffs/rounding, refuse non-compliant SL/TP.
            if not distances_meet_broker_min(brackets['stop_distance'],
                                             brackets['profit_distance'],minimum):
                self.store.event('warning','OPEN skipped: SL/TP below broker min distance',{
                    'stop_distance':float(brackets['stop_distance']),
                    'profit_distance':float(brackets['profit_distance']),
                    'min_distance':float(minimum),'kappa':bp['kappa'],
                    'raised_to_broker_min':brackets.get('raised_to_broker_min')})
                return
            tick_val=tick_value_from_instrument(market.get('instrument') or {},tick)
            # Capital dealingRules size grid — never invent 0.1009-style lots.
            size_grid=dealing_size_grid(market,mandate_size=self.mandate['size'])
            # Day book: full account balance → lot pack; multi-pos when capacity remains.
            equity=self._account_equity()
            # When Capital omits valueOfOnePip:
            #  • fractional mode — calibrate so mandate ≈ risk_per_trade
            #  • full_balance — calibrate so open_slices×minDealSize ≈ equity
            #    cash-risk (never leave tick_val None → mandate glue / one chunk)
            tick_val_src='capital'
            if tick_val is None and brackets.get('stop_ticks') and equity:
                try:
                    params=day_book.load_params(self.store)
                    full_bal=int(params.get('size_full_balance') or 0)==1
                    st=float(brackets['stop_ticks'])
                    if full_bal and size_grid.get('ok') and st>0:
                        want=max(3,int(params.get('open_slices') or 5))
                        mn=float(Decimal(str(size_grid['min'])))
                        target=max(mn,mn*want)
                        if target>0 and equity>0:
                            tick_val=float(equity)/(target*st)
                            tick_val_src='full_balance_min_pack_calibrated'
                    elif not full_bal:
                        bmap=day_book.budgets(
                            equity=equity,params=params,
                            daily=day_book.load_daily(self.store,equity=equity))
                        rpt=_num(bmap.get('risk_per_trade'))
                        msize=float(Decimal(str(self.mandate['size'])))
                        if rpt and rpt>0 and msize>0 and st>0:
                            tick_val=rpt/(msize*st)
                            tick_val_src='mandate_risk_calibrated'
                except Exception:
                    tick_val=None
            _scores_for_align,_edge_for_align,_aligned=self._day_book_add_edge()
            # Flat = broker book empty after the submit sync. before_ids alone can
            # lie (stale local []); post-sync empty is the real flat pack signal.
            # force_flat still protects the split if a mid-burst sibling appears
            # later — plan_open must see the true book for ADD gating.
            flat_at_submit=len(self.positions or [])==0
            plan=day_book.plan_open(
                store=self.store,equity=equity,mandate_size=self.mandate['size'],
                stop_ticks=brackets.get('stop_ticks'),tick_value=tick_val,
                positions=self.positions,
                stop_ticks_by_deal=self._stop_ticks_by_deal(tick),
                score_edge=score_edge if score_edge is not None else _edge_for_align,
                epic=self.mandate.get('epic'),mandate_epic=self.mandate.get('epic'),
                market=market,trend_aligned=_aligned)
            if not plan.get('allowed') or plan.get('size') is None:
                self.last_entry_reason=plan.get('reason') or 'day_book_blocked'
                self.store.event('entry','OPEN skipped — day book',{
                    'reason':self.last_entry_reason,'phase':plan.get('phase'),
                    'open_count':plan.get('open_count'),
                    'budgets':plan.get('budgets'),'size_grid':plan.get('size_grid')})
                return
            size=snap_deal_size(
                plan['size'],min_size=size_grid['min'],step=size_grid['step'],
                max_size=size_grid.get('max')) if size_grid.get('ok') else None
            if size is None:
                self.last_entry_reason='size_off_capital_grid'
                self.store.event('entry','OPEN skipped — size not on Capital grid',{
                    'raw':str(plan.get('size')),'size_grid':{
                        'min':str(size_grid.get('min')),'step':str(size_grid.get('step')),
                        'max':str(size_grid.get('max')) if size_grid.get('max') is not None else None,
                        'source':size_grid.get('source')}})
                return
            if not on_deal_size_grid(
                    size,min_size=size_grid['min'],step=size_grid['step'],
                    max_size=size_grid.get('max')):
                self.last_entry_reason='size_grid_check_failed'
                return
            # Split one planned lot into simultaneous Capital-grid slices when
            # flat + advantageous (e.g. 5× BUY/SELL). Prefer N×minDealSize.
            # ADD path stays 1 slice. force_flat when we started flat so a
            # post-sync ghost cannot collapse 0.03 → one ticket.
            sit_struct=((self.last_signal or {}).get('structure_edge') or {}).get('structure')
            if not sit_struct:
                sit_struct=zone.get('structure')
            live_trend=(self.market_context or {}).get('trend') or (
                self.last_signal or {}).get('trend')
            returns_bps=(self.market_context or {}).get('returns_bps') or (
                self.last_signal or {}).get('returns_bps')
            slice_sizes=day_book.split_planned_sizes(
                size,store=self.store,positions=self.positions,size_grid=size_grid,
                score_edge=score_edge if score_edge is not None else _edge_for_align,
                action=action,live_trend=live_trend,structure=sit_struct,
                returns_bps=returns_bps,force_flat=flat_at_submit)
            if not slice_sizes:
                self.last_entry_reason='size_slice_empty'
                return
            for sl in slice_sizes:
                if not on_deal_size_grid(
                        sl,min_size=size_grid['min'],step=size_grid['step'],
                        max_size=size_grid.get('max')):
                    self.last_entry_reason='size_grid_check_failed'
                    return
            # SL/TP distances on the price tick grid (Capital POINTS).
            sd=snap_distance(brackets['stop_distance'],tick,minimum=minimum,round_up=True)
            pd=snap_distance(brackets['profit_distance'],tick,minimum=minimum,round_up=True)
            if sd is None or pd is None:
                self.store.event('warning','OPEN skipped: SL/TP could not snap to tick grid',{
                    'stop_distance':brackets['stop_distance'],
                    'profit_distance':brackets['profit_distance'],'tick':tick})
                return
            tick_d=Decimal(str(tick))
            brackets=dict(brackets,stop_distance=float(sd),profit_distance=float(pd),
                          stop_ticks=int((Decimal(str(sd))/tick_d).to_integral_value()),
                          profit_ticks=int((Decimal(str(pd))/tick_d).to_integral_value()))
            if not distances_meet_broker_min(brackets['stop_distance'],
                                             brackets['profit_distance'],minimum):
                self.store.event('warning','OPEN skipped: snapped SL/TP below broker min',{
                    'stop_distance':brackets['stop_distance'],
                    'profit_distance':brackets['profit_distance'],
                    'min_distance':float(minimum)})
                return
            total_size=sum(slice_sizes,Decimal('0'))
            proposed=None
            if tick_val is not None:
                try:
                    proposed=float(cash_risk(total_size,brackets['stop_ticks'],tick_val))
                except (ValueError,TypeError,ArithmeticError):
                    proposed=None
            gate=self._hard_risk_gate(proposed_risk=proposed)
            self.store.event('risk','Day-book OPEN cash-risk (Capital-grid sized)',{
                'proposed_risk':proposed,'size':str(total_size),
                'size_wire':broker_wire_number(total_size),
                'slices':[str(s) for s in slice_sizes],
                'slice_count':len(slice_sizes),
                'mandate_size':str(self.mandate['size']),
                'size_scale':plan.get('scale'),
                'size_grid':plan.get('size_grid') or {
                    'min':str(size_grid.get('min')),'step':str(size_grid.get('step')),
                    'max':str(size_grid.get('max')) if size_grid.get('max') is not None else None,
                    'source':size_grid.get('source')},
                'stop_ticks':int(brackets['stop_ticks']),
                'tick_value':float(tick_val) if tick_val is not None else None,
                'tick_value_source':tick_val_src,
                'limits':(gate or {}).get('limits'),'direction':action,
                'phase':plan.get('phase'),'day_budgets':plan.get('budgets'),
                'open_gated':False})
            self.store.event('bracket','OPEN brackets decided from multi-TF OHLC',{
                'kappa':bp['kappa'],'sl_scale':1.0,
                'ohlc_kappa':decided.get('ohlc_kappa'),
                'ohlc_sl':decided.get('ohlc_sl'),'ohlc_tp':decided.get('ohlc_tp'),
                'tighten':decided.get('tighten'),
                'median_up':bp.get('median_up'),'median_down':bp.get('median_down'),
                'lookback':bp.get('lookback') or zone.get('lookback'),
                'base_sl':base_sl,'max_tp':zone.get('max_tp'),
                'noise_distance':zone.get('noise_distance'),
                'structure':zone.get('structure'),
                'pull_share':zone.get('pull_share'),
                'favor_share':zone.get('favor_share'),
                'multi_tf':bool(zone.get('multi_tf')),
                'horizons_minutes':zone.get('horizons_minutes'),
                'timeframes':zone.get('timeframes'),
                'stop_distance':float(brackets['stop_distance']),
                'profit_distance':float(brackets['profit_distance']),
                'bracket_source':'ohlc_zone_multi_tf',
                'raised_to_broker_min':bool(brackets.get('raised_to_broker_min')),
                'cash_risk':proposed,
                'tick_value':float(tick_val) if tick_val is not None else None,
                'learn_samples':bp.get('nudge_samples'),
                'day_phase':plan.get('phase'),'size_scale':plan.get('scale'),
                'size':str(total_size),'slices':[str(s) for s in slice_sizes]})
            # Snapshot the neural state that voted for THIS entry — not a later bar.
            if self.brain.core.last_transition and len(self.brain.core.last_transition['h'])==self.brain.core.n:
                open_ctx=champion_library.context_from(
                    epic=self.mandate['epic'],
                    trend=(self.market_context or {}).get('trend')
                          or (self.last_signal or {}).get('trend'),
                    regime=(self.market_context or {}).get('regime')
                           or (self.last_signal or {}).get('regime'),
                    news=self._news_risk())
                sit_fp=self._situation_fingerprint_now(structure=sit_struct)
                self._pending_entry_brain={
                    'epic':self.mandate['epic'],'direction':action,'entry':entry,
                    'tick_size':tick,'entry_spread':spread,
                    'transition':self.brain._json_transition(self.brain.core.last_transition),
                    'scores':(self.last_signal or {}).get('scores'),
                    'shadow_scores':self._shadow_scores_now(),
                    'bar_time':self.last_decision_bar,'captured':'submit',
                    'bracket_kappa':bp['kappa'],'bracket_sl_scale':bp['sl_scale'],
                    'bracket_explored':bool(bp.get('explored')),
                    'cycle_aggression':bp.get('aggression'),
                    'stop_distance':float(brackets['stop_distance']),
                    'profit_distance':float(brackets['profit_distance']),
                    'champion_context':open_ctx,
                    'day_phase':plan.get('phase'),
                    'size':str(total_size),'size_scale':plan.get('scale'),
                    'slices':[str(s) for s in slice_sizes],
                    # Situation at OPEN — £ banks into this scene on close.
                    'situation':sit_fp,
                    'trend':(self.market_context or {}).get('trend'),
                    'returns_bps':(self.market_context or {}).get('returns_bps'),
                    'structure':sit_struct,
                }
            self.last_entry_signal_step=self.brain.core.steps
            # Freeze UI scores at the OPEN decision before manage ticks start.
            # Only freeze when this is the first open — multi-add keeps manage votes live.
            if not before_ids:
                self._lock_entry_signal_for_open()
            pending_brain=self._pending_entry_brain
            opened_slices=0
            # Flat AI OPEN → fire the *whole* planned multi-lot pack in this
            # submit (all lot sizes now). Do not abort after slice 0 because a
            # sibling OPEN confirm is still settling / unresolved.
            flat_burst=bool(flat_at_submit) and len(slice_sizes)>1
            burst_oids=set()
            try:
                for slice_i,size in enumerate(slice_sizes):
                    if slice_i>0 and not flat_burst:
                        # Later ADD path: one slice per call — capacity checks apply.
                        await self.sync()
                        if self.store.unresolved():
                            self.store.event('entry',
                                'Multi-lot OPEN stopped — unresolved after prior slice',
                                {'opened_slices':opened_slices,
                                 'remaining':len(slice_sizes)-slice_i})
                            break
                        room=day_book.allows_new_open(
                            store=self.store,equity=equity,positions=self.positions,
                            epic=self.mandate.get('epic'),
                            mandate_epic=self.mandate.get('epic'),
                            score_edge=score_edge if score_edge is not None else _edge_for_align,
                            trend_aligned=True,
                            skip_add_edge=True)
                        if not room.get('allowed'):
                            self.store.event('entry',
                                'Multi-lot OPEN stopped — day book capacity',
                                {'reason':room.get('reason'),
                                 'opened_slices':opened_slices})
                            break
                    elif slice_i>0 and flat_burst:
                        # Keep book fresh for dealId binding; never stop the pack
                        # on our own in-flight OPEN rows from earlier slices.
                        await self.sync()
                        foreign=[u for u in (self.store.unresolved() or [])
                                 if u.get('id') not in burst_oids
                                 and str(u.get('deal_id') or '') not in before_ids]
                        if foreign:
                            self.store.event('entry',
                                'Multi-lot OPEN pack continues despite foreign unresolved',
                                {'foreign':len(foreign),'opened_slices':opened_slices})
                        # Pack was pre-planned for len(slice_sizes) tickets —
                        # do not stop early on a stale evolved max_positions=1.
                        try:
                            max_pos=max(5,int(day_book.load_params(self.store).get(
                                'max_positions') or 5),len(slice_sizes))
                        except Exception:
                            max_pos=max(5,len(slice_sizes))
                        if len(self.positions or [])>=max_pos:
                            self.store.event('entry',
                                'Multi-lot OPEN pack hit max_positions',
                                {'opened_slices':opened_slices,'max_positions':max_pos})
                            break
                    slice_before={str(p['dealId']) for p in (self.positions or [])
                                  if p.get('dealId')}
                    # Durable order record BEFORE a real order is sent.
                    oid=self.store.order(
                        'OPEN',None,{
                            'epic':self.mandate['epic'],'direction':action,
                            'size':str(size),
                            'stop_distance':str(brackets['stop_distance']),
                            'profit_distance':str(brackets['profit_distance']),
                            'kappa':bp['kappa'],'sl_scale':bp['sl_scale'],
                            'day_phase':plan.get('phase'),
                            'slice_index':slice_i,
                            'slice_count':len(slice_sizes),
                            'flat_burst':bool(flat_burst)})
                    if oid is not None:
                        burst_oids.add(oid)
                    receipt=await self.broker.open_position(
                        self.mandate['epic'],action,size,
                        stop_distance=brackets['stop_distance'],
                        profit_distance=brackets['profit_distance'])
                    self.last_order_ref=receipt.get('dealReference')
                    confirmed=await self._confirm_reconcile(receipt,oid,'OPEN')
                    if confirmed is False:
                        # Broker REJECTED this slice (margin / filters). Do not
                        # count it as filled; stop the pack to avoid hammering.
                        self.store.event('entry',
                            'Multi-lot OPEN slice rejected — stopping pack',
                            {'slice_index':slice_i,'size':str(size),
                             'opened_slices':opened_slices})
                        break
                    opened_slices+=1
                    if self.positions and pending_brain:
                        after={str(p['dealId']):p for p in self.positions
                               if p.get('dealId')}
                        new_ids=[d for d in after if d not in slice_before]
                        deal_id=None
                        # Prefer confirm receipt dealId when present.
                        conf_deal=None
                        try:
                            conf_deal=(receipt or {}).get('dealId') or (
                                (receipt or {}).get('dealReference'))
                        except Exception:
                            conf_deal=None
                        if conf_deal is not None and str(conf_deal) in after:
                            deal_id=str(conf_deal)
                        elif len(new_ids)==1:
                            deal_id=new_ids[0]
                        else:
                            latest=self.store.get('latest_robot_deal_id')
                            if latest is not None and str(latest) in after:
                                deal_id=str(latest)
                            elif len(new_ids)>1:
                                # Stable pick: newest id not yet bound.
                                unbound=[d for d in new_ids
                                         if self.store.get('entry_brain:'+d) is None]
                                deal_id=unbound[-1] if unbound else new_ids[-1]
                            elif len(after)==1:
                                deal_id=next(iter(after))
                        key='entry_brain:'+str(deal_id) if deal_id else None
                        pos=after.get(str(deal_id)) if deal_id else {}
                        if deal_id and key and self.store.get(key) is None:
                            self.store.set(key,{**pending_brain,
                                'size':str(size),
                                'slice_index':slice_i,
                                'entry':(pos.get('level') if pos and pos.get('level') is not None
                                         else pending_brain.get('entry'))})
                        elif pending_brain and not deal_id:
                            self.store.event('warning',
                                'OPEN entry_brain not bound — ambiguous multi-pos dealId',
                                {'new_ids':new_ids,'open':list(after.keys()),
                                 'slice_index':slice_i})
                        day_book.record_open(self.store,equity=equity)
                if opened_slices>0:
                    unified_cycle.note_open(self.store,action=action)
                    self.store.event('entry','Multi-lot OPEN burst',{
                        'direction':action,'opened_slices':opened_slices,
                        'planned_slices':len(slice_sizes),
                        'sizes':[str(s) for s in slice_sizes[:opened_slices]],
                        'flat_burst':bool(flat_burst),
                        'full_pack':opened_slices==len(slice_sizes)})
                self._pending_entry_brain=None
            except Exception as exc:
                # Keep UNCERTAIN so a second OPEN cannot fire; operator Resume clears.
                # Keep _pending_entry_brain — Capital often filled while confirm failed;
                # next sync adopt/supersede must claim robot_open + trail + brain.
                if 'oid' in locals() and oid is not None:
                    self.store.order_update(oid,'UNCERTAIN',self.last_order_ref)
                if pending_brain and self._pending_entry_brain is None:
                    self._pending_entry_brain=pending_brain
                opened=opened_slices if 'opened_slices' in locals() else 0
                # Partial multi-lot fill: keep execution so trail/protect continue.
                if opened<=0:
                    self.execution_enabled=False
                self.store.event('error' if opened<=0 else 'warning',
                    ('Entry outcome uncertain; no automatic retry until Resume'
                     if opened<=0 else
                     'Multi-lot OPEN partial — keeping execution for filled slices'),
                    {'type':type(exc).__name__,
                     'opened_slices':opened,
                     'pending_entry_brain_kept':bool(self._pending_entry_brain),
                     'execution_enabled':bool(self.execution_enabled)})
        finally:self.pending_order=False
    def _manage_perceive_ohlc(self,*,mid,spread,tick,epic,bar_t,features):
        """Forward-only manage scores when fusion EXTERNAL is gapped.

        Never fabricates EXTERNAL for a flat OPEN — only while in_position so a
        peer/news lag cannot freeze reverse CLOSE. Pads last-good tails when
        present; otherwise zeros the EXTERNAL block (manage exit only).
        """
        brain=self.brain
        if brain is None or features is None:
            return None
        try:
            feats=[float(x) for x in features]
        except (TypeError,ValueError):
            return None
        if brain.feature_schema!=brain.fusion_schema:
            return brain.tick(mid,spread,tick,epic,dt=60,features=feats,
                              bar_time=bar_t,in_position=True)
        from .market_fusion import EXTERNAL_NAMES
        need=int(brain.ohlc_dim)
        if len(feats)!=need:
            return None
        ext=[0.]*len(EXTERNAL_NAMES)
        peers=self._last_good_peers
        news=self._last_good_news
        liq=self._last_good_liquidity
        if isinstance(peers,(list,tuple)) and len(peers)>=6:
            ext[0:6]=[float(x) for x in peers[:6]]
        if isinstance(news,(list,tuple)) and len(news)>=2:
            ext[6:8]=[float(x) for x in news[:2]]
        if isinstance(liq,(list,tuple)) and liq:
            n=min(len(liq),len(ext)-8)
            if n>0:
                ext[-n:]=[float(x) for x in liq[-n:]]
        fused_vec=feats+ext
        fdim=int(brain.fusion_dim)
        if len(fused_vec)<fdim:
            fused_vec=fused_vec+[0.]*(fdim-len(fused_vec))
        elif len(fused_vec)>fdim:
            fused_vec=fused_vec[:fdim]
        return brain.tick(mid,spread,tick,epic,dt=60,features=fused_vec,
                          bar_time=bar_t,external_features=True,in_position=True)

    def _lock_entry_signal_for_open(self):
        """Pin Admin/UI BUY/HOLD/SELL to the OPEN vote for the whole deal."""
        import copy
        if self._locked_entry_signal is None and isinstance(self.last_signal,dict):
            locked=copy.deepcopy(self.last_signal)
            locked['in_position']=True
            locked['entry_train']=False
            locked['scores_frozen']=True
            self._locked_entry_signal=locked
        if self._locked_entry_signal is not None:
            # Always re-publish the locked OPEN scores — never mid-trade perceive.
            self.last_signal=copy.deepcopy(self._locked_entry_signal)

    def _existing_open_block_reason(self):
        """Name the live deal(s) + UPL; explain manage reverse watch vs waiting."""
        if not self.positions:return 'Existing open position'
        n=len(self.positions)
        p=self.positions[0]
        deal=p.get('dealId')
        upl=_num(p.get('upl'))
        if n>1:
            try:
                upl=sum(float(x) for x in (_num(q.get('upl')) for q in self.positions)
                        if x is not None and math.isfinite(x))
            except (TypeError,ValueError):
                pass
        manage=self._manage_signal if isinstance(self._manage_signal,dict) else None
        if self.last_signal is None and not (manage and manage.get('scores')):
            fusion_cold=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                         and getattr(self.brain,'fusion_schema',None) is not None
                         and not self.latest_fusion_ready)
            head=('Existing open position · waiting for neural fusion vote'
                  if fusion_cold else
                  'Existing open position · waiting for first M1 neural vote')
        elif manage and manage.get('action'):
            head=('Existing open position · reverse watch ({0})'.format(
                manage.get('action')))
        else:
            head='Existing open position'
        if n>1:
            head=head.replace('Existing open position',
                              'Existing open positions ({0})'.format(n),1)
        bits=[head]
        if n==1 and deal is not None:
            bits.append('deal '+str(deal))
        elif n>1:
            ids=[str(q.get('dealId')) for q in self.positions if q.get('dealId')]
            bits.append('deals '+','.join(ids[:4])+(('…') if len(ids)>4 else ''))
        if upl is not None and math.isfinite(upl):
            bits.append('UPL {0:.2f}'.format(float(upl)))
        return ' — '.join(bits)

    def _compose_entry_block_reason(self,*,readiness,evaluation,decision):
        """Name the real OPEN blocker — never mask hard-risk as M1 wait."""
        # Auto-pause is banned — heal sticky armed flags before reporting.
        self._ensure_live_unless_operator_paused()
        if self.operator_paused:
            return 'Paused by operator'
        if self.store.unresolved():
            # Flat leftovers must not freeze Admin forever. Also heal stale
            # PROTECT/OPEN journal rows while a live deal is already on broker —
            # otherwise "Unresolved broker operation" blocks the whole rebound.
            self.clear_unresolved_on_operator_resume()
            self._ensure_live_unless_operator_paused()
            if self.store.unresolved():
                return 'Unresolved broker operation'
        if self.positions:
            _scores,_edge,_aligned=self._day_book_add_edge()
            book=day_book.allows_new_open(
                store=self.store,equity=self._account_equity(),
                positions=self.positions,
                epic=(self.mandate or {}).get('epic'),
                mandate_epic=(self.mandate or {}).get('epic'),
                score_edge=_edge,trend_aligned=_aligned)
            if not book.get('allowed'):
                return (self._existing_open_block_reason()
                        + ' · day book: ' + str(book.get('reason') or book.get('phase')))
            # Capacity for ADD left — still name the open book when cold-start /
            # market context cannot actually fire another OPEN yet.
            if self.last_closed_step<0 or not self.market_context.get('ready'):
                return self._existing_open_block_reason()
            # Otherwise fall through to readiness/signal reasons for ADD.
        if not self.mandate:
            return 'No adoptive size this session — open a new manual trade'
        if self.last_closed_step<0:
            return 'Waiting for first manually opened trade to close'
        if not self.market_context.get('ready'):
            return 'Market context unavailable: '+self.market_context.get('reason','')
        if self._fusion_entry_blocked():
            return ('External fusion gap blocks new entry: '+(
                self.fusion_block_reason or 'aligned external neural inputs unavailable'))
        from . import model_readiness as _mr
        if not _mr.allows_live_open(readiness):
            return 'Model prep/eval not ready: '+(readiness.get('reason') or '')
        bar_done=(self.last_entry_processed_bar==self.last_decision_bar
                  and self.last_decision_bar is not None)
        reason=self.last_entry_reason
        if bar_done and reason and getattr(self,'last_policy_decision',None)=='HOLD':
            return reason
        if bar_done:
            return 'Waiting for next completed M1 candle'
        if self.last_signal:
            return self._signal_reason(decision,self.market.get('status'),evaluation)
        on_fusion=(getattr(self.brain,'feature_schema',None)==getattr(self.brain,'fusion_schema',None)
                   and getattr(self.brain,'fusion_schema',None) is not None)
        if on_fusion:
            return 'Waiting for aligned external neural inputs'
        return 'Waiting for signal'

    def snapshot(self):
        account=self.account.get('accounts',[]) if isinstance(self.account,dict) else []
        active=next((a for a in account if a.get('preferred') or a.get('accountId')==self.account.get('currentAccountId')),account[0] if account else {})
        _snap_scores,_=self._entry_vote_scores()
        _snap_edge=(self.policy.score_edge(_snap_scores)
                    if _snap_scores and hasattr(self.policy,'score_edge') else None)
        book_open=day_book.allows_new_open(
            store=self.store,equity=self._account_equity(),
            positions=self.positions,
            epic=(self.mandate or {}).get('epic'),
            mandate_epic=(self.mandate or {}).get('epic'),
            score_edge=_snap_edge)
        evaluation=(self._entry_evaluation()
                    if book_open.get('allowed') else None)
        news=self._news_risk()
        intermarket=self.intermarket.state
        readiness=(self.brain.trading_readiness()
                   if hasattr(self.brain,'trading_readiness') else {'ready':False})
        from . import model_readiness as _mr_snap
        live_ready=_mr_snap.allows_live_open(readiness)
        hr_snap=hard_risk.snapshot(self.store)
        db_snap=day_book.snapshot(
            self.store,equity=self._account_equity(),positions=self.positions)
        minute_acc=(self.brain.correct/self.brain.labeled) if self.brain.labeled else None
        # Closed-trade win rate is separate from minute-direction accuracy (audit #24).
        trade_wins=trade_losses=0
        try:
            for t in self.store.trades(200):
                if not t.get('closed') or t.get('pnl') is None:continue
                src=(t.get('pnl_source') or '').strip().lower()
                # Match Admin card: only broker cash counts as W/L.
                if src!='broker':continue
                try:val=float(t['pnl'])
                except (TypeError,ValueError):continue
                if val>0:trade_wins+=1
                elif val<0:trade_losses+=1
        except Exception:
            pass
        trade_n=trade_wins+trade_losses
        trade_win_rate=(trade_wins/trade_n) if trade_n else None
        # Exit intent from manage scores when present (frozen OPEN is fallback).
        raw_exit_intent=None
        if self.positions:
            m_scores=((self._manage_signal or {}).get('scores')
                      if isinstance(self._manage_signal,dict) else None)
            s_scores=(self.last_signal or {}).get('scores') if isinstance(self.last_signal,dict) else None
            use=m_scores or s_scores
            dirs={p.get('direction') for p in self.positions
                  if p.get('direction') in ('BUY','SELL')}
            # Multi-pos mixed directions → no single exit intent label.
            if use and len(dirs)==1:
                raw_exit_intent=self.policy.neural_decision(
                    use,next(iter(dirs)))
            elif use and len(dirs)>1:
                raw_exit_intent=self.policy.neural_decision(use)
        decision=(raw_exit_intent if self.positions else
                  (evaluation['action'] if evaluation else 'HOLD'))
        gates=self.live_open_gates(evaluation['action'] if evaluation else None)
        # Snapshot publishes locked OPEN (or manage-seeded) scores — not every
        # mid-trade manage flicker (Admin freezes client-side too).
        if self.positions:
            self._lock_entry_signal_for_open()
        ui_signal=self._locked_entry_signal if (self.positions and self._locked_entry_signal) else self.last_signal
        return {'connected':bool(self.broker),'armed':self.armed,'execution_enabled':self.execution_enabled,
                'mandate':self.mandate,'data_dir':str(self.store.path.parent),
                'positions':self.positions,'market':self.market,'signal':ui_signal,
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
                'neural_adapt_reason':self.store.get('neural_adapt_reason'),
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
                'neural_entry_rule':'neural argmax proposes; evolved policy.py confirms or HOLDs when policy_live_execute=1; readiness+utility separate',
                # Confirmed reverse (fresh manage scores + streak + 5/15m) may market-exit.
                'neural_market_exit':True,
                'neural_close_diagnostic_only':False,
                'neural_close_executable':True,
                'neural_exit_intent':raw_exit_intent,
                'neural_exit_rule':'confirmed opposite manage vote + aligned 5/15m → market CLOSE; else broker SL/TP/trail',
                'manage_signal':self._manage_signal,
                'hard_risk':hr_snap,
                'day_book':db_snap,
                'entry_eligible':bool(self.market_context.get('ready') and self.mandate and book_open.get('allowed') and self.last_closed_step>=0 and self.armed and self.execution_enabled and not self.store.unresolved() and live_ready),
                'entry_signal_validated':bool(evaluation and evaluation.get('action') in ('BUY','SELL')),
                'entry_decision_pending_new_bar':bool(self.last_decision_bar==self.last_entry_processed_bar),
                'entry_policy_reason':evaluation.get('reason') if evaluation else None,
                'entry_independent_samples':(evaluation.get('evidence') or {}).get('samples') if evaluation else None,
                'entry_can_submit_now':bool(evaluation and evaluation.get('action') in ('BUY','SELL') and
                    gates.get('allowed') and book_open.get('allowed') and
                    self.market.get('status')=='TRADEABLE' and self.market_context.get('ready') and
                    self.mandate and self.last_closed_step>=0 and self.armed and
                    self.execution_enabled and not self.store.unresolved() and
                    not self._fusion_entry_blocked() and
                    self._external_entry_check(evaluation['action'])['entry_allowed'] and
                    self.brain.core.steps>max(self.last_closed_step,self.last_entry_signal_step) and
                    self.last_decision_bar is not None and
                    self.last_entry_processed_bar!=self.last_decision_bar),
                'fusion_block_reason':self.fusion_block_reason,
                'entry_block_reason':self._compose_entry_block_reason(
                    readiness=readiness,evaluation=evaluation,decision=decision),
                'self_development':{
                    'registry':self.feature_registry.snapshot(),
                    'profit':self.profit_learning.summary(),
                    'model_stack':len(self.model_validator.stack),
                    'shadow_active':self.shadow_brain is not None,
                    'shadow_paper':self.shadow_paper.summary(),
                    'hold_shadow':hold_shadow.strategy_evidence(self.store),
                    'structure_shadow':structure_shadow.summary(self.store),
                    'situation_memory':situation_memory.snapshot(self.store),
                    'day_book':db_snap,
                    'champion_library':champion_library.snapshot(self.store),
                    'self_params':__import__('app.self_params',fromlist=['snapshot']).snapshot(self.store)},
                'bracket':bracket_learn.load_params(self.store),
                'bracket_learn_reason':self.store.get('bracket_learn_reason'),
                'bracket_outcomes':len(self.store.get('bracket_outcome_history',[]) or []),
                'unified_cycle':unified_cycle.load_state(self.store)}

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
            from .self_params import get as sp_get
            nmax=int(sp_get(self.store,'max_neurons',64))
            alt_n=max(8,self.brain.core.n+4 if self.brain.core.n<48 else max(8,self.brain.core.n-4))
            alt_n=min(alt_n,nmax)
            # Derive from live init_seed so shadow search is not identical across clients.
            base=int(getattr(self.brain,'init_seed',7) or 7)
            shadow_seed=(base^(0xA5A5+alt_n))%(2**31-1) or (base+alt_n+1)
            saved=self.store.get(self.SHADOW_STATE_KEY) or {}
            try:saved_n=int(saved.get('n') or 0)
            except (TypeError,ValueError):saved_n=0
            try:saved_d=int(saved.get('d') or -1)
            except (TypeError,ValueError):saved_d=-1
            use_n=saved_n if (saved_n>=8 and saved_d==d) else alt_n
            use_n=min(max(8,use_n),nmax)
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
        """Reload persisted shadow weights + h/c + step counter after create/restart."""
        from .core import WEIGHT_PARAM_NAMES
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
        # Critical #1: restore trained shadow weight matrices when present.
        weights=raw.get('weights')
        if isinstance(weights,dict):
            snap={}
            try:
                for name in WEIGHT_PARAM_NAMES:
                    if name not in weights:raise KeyError(name)
                    arr=np.asarray(weights[name],dtype=float)
                    if arr.shape!=np.asarray(getattr(shadow,name)).shape:
                        raise ValueError('shape')
                    if not np.isfinite(arr).all():raise ValueError('nan')
                    snap[name]=arr
                if not shadow.restore_weights(snap):
                    return False
            except (TypeError,ValueError,KeyError):
                # Corrupt weight blob — refuse partial restore (fail closed).
                return False
        shadow.h=h;shadow.c=c
        try:shadow.steps=int(raw.get('steps') or 0)
        except (TypeError,ValueError):shadow.steps=0
        try:shadow.model_version=int(raw.get('model_version') or 0)
        except (TypeError,ValueError):shadow.model_version=0
        ids_raw=raw.get('neuron_ids')
        if ids_raw is not None:
            try:
                ids=np.asarray(ids_raw,dtype=np.int64)
                if ids.shape==(shadow.n,):shadow.neuron_ids=ids
            except (TypeError,ValueError):
                pass
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
        """Persist shadow weights + h/c + steps after every completed-candle advance."""
        from .core import WEIGHT_PARAM_NAMES
        shadow=self.shadow_brain
        if shadow is None:return
        tr=getattr(shadow,'last_transition',None)
        # Critical #1: include trainable matrices so restart keeps shadow learning.
        weights={name:np.asarray(getattr(shadow,name),dtype=float).tolist()
                 for name in WEIGHT_PARAM_NAMES}
        payload={
            'h':np.asarray(shadow.h,dtype=float).tolist(),
            'c':np.asarray(shadow.c,dtype=float).tolist(),
            'steps':int(getattr(shadow,'steps',0) or 0),
            'n':int(shadow.n),'d':int(shadow.d),
            'model_version':int(getattr(shadow,'model_version',0) or 0),
            'neuron_ids':np.asarray(shadow.neuron_ids,dtype=np.int64).tolist(),
            'weights':weights,
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
            # ru_maxrss is KiB on Linux; resource module does not exist on Windows.
            if resource is None:
                rss_mb=0.
            else:
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
        # Do not propose/validate until history can pass train/OOS thin-split.
        need=min_bars_for_train_oos_split(self.store)
        if len(bars)<need:return
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
                max_tp=zone.get('max_tp'),candles=rows,
                tick=tick,spread=spread,min_distance=minimum)
            # Same decided distances as LIVE — do not re-run stop_take on raw rng.
            stop_d=_num(bp.get('stop_distance'))
            profit_d=_num(bp.get('profit_distance'))
            if stop_d is None or profit_d is None or stop_d<=0 or profit_d<=0:
                entry_proxy=float(mid)+abs(float(spread or 0))/2.
                brackets=stop_take(entry_proxy,'BUY',tick,spread,minimum,
                                   kappa=bp['kappa'],sl_scale=1.0,
                                   range_distance=float(rng) if rng is not None else 0)
                stop_d=float(brackets['stop_distance'])
                profit_d=float(brackets['profit_distance'])
            kappa=bp.get('kappa') or 2.0;sl_scale=1.0
        except Exception:
            stop_d=profit_d=None
        # Critical #4: friction from Store self_params — robot evolves these.
        from .self_params import load as load_self_params
        sp=load_self_params(self.store)
        slip_bps=float(sp['exec_slip_bps'])
        fill_delay_bars=int(sp['exec_fill_delay_bars'])
        closed=self.shadow_paper.on_bar(
            action,mid=mid,spread=spread,bar_time=bar_time,
            cash_scale=self.shadow_paper.cash_scale,
            high=high,low=low,tick=tick,min_distance=minimum,
            stop_distance=stop_d,profit_distance=profit_d,
            kappa=kappa,sl_scale=sl_scale,
            model_version=int(getattr(shadow,'model_version',0) or 0),
            slip_bps=slip_bps,fill_delay_bars=fill_delay_bars,
            shadow_transition=shadow_tr)
        if closed is not None and closed.get('exit_reason') in ('sl','tp','trail'):
            # CRITICAL: shadow paper must use the SAME loss teacher as LIVE
            # (label_from_profit). Old path called best_action_from_counterfactual
            # raw → false flip labels with missing/tiny spread, then promote
            # copied those poisoned weights into LIVE (Gold flip_loss churn).
            pnl=closed.get('paper_pnl')
            if pnl is None:pnl=closed.get('price_pnl')
            label=None
            teach_mode=None
            if pnl is not None:
                try:
                    cash=float(pnl)
                    direction=closed.get('direction')
                    entry=_num(closed.get('entry'))
                    exit_px=_num(closed.get('exit') or closed.get('exit_px'))
                    # Prefer the trade's own spread (stored at close), not bar mid.
                    try:
                        trade_spread=float(closed.get('spread')
                                           if closed.get('spread') is not None
                                           else (spread or 0))
                    except (TypeError,ValueError):
                        trade_spread=float(spread or 0)
                    if not math.isfinite(trade_spread) or trade_spread<0:
                        trade_spread=0.
                    # flip_after_loss audit: prior *paper* loss + direction change.
                    hist=list(self.shadow_paper.history or [])
                    prev=hist[-2] if len(hist)>=2 else None
                    flip_after_loss=False
                    if (cash<0 and isinstance(prev,dict)
                            and prev.get('direction') in ('BUY','SELL')
                            and prev.get('direction')!=direction):
                        prev_pnl=prev.get('paper_pnl')
                        if prev_pnl is None:prev_pnl=prev.get('price_pnl')
                        try:
                            flip_after_loss=float(prev_pnl)<0
                        except (TypeError,ValueError):
                            flip_after_loss=False
                    from .profit_learning import label_from_profit
                    decision=label_from_profit(
                        direction,cash,flip_after_loss=flip_after_loss,
                        entry=entry,exit_level=exit_px,
                        spread=trade_spread,slip_bps=slip_bps)
                    label=decision.get('label')
                    teach_mode=decision.get('mode')
                    if label is not None:label=int(label)
                except (TypeError,ValueError):
                    label=None;teach_mode=None
            if label is not None:
                self._shadow_learn(closed.get('shadow_transition'),label)
                try:
                    self.store.event('shadow_paper','Shadow paper taught from close',{
                        'direction':closed.get('direction'),
                        'paper_pnl':pnl,'label':label,'mode':teach_mode,
                        'spread':closed.get('spread'),
                        'exit_reason':closed.get('exit_reason')})
                except Exception:
                    pass
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


