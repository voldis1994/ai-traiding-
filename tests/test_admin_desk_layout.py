"""VS-System 1:1 Desk layout — DOM ids and safe UI reset."""
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
    assert 'VS-System' in html
    assert 'vs-logo-mark.png' in html


def test_desk_css_tokens_and_grid_areas():
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    compact = css.replace(' ', '')
    assert '--desk-bg:#060a09' in compact
    assert 'grid-template-areas' in css
    assert 'chart chart book engine' in css
    assert 'IBM Plex Mono' in css
    assert 'height:34px' in compact
    assert '.psx-ind-kv' in css
    assert 'border-top:1px solid var(--desk-grid)' in css.split('.psx-ind-kv')[1].split('.psx-trades')[0]
    assert 'term-mark-img' in css


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
    assert 'M1 CONSENSUS' in js
    assert 'Free sources fused' in js
    assert 'Affects OPEN' in js
    assert 'learns from' in js
    assert 'Paid slots idle' not in js
    assert 'ESTIMATED LADDER' not in js


def test_cache_bust_bumped():
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'admin.css?v=' in html
    assert 'admin.js?v=' in html
    hub = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    assert 'DESK_TRUTH_REV =' in hub
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'truth #' in js
    assert '/api/admin/refresh-truth' in js
    assert 'CAPITAL 24h' in js
    assert 'rebuilding fingerprints' in js
    assert 'ohlc-chart.js?v=13' in html


def test_status_board_visible_at_once():
    """Operator must see OPEN/CLOSE/flow/cards/shadow without nested scroll hunt."""
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'id="deskStatusBoard"' in html
    assert 'SYSTEM LEARN' in html
    assert 'id="deskLearn"' in html
    assert 'hidden' not in html.split('id="deskLearn"')[0][-80:]
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    assert '.psx-status-board' in css
    assert 'grid-template-columns:repeat(7' in css.replace(' ', '')
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'renderDeskStatusBoard' in js
    assert 'CAN OPEN' in js
    assert 'NEURAL CLOSE' in js
    assert 'STRATEGY CARDS' in js
    assert 'SHADOW £' in js
    assert 'LIVE 24h £' in js
    assert "shadowHero==='live24h'" in js
    assert 'Capital 24h' in js
    assert "st==='UNMAPPED_INSTRUMENT'" in js
    assert "softBoot" in js or "softWait" in js


def test_desk_panels_have_readable_spacing():
    """Laptop Desk must not crush labels into unreadable 8px cells."""
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    assert (
        '--desk-gap:4px' in css.replace(' ', '')
        or '--desk-gap:6px' in css.replace(' ', '')
        or '--desk-gap:8px' in css.replace(' ', '')
    )
    assert 'min-height:22px' in css.replace(' ', '') or 'min-height:34px' in css.replace(' ', '')
    assert '.psx-score-row' in css
    assert '.psx-ind-kv .desk-kv' in css
    # Rail wide enough for TERMINAL / RESEARCH / PRACTICE (44px clipped).
    assert 'grid-template-columns:78px' in css.replace(' ', '')
    # Intel reasons wrap — no 4-line clamp ellipsis.
    assert '-webkit-line-clamp:4' not in css
    # Short indicator labels — values dominate; no ellipsis clipping names.
    assert 'text-overflow:ellipsis' not in css.split('.psx-ind-kv')[1].split('.psx-trades')[0]
    assert 'psx-ind-head i' in css.replace(' ', '') or '.psx-ind-head i' in css
    # Toolbar grows with Bid/Ask — fixed 36px clipped the quote.
    assert 'min-height:40px' in css.replace(' ', '')


