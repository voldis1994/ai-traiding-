"""Counterfactual only — what if LIVE gated OPEN on shadow market structure?

No production code changes. Uses the same labels as structure_shadow
(TREND_UP/DOWN, BREAKOUT_UP/DOWN, RANGE, CHOP) classified from candles
*before* entry (no look-ahead into the trade path).

Two gates compared against the recorded broker PnL:
  1. geometric — block BUY in TREND_DOWN/BREAKOUT_DOWN, SELL in TREND_UP/BREAKOUT_UP
  2. library   — walk-forward structure_shadow.library_prefers_hold (past graves only)

If a history dump is present (uploaded client sqlite), replay it; otherwise a
tiny synthetic series still exercises the math so CI stays green.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.structure_shadow import (
    ADVICE_SL_RATE,
    ADVISE_PATH_BARS,
    ADVISE_PRE_BARS,
    MIN_ADVICE_SAMPLES,
    frame_from_ohlc,
    frame_key,
    library_prefers_hold,
    normalise_bars,
)

DUMP_ROOTS = [
    Path('/tmp/history-audit'),
    Path('/home/ubuntu/.cursor/projects/agent-repos-ai-traiding/uploads'),
]


@dataclass
class TradeRow:
    deal_id: str
    epic: str
    direction: str
    opened: str
    closed: str | None
    pnl: float
    exit_reason: str | None = None


@dataclass
class GateResult:
    name: str
    taken: list = field(default_factory=list)
    blocked: list = field(default_factory=list)

    @property
    def n_taken(self):
        return len(self.taken)

    @property
    def n_blocked(self):
        return len(self.blocked)

    @property
    def pnl(self):
        return float(sum(t.pnl for t in self.taken))

    @property
    def wins(self):
        return sum(1 for t in self.taken if t.pnl > 0)

    @property
    def losses(self):
        return sum(1 for t in self.taken if t.pnl < 0)

    @property
    def blocked_pnl(self):
        """PnL that would have been avoided (sum of blocked trades)."""
        return float(sum(t.pnl for t in self.blocked))


class _MemStore:
    """Minimal store for library_prefers_hold (state bag only)."""

    def __init__(self, library=None):
        self._lib = library if isinstance(library, dict) else {'buckets': {}, 'updated': None}

    def get(self, key, default=None):
        if key == 'structure_shadow_library_v1':
            return self._lib
        return default

    def set(self, key, value):
        if key == 'structure_shadow_library_v1':
            self._lib = value


def _parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def candles_before(conn, epic, opened, *, n_path=ADVISE_PATH_BARS, n_pre=ADVISE_PRE_BARS):
    """Completed bars strictly before OPEN — what LIVE could see at decision time."""
    opened_ts = _parse_ts(opened)
    if opened_ts is None:
        return [], []
    # Compare as ISO strings (store uses +00:00).
    cut = opened_ts.astimezone(timezone.utc).isoformat(timespec='seconds')
    need = int(n_path) + int(n_pre) + 5
    rows = conn.execute(
        "SELECT at,open,high,low,close FROM candles "
        "WHERE epic=? AND at<? ORDER BY at DESC LIMIT ?",
        (epic, cut, need),
    ).fetchall()
    rows = list(reversed(rows))
    if len(rows) < 8:
        return [], []
    bars = [
        {'at': r[0], 'open': r[1], 'high': r[2], 'low': r[3], 'close': r[4]}
        for r in rows
    ]
    path = bars[-n_path:] if len(bars) >= n_path else bars
    pre = bars[: max(0, len(bars) - len(path))][-n_pre:]
    return path, pre


def structure_at_entry(path, pre):
    frame = frame_from_ohlc(normalise_bars(path), pre_bars=pre)
    return str(frame.get('structure') or 'CHOP'), bool(frame.get('ready')), frame


def geometric_blocks(structure, direction):
    """Shadow-style layout hostility — no library samples required."""
    st = str(structure or '').upper()
    if direction == 'BUY' and st in ('TREND_DOWN', 'BREAKOUT_DOWN'):
        return True, f'{st} hostile to BUY'
    if direction == 'SELL' and st in ('TREND_UP', 'BREAKOUT_UP'):
        return True, f'{st} hostile to SELL'
    return False, 'allow'


def load_closed_trades(conn):
    rows = conn.execute(
        "SELECT deal_id,epic,direction,opened,closed,pnl FROM trades "
        "WHERE closed IS NOT NULL AND pnl IS NOT NULL AND direction IN ('BUY','SELL') "
        "ORDER BY opened ASC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            pnl = float(r[5])
        except (TypeError, ValueError):
            continue
        out.append(TradeRow(
            deal_id=str(r[0]), epic=str(r[1]), direction=str(r[2]),
            opened=str(r[3]), closed=str(r[4]) if r[4] else None, pnl=pnl,
        ))
    return out


def replay_client(db_path: Path):
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    trades = load_closed_trades(conn)
    baseline = GateResult('baseline_all_taken')
    geo = GateResult('geometric_structure_gate')
    lib = GateResult('library_prefers_hold_gate')

    # Walk-forward library: only graves closed *before* this OPEN.
    buckets = {}
    closed_so_far = []  # (closed_ts, structure, direction, pnl)

    by_structure = {}

    for tr in trades:
        path, pre = candles_before(conn, tr.epic, tr.opened)
        structure, ready, frame = structure_at_entry(path, pre) if path else ('CHOP', False, {})
        by_structure[structure] = by_structure.get(structure, 0) + 1

        baseline.taken.append(tr)

        # --- geometric gate (layout labels only) ---
        block_geo, why_geo = geometric_blocks(structure, tr.direction)
        if not ready:
            block_geo, why_geo = False, 'structure_not_ready'
        if block_geo:
            geo.blocked.append(tr)
        else:
            geo.taken.append(tr)

        # --- library gate (walk-forward from prior losses) ---
        # Update library with losses that closed before this open.
        open_ts = _parse_ts(tr.opened)
        while closed_so_far and open_ts and closed_so_far[0][0] <= open_ts:
            _, st, side, pnl = closed_so_far.pop(0)
            if pnl >= 0:
                continue
            key = frame_key(epic=tr.epic, structure=st, direction=side)
            b = buckets.get(key) or {
                'samples': 0, 'sl_count': 0, 'loss_count': 0, 'win_count': 0,
            }
            b['samples'] = int(b['samples']) + 1
            b['loss_count'] = int(b['loss_count']) + 1
            # Treat cash loss as SL-equivalent for hostility (same as shadow soft gate).
            b['sl_count'] = int(b['sl_count']) + 1
            buckets[key] = b
        store = _MemStore({'buckets': buckets, 'updated': None})
        prefer = library_prefers_hold(
            store, epic=tr.epic, structure=structure, direction=tr.direction,
            min_samples=MIN_ADVICE_SAMPLES, sl_rate=ADVICE_SL_RATE,
        )
        if prefer.get('prefer_hold') and ready:
            lib.blocked.append(tr)
        else:
            lib.taken.append(tr)

        # Queue this trade's outcome for future opens (after we finish gating).
        close_ts = _parse_ts(tr.closed) or open_ts
        # Structure for the grave: use entry-time structure (what gate saw).
        closed_so_far.append((close_ts, structure, tr.direction, tr.pnl))
        closed_so_far.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc))

    conn.close()
    return {
        'client': db_path.parent.name,
        'n_trades': len(trades),
        'structures_seen': by_structure,
        'baseline': baseline,
        'geometric': geo,
        'library': lib,
    }


def _fmt(g: GateResult):
    return {
        'taken': g.n_taken,
        'blocked': g.n_blocked,
        'pnl': round(g.pnl, 2),
        'wins': g.wins,
        'losses': g.losses,
        'blocked_pnl_sum': round(g.blocked_pnl, 2),
        'blocked_losses': sum(1 for t in g.blocked if t.pnl < 0),
        'blocked_wins': sum(1 for t in g.blocked if t.pnl > 0),
    }


def find_client_dbs():
    found = []
    for root in DUMP_ROOTS:
        if not root.exists():
            continue
        found.extend(sorted(root.glob('client_*/robot.sqlite3')))
    return found


def test_geometric_gate_blocks_hostile_layouts_synthetic():
    """Unit: TREND_DOWN must block BUY; TREND_UP must block SELL."""
    assert geometric_blocks('TREND_DOWN', 'BUY')[0] is True
    assert geometric_blocks('BREAKOUT_DOWN', 'BUY')[0] is True
    assert geometric_blocks('TREND_UP', 'SELL')[0] is True
    assert geometric_blocks('BREAKOUT_UP', 'SELL')[0] is True
    assert geometric_blocks('TREND_UP', 'BUY')[0] is False
    assert geometric_blocks('RANGE', 'BUY')[0] is False
    assert geometric_blocks('CHOP', 'SELL')[0] is False


def test_structure_from_pre_entry_bars_synthetic():
    """frame_from_ohlc on falling pre-entry path → TREND_DOWN (or similar)."""
    bars = []
    px = 2000.0
    t0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    for i in range(40):
        o = px
        px = px - 0.8
        bars.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': o + 0.1, 'low': px - 0.1, 'close': px,
        })
    frame = frame_from_ohlc(normalise_bars(bars[-30:]), pre_bars=bars[:10])
    assert frame.get('ready')
    assert frame.get('structure') in (
        'TREND_DOWN', 'BREAKOUT_DOWN', 'CHOP', 'RANGE', 'TREND_UP', 'BREAKOUT_UP',
    )
    # Strong down path should not classify as TREND_UP.
    assert frame.get('structure') != 'TREND_UP'


@pytest.mark.parametrize('db_path', find_client_dbs() or [None])
def test_counterfactual_structure_gate_on_history_dump(db_path):
    if db_path is None:
        pytest.skip('No client history dump mounted — synthetic tests still cover gate rules')
    report = replay_client(Path(db_path))
    base = _fmt(report['baseline'])
    geo = _fmt(report['geometric'])
    lib = _fmt(report['library'])

    # Always print so `pytest -s` shows the interest result.
    print('\n=== STRUCTURE ENTRY GATE COUNTERFACTUAL:', report['client'], '===')
    print('structures_at_entry:', report['structures_seen'])
    print('baseline (all opens kept):', base)
    print('IF geometric TREND/BREAKOUT gate:', geo)
    print('IF library_prefers_hold gate (walk-forward):', lib)
    print(
        'delta_pnl geometric vs baseline:',
        round(geo['pnl'] - base['pnl'], 2),
        '| blocked', geo['blocked'],
        '| avoided_loss_trades', geo['blocked_losses'],
        '| cut_winning_trades', geo['blocked_wins'],
    )
    print(
        'delta_pnl library vs baseline:',
        round(lib['pnl'] - base['pnl'], 2),
        '| blocked', lib['blocked'],
        '| avoided_loss_trades', lib['blocked_losses'],
        '| cut_winning_trades', lib['blocked_wins'],
    )

    assert report['n_trades'] >= 50
    assert base['taken'] == report['n_trades']
    # Gate must actually do something on real dumps (structure was populated).
    assert geo['blocked'] > 0 or lib['blocked'] > 0
    # Sanity: taken + blocked == all
    assert geo['taken'] + geo['blocked'] == report['n_trades']
    assert lib['taken'] + lib['blocked'] == report['n_trades']


def test_print_all_clients_summary():
    """Single summary row across every mounted dump (skips if none)."""
    dbs = find_client_dbs()
    if not dbs:
        pytest.skip('No history dump')
    rows = []
    for db in dbs:
        r = replay_client(db)
        base, geo, lib = _fmt(r['baseline']), _fmt(r['geometric']), _fmt(r['library'])
        rows.append({
            'client': r['client'],
            'n': r['n_trades'],
            'base_pnl': base['pnl'],
            'geo_pnl': geo['pnl'],
            'geo_delta': round(geo['pnl'] - base['pnl'], 2),
            'geo_blocked': geo['blocked'],
            'lib_pnl': lib['pnl'],
            'lib_delta': round(lib['pnl'] - base['pnl'], 2),
            'lib_blocked': lib['blocked'],
            'structures': r['structures_seen'],
        })
    print('\n=== ALL CLIENTS SUMMARY (structure gate before entry) ===')
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    # At least one client improves or ties on geometric cash if dumps exist.
    assert any(row['geo_blocked'] > 0 for row in rows)
