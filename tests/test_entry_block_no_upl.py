"""Entry-block reasons must never paste live UPL into Admin / OI-FUND."""
import tempfile
from pathlib import Path

from app.engine import AdaptiveBrain, SingleTradeEngine, Store


def test_existing_open_block_reason_omits_upl_and_deal_id():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 'db.sqlite3')
        brain = AdaptiveBrain(store, data_dir=d)
        e = SingleTradeEngine(store, brain)
        e.positions = [{
            'dealId': '00601567-0001-54c4-0000-000092bc812b',
            'direction': 'BUY',
            'upl': -0.15,
        }]
        e.last_signal = {
            'action': 'BUY',
            'scores': {'BUY': 0.5, 'SELL': 0.3, 'HOLD': 0.2},
        }
        reason = e._existing_open_block_reason()
        assert reason == 'Existing open position'
        assert 'UPL' not in reason
        assert '00601567' not in reason


def test_admin_js_strips_legacy_upl_from_entry_block_copy():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'function humanEntryBlock' in js
    assert r'\bUPL\b' in js
    assert 'function accountCashCcy' in js
    assert 'Open <strong' in js
    deal = js.split('function renderDeal')[1].split('function renderCard')[0]
    assert 'UPL <strong' not in deal
    eng = Path('app/engine.py').read_text(encoding='utf-8')
    assert "bits.append('UPL" not in eng
    assert 'UPL {0:.2f}' not in eng
    hub = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    assert "cash_ccy or acct or 'GBP'" in hub