def test_desk_chart_footer_and_levels_readable():
    """Chart ADX/ATR footer + live ENT must not collide with MID."""
    ohlc = Path('app/web/admin/ohlc-chart.js').read_text(encoding='utf-8')
    assert 'yPadBot=showLabels?52' in ohlc.replace(' ', '')
    assert "if(!inTrade)hline(mid" in ohlc.replace(' ', '')
    assert 'band(zones.buy_entry' in ohlc.replace(' ', '')
    assert 'metrics.join' in ohlc
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert "deskKv(client.id,'ATR'" in js.replace(' ', '') or "deskKv(client.id,'ATR'" in js
    assert '<i>ATR</i>' in js
    assert "String(t).slice(0,120)" not in js


def test_signal_watch_and_open_positions_fit_text():
    """Signal rows stack epic/reason; OPEN POSITIONS shows Capital SL/TP."""
    css = Path('app/web/admin/admin.css').read_text(encoding='utf-8')
    compact = css.replace(' ', '')
    # Signal rows: single-line epic+btn (reason hidden for frame fit) or stacked.
    assert (
        'grid-template-rows:auto' in compact
        or 'grid-template-rows:autoauto' in compact
        or '.psx-sig-row' in css
    )
    # Layout tracks evolve with autofit — accept current or prior readable floors.
    assert (
        'minmax(280px,1.5fr)' in compact
        or 'minmax(220px,1.25fr)' in compact
        or '--desk-grid-r2:minmax' in compact
    )
    assert (
        'minmax(200px,1.1fr)' in compact
        or 'minmax(200px,.85fr)' in compact
        or 'minmax(160px,1.1fr)' in compact
    )
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'SL/TP' in js
    assert 'stopLevel' in js
    engine = Path('app/engine.py').read_text(encoding='utf-8')
    assert 'protect_position(naked,force=True)' in engine.replace(' ', '')


def test_client_closes_marks_upl_vs_capital():
    """CLIENT CLOSES must not present ≈UPL as Capital.com £."""
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'closePnlSrcBadge' in js
    assert '≈UPL' in js
    assert 'CAPITAL' in js
    assert 'No Capital Funds £ yet' in js or 'nav Capital £' in js
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


def test_open_pnl_uses_account_currency_not_gold_quote_usd():
    """GOLD position.currency=USD must not label open cash as USD on a GBP account."""
    js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    assert 'p.currency&&p.currency!==ccy?p.currency:ccy' not in js.replace(' ', '')
    assert 'Always account cash currency' in js or 'Account currency only' in js
    chunk = js.split('function collectOpenTrades')[1].split('function instrumentIcon')[0]
    assert 'accountCashCcy(c)' in chunk
    assert "currency:ccy||'GBP'" in chunk.replace(' ', '') or 'currency:ccy||"GBP"' in chunk
    assert 'const ccy=accountCashCcy(c)' in chunk.replace('\n','')
    assert 'perfClients' in js
    assert 'humanEntryBlock' in js
    assert 'parseCloseTs' in js
    assert '>Open<' in js
    assert 'function accountCashCcy' in js
    assert 'function isDeskListableClose' in js
    card = js.split('function renderCard')[1].split('function renderRiskAlerts')[0]
    assert 'accountCashCcy(c)' in card
    assert 'UPL {0:.2f}' not in Path('app/engine.py').read_text(encoding='utf-8')
    assert "bits.append('UPL" not in Path('app/engine.py').read_text(encoding='utf-8')
    # Deal card must never paint the literal UPL label again.
    deal = js.split('function renderDeal')[1].split('function renderCard')[0]
    assert 'UPL <strong' not in deal
    assert 'Open <strong' in deal
    html = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'id="perfClients"' in html


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
    # Frame-fit: no nested scrollbars on desk modules.
    assert 'Hard frame lock' in css or '#dash,.shell,.term' in css.replace(' ', '')
    assert 'overflow:hidden!important' in css.replace(' ', '')
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
    assert 'M1 CONSENSUS' in js
    assert 'Free sources fused' in js
    assert 'Affects OPEN' in js
    assert 'learns from' in js
    assert 'Paid slots idle' not in js
    assert 'id="deskFunding"' in html
    assert 'id="deskFear"' in html
