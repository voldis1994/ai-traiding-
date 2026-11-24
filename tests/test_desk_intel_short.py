"""MARKET INTEL chips must stay short — no engine prose walls."""
from pathlib import Path


def test_short_intel_helpers_exist():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'function shortIntelLine' in js
    assert 'Nav HI news logā' in js
    assert 'Peers gaida' in js
    assert 'Open ir — gaida neironu' in js
    assert 'Open/SYNC var atšķirties no phone Funds £' in js
    assert 'SYNC ≠ phone Funds £' in js
    assert 'Gaida epic · manuāls OPEN' in js
    assert 'Peers · gaida manuālu OPEN' in js
    assert 'M1 · gaida likviditāti' in js
    # Lot wait is CONTEXT (operator action), not BEARISH market gate.
    assert "lotWait?'CONTEXT':'BEARISH'" in js
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    assert '.psx-intel-tip' in css
