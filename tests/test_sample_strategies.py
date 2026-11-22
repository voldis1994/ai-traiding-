"""Strategies from samples — library buckets → live cards."""
import tempfile
from pathlib import Path

from app.engine import Store
from app import sample_strategies as sm
from app import strategy_gen as sg
from app import structure_shadow as ss


def _bar(at, o, h, l, c):
    return {'at': at, 'open': o, 'high': h, 'low': l, 'close': c, 'mid': c}


def test_ingest_trades_builds_library_and_cards():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        # Synthetic downtrend path — SELL should frame as paying side later.
        candles = []
        px = 2000.0
        for i in range(120):
            at = f'2026-11-05T10:{i:02d}:00+00:00' if i < 60 else (
                f'2026-11-05T11:{i-60:02d}:00+00:00')
            nxt = px - 0.4
            candles.append(_bar(at, px, px + 0.2, nxt - 0.1, nxt))
            px = nxt
        trades = [
            {
                'deal_id': 'd1', 'epic': 'GOLD', 'direction': 'SELL',
                'opened': '2026-11-05T10:30:00+00:00',
                'closed': '2026-11-05T10:45:00+00:00',
                'entry': 1988.0, 'exit': 1980.0, 'pnl': 1.2,
            },
            {
                'deal_id': 'd2', 'epic': 'GOLD', 'direction': 'BUY',
                'opened': '2026-11-05T11:00:00+00:00',
                'closed': '2026-11-05T11:20:00+00:00',
                'entry': 1975.0, 'exit': 1968.0, 'pnl': -0.9,
            },
        ]
        out = sm.ingest_trades(store, trades, candles, epic='GOLD', clear=True)
        assert out['ok'] is True
        assert out['ingested'] == 2
        assert sm.library_sample_count(store) >= 2
        sg.seed_audit_cards(store)
        h = sg.harvest(store, epic='GOLD')
        assert h['ok']
        assert h.get('activated')


def test_pathless_trade_still_banks_combo():
    """Even without OHLC path, ±£ must teach strategy_gen combos."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        trades = [{
            'deal_id': 'lonely', 'epic': 'GOLD', 'direction': 'BUY',
            'opened': '2020-01-01T00:00:00+00:00',
            'closed': '2020-01-01T00:05:00+00:00',
            'entry': 100.0, 'exit': 101.5, 'pnl': 0.7,
        }]
        out = sm.ingest_trades(store, trades, [], epic='GOLD')
        assert out['ok'] is True
        assert out['ingested'] == 1
        bag = sg.load(store)
        assert bag.get('combos')


def test_merged_backup_candles_cover_trade_span():
    root = Path('/tmp/visi-dati')
    if not root.is_dir():
        return
    bag = sm.collect_gold_samples_from_backups(root)
    if not bag.get('ok'):
        return
    ats = sorted(str(c.get('at') or '') for c in bag['candles'] if c.get('at'))
    if not ats:
        return
    inside = sum(
        1 for t in bag['trades']
        if ats[0] <= str(t.get('opened') or '') <= ats[-1])
    # Merged tapes must cover nearly all sample trades (was ~818/2324).
    assert inside >= int(0.9 * bag['n_trades'])


def test_build_from_visi_dati_backups_if_present():
    root = Path('/tmp/visi-dati')
    if not root.is_dir():
        return
    bag = sm.collect_gold_samples_from_backups(root)
    if not bag.get('ok'):
        return  # environment without usable dumps
    assert bag['n_trades'] >= 10
    assert bag['n_candles'] >= 40
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 't.sqlite3')
        out = sm.build_strategies_from_samples(
            store, backup_root=root, use_store_trades=False, force=True,
            clear_library=True)
        assert out.get('ok') is True
        assert out['library_samples'] >= 10
        assert out.get('active_cards')
        # Soft nudge must work on a known grave/pay frame if present.
        scores = {'BUY': 0.6, 'SELL': 0.3, 'HOLD': 0.1}
        # Pick any active structure
        card = sg.load(store)['cards'][out['active_cards'][0]]
        nudged, meta = sg.soft_nudge_scores(
            scores, structure=card['structure'], epic='GOLD', store=store)
        assert meta['nudged'] is True
