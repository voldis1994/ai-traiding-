"""Offline history relearn rebuilds structure + trade-PnL from saved closes."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.engine import AdaptiveBrain, Store
from app import history_relearn as hr
from app import structure_shadow as ss


def _bars(start, n, *, step=-0.4, epic='GOLD'):
    rows = []
    px = start
    t0 = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
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
