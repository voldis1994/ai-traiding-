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
    assert 'QUOTE' in html
    assert 'OPEN POSITIONS' in html
    assert 'CLIENT CLOSES' in html
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


def test_quote_panel_no_fake_depth():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'Depth/size N/A on Capital CFD' in js
    assert 'ESTIMATED LADDER' not in js


def test_cache_bust_bumped():
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'admin.css?v=117' in html
    assert 'admin.js?v=122' in html
    assert 'ohlc-chart.js?v=12' in html


def test_desk_panels_have_readable_spacing():
    """Laptop Desk must not crush labels into unreadable 8px cells."""
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    assert '--desk-gap:8px' in css.replace(' ', '')
    assert 'min-height:34px' in css.replace(' ', '') or 'min-height:34px' in css
    assert '.psx-score-row' in css
    assert '.psx-ind-kv .desk-kv' in css
    # Rail wide enough for TERMINAL / RESEARCH / PRACTICE (44px clipped).
    assert 'grid-template-columns:78px' in css.replace(' ', '')
    # Intel reasons wrap — no 4-line clamp ellipsis.
    assert '-webkit-line-clamp:4' not in css
    # Indicator labels (ATR14 M1) must not ellipsis into "AUX" junk.
    assert 'text-overflow:ellipsis' not in css.split('.psx-ind-kv')[1].split('.psx-trades')[0]
    # Toolbar grows with Bid/Ask — fixed 36px clipped the quote.
    assert 'min-height:40px' in css.replace(' ', '')


def test_desk_chart_footer_and_levels_readable():
    """Chart ADX/ATR footer + live ENT must not collide with MID."""
    ohlc = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert 'yPadBot=showLabels?52' in ohlc.replace(' ', '')
    assert "if(!inTrade)hline(mid" in ohlc.replace(' ', '')
    assert 'metrics.join' in ohlc
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'ATR14 M1' in js
    assert "String(t).slice(0,120)" not in js


def test_client_closes_marks_upl_vs_capital():
    """CLIENT CLOSES must not present ≈UPL as Capital.com £."""
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'closePnlSrcBadge' in js
    assert '≈UPL' in js
    assert 'CAPITAL' in js
    assert 'gaida Capital £' in js
    assert "src==='broker'||src==='upl_estimate'" not in js
    assert "mtf.ready?'BULL':'NEUT'" not in js
    assert 'deskOhlcSignal' in js
    assert 'ohlc RR' in js or 'wait RR' in js
    assert "tape ${ch" not in js
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'id="deskClosedSrc"' in html
    assert 'id="deskClosedSrc">CAPITAL<' not in html.replace(' ','')
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    assert 'psx-pnl-src' in css
    ohlc = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert 'view.mtf||{}).atr14' in ohlc.replace(' ','') or '(view.mtf||{}).atr14' in ohlc


def test_demo_fixture_isolated():
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'buildDeskDemoFixture' in js
    assert 'deskDemo' in js
    assert 'DEMO mode — trading commands disabled' in js
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'id="deskLean"' in html
    assert 'AI SCORE LEAN' in html


def test_terminal_views_and_fullscreen():
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'data-view="overview"' in html
    assert 'data-view="trades"' in html
    assert 'data-view="risks"' in html
    assert 'data-view="clients"' in html
    assert 'data-view="settings"' in html
    assert 'id="fullscreenBtn"' in html
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'enterAdminFullscreen' in js
    assert 'requestFullscreen' in js
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    assert 'term-main>.view:not([data-view="desk"])' in css
    ohlc = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert '#31d59a' in ohlc
    assert '#ef6875' in ohlc
    assert 'IBM Plex Mono' in ohlc


def test_visual_only_chrome_removed():
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'data-desk-mode="SCALPER"' not in html
    assert 'deskWidgetBtn' not in html
    assert 'PLATINUM' not in html
    assert 'ASSET FUNDING' not in html
    assert 'CRYPTO MARKET LEAN' not in html
    assert 'GLOBAL SENTIMENT' not in html
    assert 'Filter tape' in html
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'TP2†' not in js
    assert 'ESTIMATED LADDER' not in js
    assert 'Depth/size N/A on Capital CFD' in js
    assert 'id="deskFunding"' in html
    assert 'id="deskFear"' in html
