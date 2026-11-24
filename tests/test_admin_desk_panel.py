"""NEURAL Desk control panel — real Capital feeds, no fake equity/tape."""
from pathlib import Path


def test_desk_html_shell_present():
    index = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'data-view="desk"' in index
    assert 'id="deskChart"' in index
    assert 'id="deskEngine"' in index
    assert 'id="deskTape"' in index
    assert 'id="deskFocus"' in index
    assert 'id="deskWeights"' in index
    assert 'id="deskReact"' in index
    assert 'id="deskSignal"' in index
    assert 'id="deskCrowd"' in index
    assert 'id="deskPulse"' in index


def test_desk_js_uses_live_endpoints_not_fake_curve():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'function renderDesk' in js
    assert 'renderDeskWeights' in js
    assert 'renderDeskReact' in js
    assert 'deskTrack' in js
    assert 'POLL_DESK_MS' in js
    assert 'POLL_DESK_MS=120' in js.replace(' ', '')
    assert 'renderDeskLight' in js
    assert 'renderDeskHeavy' in js
    # Heavy OHLC must not block overviewInFlight (was 5–33s Desk lag).
    assert 'renderDeskHeavy(focus,demoFx).catch' in js.replace(' ', '') or \
        'renderDeskHeavy(focus, demoFx).catch' in js
    # After ~1m stick: heavy must coalesce (not stack every 120ms poll).
    assert 'deskHeavyInFlight' in js
    assert 'DESK_HEAVY_MIN_MS' in js
    assert '/api/admin/markets-ohlc' in js
    assert 'structure-view' in js
    assert 'Nav Capital Funds £ closes' in js or 'Nav Capital £ closes' in js
    # Placeholder equity seed removed.
    assert 'seed=[0,12,8,22' not in js
    # CLIENT CLOSES = Capital Funds £ only (no UPL/pending guesses).
    chunk = js.split('function isDeskListableClose')[1].split('function sourceLabel')[0]
    assert 'upl_estimate' not in chunk
    assert 'isCapitalFundsSrc' in chunk


def test_admin_card_exposes_desk_intel_fields():
    from app.runtime_hub import RuntimeHub
    src = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    assert 'economic_news_risk' in src
    assert 'intermarket_analysis' in src
    assert 'trail_enabled' in src
    assert 'returns_bps' in src
    assert 'situation_memory' in src
    assert 'neural_period_weights' in Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert RuntimeHub is not None
