"""Capital.com lot/distance grid — no invented sizes like 0.1009."""
from decimal import Decimal

from app.math_rules import (
    dealing_size_grid, snap_deal_size, on_deal_size_grid, snap_distance,
    broker_wire_number,
)
from app import day_book as db


def test_snap_deal_size_rejects_junk_fractions():
    # GOLD-style: min 0.1, step 0.1 → 0.1009 must become 0.1
    out = snap_deal_size('0.1009', min_size='0.1', step='0.1', max_size='50')
    assert out == Decimal('0.1')
    assert on_deal_size_grid(out, min_size='0.1', step='0.1', max_size='50')
    assert not on_deal_size_grid('0.1009', min_size='0.1', step='0.1')


def test_snap_deal_size_multiples_only():
    assert snap_deal_size(0.37, min_size=0.1, step=0.1) == Decimal('0.3')
    assert snap_deal_size(0.05, min_size=0.1, step=0.1) == Decimal('0.1')
    assert snap_deal_size(99.99, min_size=0.1, step=0.1, max_size=50) == Decimal('50')


def test_dealing_size_grid_from_capital_rules():
    market = {
        'dealingRules': {
            'minDealSize': {'unit': 'AMOUNT', 'value': 0.1},
            'maxDealSize': {'unit': 'AMOUNT', 'value': 100},
            'minSizeIncrement': {'unit': 'AMOUNT', 'value': 0.1},
        }
    }
    g = dealing_size_grid(market, mandate_size='0.25')
    assert g['ok'] is True
    assert g['source'] == 'capital_dealing_rules'
    assert g['min'] == Decimal('0.1')
    assert g['step'] == Decimal('0.1')
    # Mandate 0.25 is off 0.1 grid → snap in propose
    sized = snap_deal_size('0.25', min_size=g['min'], step=g['step'], max_size=g['max'])
    assert sized == Decimal('0.2')


def test_mandate_fallback_when_rules_missing():
    g = dealing_size_grid({}, mandate_size='0.25')
    assert g['ok'] is True
    assert g['source'] == 'mandate_fallback'
    assert g['step'] == Decimal('0.25')
    assert snap_deal_size('0.7', min_size=g['min'], step=g['step']) == Decimal('0.5')


def test_broker_wire_number_no_float_dust():
    assert broker_wire_number(Decimal('0.10')) == 0.1
    assert broker_wire_number('1.000') == 1
    assert broker_wire_number(Decimal('0.2')) == 0.2


def test_snap_distance_to_tick():
    assert snap_distance(0.153, 0.01, minimum=0.15) == Decimal('0.16')
    assert snap_distance(0.149, 0.01, minimum=0.15) == Decimal('0.15')


def test_day_book_propose_never_emits_off_grid():
    market = {
        'dealingRules': {
            'minDealSize': {'unit': 'AMOUNT', 'value': '0.1'},
            'maxDealSize': {'unit': 'AMOUNT', 'value': '20'},
            'minSizeIncrement': {'unit': 'AMOUNT', 'value': '0.1'},
        }
    }
    out = db.propose_size(
        mandate_size='0.1', equity=10_000.0, daily={'realized_pnl': 0},
        params=db.load_params(None), stop_ticks=20, tick_value=1.0,
        positions=[], score_edge=0.2, market=market)
    assert out['allowed'] is True
    size = out['size']
    assert on_deal_size_grid(size, min_size='0.1', step='0.1', max_size='20')
    # Must be exact tenth — never 0.1009 / binary dust
    assert Decimal(str(size)) * 10 == (Decimal(str(size)) * 10).to_integral_value()


def test_propose_size_preserves_manual_mandate_015_not_floor_to_010():
    """Manual adopt 0.15 must not become robot OPEN 0.10 via floor snap."""
    market = {
        'dealingRules': {
            'minDealSize': {'unit': 'AMOUNT', 'value': '0.1'},
            'maxDealSize': {'unit': 'AMOUNT', 'value': '50'},
            'minSizeIncrement': {'unit': 'AMOUNT', 'value': '0.1'},
        }
    }
    # Floor-only snap would shrink 0.15 → 0.1 — that was the LIVE complaint.
    assert snap_deal_size('0.15', min_size='0.1', step='0.1') == Decimal('0.1')
    # Missing tick_value → mandate fallback path (common until instrument pip known).
    params = dict(db.load_params(None))
    params['size_full_balance'] = 0  # fractional: return mandate seed
    out = db.propose_size(
        mandate_size='0.15', equity=5_000.0, daily={'realized_pnl': 0},
        params=params, stop_ticks=None, tick_value=None,
        positions=[], score_edge=0.1, market=market)
    assert out['allowed'] is True
    assert Decimal(str(out['size'])) == Decimal('0.15')
    assert out['reason'] == 'mandate_on_capital_grid'
