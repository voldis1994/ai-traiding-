"""PerpSignal 1:1 Desk layout — DOM ids and safe UI reset."""
from pathlib import Path


def test_desk_four_column_grid_present():
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'psx-grid' in html
    assert 'psx-chart-col' in html
    assert 'psx-book-col' in html
    assert 'psx-engine-col' in html
    assert 'id="deskClosed"' in html
    assert 'id="deskFunding"' in html
    assert 'id="deskUiResetBtn"' in html
    assert 'ESTIMATED' in html
    assert 'OPEN POSITIONS' in html
    assert 'LARGE TRADES' in html
    assert 'AI SCORE LEAN' in html


def test_desk_css_tokens_and_grid_areas():
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    compact = css.replace(' ', '')
    assert '--desk-bg:#060a14' in compact
    assert 'grid-template-areas' in css
    assert 'chart chart book engine' in css
    assert 'IBM Plex Mono' in css
    assert 'height:31px' in compact


def test_ui_reset_never_calls_learning_wipe():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'deskUiResetBtn' in js
    # The UI reset handler must not POST reset-learning
    idx = js.index("deskUiResetBtn=$('deskUiResetBtn')")
    chunk = js[idx:idx + 900]
    assert "api('/api/admin/reset-learning'" not in chunk
    assert 'UI-only reset' in chunk


def test_rsi_macd_from_ohlc_or_insufficient():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert '_deskRsi' in js
    assert '_deskMacdHist' in js
    assert 'INSUFFICIENT DATA' in js


def test_order_book_marked_estimated():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'ESTIMATED LADDER' in js


def test_cache_bust_bumped():
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'admin.css?v=101' in html
    assert 'admin.js?v=108' in html


def test_demo_fixture_isolated():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'buildDeskDemoFixture' in js
    assert 'deskDemo' in js
    assert 'DEMO mode — trading commands disabled' in js
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'id="deskLean"' in html
    assert 'AI SCORE LEAN' in html
    assert 'CRYPTO MARKET LEAN' in html
