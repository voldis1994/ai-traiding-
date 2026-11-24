"""LIVE minus → SHADOW full cycle: paper settle + flush + opposite retest."""
import tempfile
from pathlib import Path

from app.engine import AdaptiveBrain, SingleTradeEngine, Store
from app import shadow_mistake_audit as sma
from app.shadow_paper import ShadowPaperJournal


def _engine(tmp):
    store = Store(Path(tmp) / 'db.sqlite3')
    brain = AdaptiveBrain(store, data_dir=tmp)
    e = SingleTradeEngine(store, brain)
    e.mandate = {'epic': 'GOLD', 'size': '0.1'}
    e.market = {
        'mid': 3600., 'tick': 0.1, 'spread': 0.2, 'min_distance': 0.5,
        'bid': 3599.9, 'ask': 3600.1, 'status': 'TRADEABLE', 'epic': 'GOLD',
    }
    e._ensure_shadow_brain()
    return store, e


def test_force_close_live_mirror_writes_cash_paper_row():
    with tempfile.TemporaryDirectory() as d:
        j = ShadowPaperJournal(Store(Path(d) / 't.sqlite3'))
        j.position = {
            'direction': 'BUY', 'entry': 3600.0, 'stop': 3595.0,
            'profit': 3610.0, 'entry_context': {'live_deal_id': 'd1'},
        }
        closed = j.force_close_live_mirror(
            exit_px=3595.0, paper_pnl=-1.17, live_deal_id='d1',
            reason='live_mirror_sl')
        assert closed is not None
        assert closed['unit'] == 'account_currency'
        assert float(closed['paper_pnl']) == -1.17
        assert closed['exit_reason'] == 'live_mirror_sl'
        assert j.position is None
        assert j._row_is_cash(closed)


def test_live_loss_full_cycle_closes_paper_and_opens_retest():
    with tempfile.TemporaryDirectory() as d:
        store, e = _engine(d)
        deal = 'live-loss-1'
        store.set('entry_brain:' + deal, {
            'direction': 'BUY', 'entry': 3600., 'tick_size': 0.1,
            'entry_spread': 0.2, 'stop_distance': 5.0, 'profit_distance': 10.0,
            'bracket_kappa': 2.0, 'bracket_sl_scale': 1.0,
            'bar_time': '2026-11-24T12:00:00+00:00',
            'ohlc_entry': {
                'trigger': 'break', 'reason': 'ohlc_break_trigger',
                'action': 'BUY',
                'zone': {'lo': 3590., 'hi': 3595.},
                'scenario': {'scenario': 'BREAKOUT_UP'},
            },
        })
        e._shadow_mirror_live_open(deal)
        assert e.shadow_paper.position is not None

        # Stub brackets so opposite retest can OPEN without full OHLC tape.
        e._would_be_brackets = lambda action: {
            'stop_distance': 4.0, 'profit_distance': 8.0,
            'ohlc_kappa': 1.5, 'tick': 0.1,
        }

        out = e._shadow_full_cycle_on_live_close(
            deal, pnl=-1.17, direction='BUY', entry=3600.,
            exit_level=3595., spread=0.2)
        assert out['paper_closed'] is True
        # Paper book has cash close (Desk filter).
        cash = e.shadow_paper.paper_cash_pnls()
        assert cash and cash[-1] == -1.17
        # After loss: opposite retest open in paper.
        assert out['retest'] is True
        assert e.shadow_paper.position is not None
        assert e.shadow_paper.position.get('direction') == 'SELL'
        ctx = e.shadow_paper.position.get('entry_context') or {}
        assert ctx.get('mirrored_from') == 'loss_retest'


def test_live_loss_audit_still_commits_fix_before_cycle():
    with tempfile.TemporaryDirectory() as d:
        store, e = _engine(d)
        deal = 'live-loss-audit'
        store.set('entry_brain:' + deal, {
            'direction': 'SELL', 'entry': 3600.,
            'ohlc_entry': {
                'action': 'SELL', 'trigger': 'zone',
                'zone': {'lo': 3605., 'hi': 3610.},
                'scenario': {'scenario': 'TREND_UP'},
            },
        })
        # Build candles so diagnose can run.
        rows = []
        px = 3600.0
        for i in range(40):
            rows.append({
                'open': px, 'high': px + 0.5, 'low': px - 0.5, 'close': px,
                'at': f'2026-11-24T10:{i:02d}:00+00:00',
            })
        e._decision_candles = lambda epic=None, n=80: rows
        audited = e._run_mistake_audit_close(
            deal, pnl=-0.54, direction='SELL', entry=3600., source='live')
        assert audited and audited.get('audited')
        # Full cycle after audit (no mirror open → still flush + retest).
        e._would_be_brackets = lambda action: {
            'stop_distance': 3.0, 'profit_distance': 6.0, 'tick': 0.1,
        }
        out = e._shadow_full_cycle_on_live_close(
            deal, pnl=-0.54, direction='SELL', entry=3600.,
            exit_level=3605., spread=0.2)
        assert out['paper_closed'] is False  # nothing mirrored
        assert out['retest'] is True
        assert e.shadow_paper.position.get('direction') == 'BUY'
        lab = sma.lab_payload(store)
        assert lab.get('ok') is not False
