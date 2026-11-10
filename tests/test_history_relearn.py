"""Offline history relearn rebuilds structure + trade-PnL from saved closes."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.engine import AdaptiveBrain, Store
from app import history_relearn as hr
from app import structure_shadow as ss


def _bars(start, n, *, step=-0.4, epic='GOLD', t0=None):
    rows = []
    px = start
    t0 = t0 or datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    for i in range(n):
        o, c = px, px + step
        hi, lo = max(o, c) + 0.05, min(o, c) - 0.05
        at = (t0 + timedelta(minutes=i)).isoformat()
        rows.append({
            'epic': epic, 'at': at, 'open': o, 'high': hi, 'low': lo,
            'close': c, 'source': 'MINUTE', 'spread': 0.5,
        })
        px = c
    return rows


def _fill_db(path: Path, *, candles, trades):
    store = Store(path)
    for c in candles:
        store.db.execute(
            'INSERT OR REPLACE INTO candles(epic,at,open,high,low,close,source,spread) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (c['epic'], c['at'], c['open'], c['high'], c['low'], c['close'],
             c['source'], c['spread']))
    for t in trades:
        store.db.execute(
            'INSERT INTO trades(deal_id,epic,size,direction,opened,closed,entry,exit,pnl,'
            'pnl_currency,pnl_source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (t['deal_id'], t.get('epic', 'GOLD'), '0.1', t['direction'],
             t['opened'], t['closed'], t['entry'], t['exit'], t['pnl'],
             t.get('pnl_currency', 'GBP'), t.get('pnl_source', 'broker')))
    store.db.commit()
    store.close()
    return path


def test_relearn_teaches_hold_on_repeated_down_buy_losses():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        store = Store(tmp / 'robot.sqlite3')
        brain = AdaptiveBrain(store, data_dir=tmp)
        # Contiguous candles long enough for features + several trades.
        candles = _bars(4000.0, 200, step=-0.25)
        for c in candles:
            store.db.execute(
                'INSERT OR REPLACE INTO candles(epic,at,open,high,low,close,source,spread) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (c['epic'], c['at'], c['open'], c['high'], c['low'], c['close'],
                 c['source'], c['spread']))
        store.db.commit()
        # Three BUY losses later in the series (need 61 bars of history first).
        for i, idx in enumerate((80, 110, 140)):
            opened = candles[idx]['at']
            closed = candles[idx + 8]['at']
            store.db.execute(
                'INSERT INTO trades(deal_id,epic,size,direction,opened,closed,entry,exit,pnl,'
                'pnl_currency,pnl_source) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (f'd{i}', 'GOLD', '0.1', 'BUY', opened, closed,
                 candles[idx]['open'], candles[idx + 8]['close'], -0.20,
                 'GBP', 'broker'))
        store.db.commit()

        # Bias BUY first so there is something to unlearn.
        z = np.zeros(brain.core.d)
        tr = {
            'z': z, 'h': np.zeros(brain.core.n), 'c': np.zeros(brain.core.n),
            'dt': 60., 'neuron_ids': brain.core.neuron_ids.copy(),
        }
        for _ in range(15):
            brain.learn_from_trade(tr, 0, lr=0.4, won=True)

        report = hr.relearn(store=store, brain=brain, lr=0.35, loss_repeats=3)
        assert report['ok'] is True
        assert report['taught'] >= 1
        lib = ss.load_library(store)
        assert lib['buckets'], 'structure library must be rebuilt from history'
        # After repeated BUY losses, later teaches should prefer HOLD modes.
        holdish = sum(v for k, v in report['modes'].items() if 'hold' in k)
        assert holdish >= 1 or any(
            r.get('structure_prefer_hold') for r in report['rows'])


def test_merge_backups_dedupes_deal_id_and_unions_candles():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / 'proj'
        b1 = root / 'NEURAL_AI_LEARNING_BACKUP_20261029_090000'
        b2 = root / 'NEURAL_AI_LEARNING_BACKUP_20261105_120000'
        b1.mkdir(parents=True)
        b2.mkdir(parents=True)
        c1 = _bars(4000.0, 100, step=-0.2)
        c2 = _bars(3990.0, 100, step=0.2,
                   t0=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc))
        # Overlapping deal d_same: backup2 has broker pnl (preferred).
        t_shared_bad = {
            'deal_id': 'd_same', 'direction': 'BUY',
            'opened': c1[70]['at'], 'closed': c1[78]['at'],
            'entry': c1[70]['open'], 'exit': c1[78]['close'], 'pnl': -0.1,
            'pnl_source': 'ticks_only',
        }
        t_shared_good = {
            'deal_id': 'd_same', 'direction': 'BUY',
            'opened': c1[70]['at'], 'closed': c1[78]['at'],
            'entry': c1[70]['open'], 'exit': c1[78]['close'], 'pnl': -0.22,
            'pnl_source': 'broker',
        }
        t_only1 = {
            'deal_id': 'd1', 'direction': 'SELL',
            'opened': c1[80]['at'], 'closed': c1[88]['at'],
            'entry': c1[80]['open'], 'exit': c1[88]['close'], 'pnl': 0.15,
        }
        t_only2 = {
            'deal_id': 'd2', 'direction': 'BUY',
            'opened': c2[70]['at'], 'closed': c2[82]['at'],
            'entry': c2[70]['open'], 'exit': c2[82]['close'], 'pnl': -0.18,
        }
        _fill_db(b1 / 'robot.sqlite3', candles=c1, trades=[t_shared_bad, t_only1])
        _fill_db(b2 / 'robot.sqlite3', candles=c2, trades=[t_shared_good, t_only2])

        found = hr.discover_sqlite_dumps(root)
        assert len(found) == 2
        dest = Store(tmp / 'merged.sqlite3')
        merge = hr.merge_sqlite_dumps(dest, found)
        assert merge['unique_trades'] == 3
        trades = hr.load_trades(dest)
        by_id = {t['deal_id']: t for t in trades}
        assert by_id['d_same']['pnl'] == -0.22
        assert by_id['d_same']['pnl_source'] == 'broker'
        assert set(by_id) == {'d_same', 'd1', 'd2'}
        candles = hr.load_candles(dest, 'GOLD')
        assert len(candles) == 200  # 100+100 union

        # End-to-end: merged corpus → relearn with a seed brain.
        work = tmp / 'merged_work'
        seed_store = Store(tmp / 'seed.sqlite3')
        seed_brain = AdaptiveBrain(seed_store, data_dir=str(tmp / 'seed_data'))
        seed_brain.core.model_version = 'LIVE'
        seed_brain.save()
        brain_path = Path(seed_brain.path)
        seed_store.close()
        dest.close()

        paths = hr.prepare_merged_workdir(
            brain=brain_path, work=work, sources=found, seed_db=None)
        brain2 = AdaptiveBrain(paths['store'], data_dir=str(paths['work']))
        report = hr.relearn(
            store=paths['store'], brain=brain2, lr=0.3, loss_repeats=2)
        assert report['ok'] is True
        assert report['trades'] == 3
        assert paths['merge']['unique_trades'] == 3
