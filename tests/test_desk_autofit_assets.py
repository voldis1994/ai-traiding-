"""Desk Auto-Fit: assets expose CSS vars + JS sizing without scale/zoom."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / 'app/web/admin/admin.css').read_text(encoding='utf-8')
JS = (ROOT / 'app/web/admin/admin.js').read_text(encoding='utf-8')
HTML = (ROOT / 'app/web/admin/index.html').read_text(encoding='utf-8')


def test_autofit_css_vars_and_no_global_scale():
    assert '--desk-fs:' in CSS or '--desk-fs:' in CSS.replace(' ', '')
    assert '--desk-col-intel' in CSS
    assert '--desk-grid-r1' in CSS
    assert 'data-autofit' in CSS
    assert 'zoom:normal' in CSS
    assert 'transform:none' in CSS
    # Fixed window order preserved in autofit lock.
    assert '"chart chart book engine"' in CSS
    assert '"ind deals watch whale"' in CSS


def test_autofit_js_api():
    assert 'function deskAutoFit(' in JS
    assert 'function termViewsAutoFit(' in JS
    assert 'function scheduleDeskAutoFit(' in JS
    assert "dataset.autofit='1'" in JS
    assert "'--desk-fs'" in JS
    assert "'--desk-col-intel'" in JS
    assert "'--desk-grid-r1'" in JS
    assert 'bottomMin' in JS
    # Forbidden whole-app shrink APIs must not be applied by autofit.
    assert 'style.zoom' not in JS
    assert 'transform:scale' not in JS
    assert 'style.transform' not in JS


def test_bottom_island_readable_markup():
    """LEARN/CLOSES must not mid-break labels or clip side text."""
    assert "deskKv(client.id,'Cards'" in JS
    assert "deskKv(client.id,'Samples'" in JS
    assert 'wh-top' in JS and 'wh-bot' in JS
    assert 'grid-template-columns:1fr' in CSS or 'grid-template-columns:1fr;' in CSS.replace(' ','')
    assert '.psx-learn-kv{display:grid;grid-template-columns:1fr' in CSS
    assert 'overflow-wrap:anywhere' not in CSS.split('.psx-learn-kv')[1].split('.psx-ind')[0]
    assert 'flex-direction:column' in CSS.split('.psx-whale-row')[1].split('.psx-pnl-src')[0]


def test_cache_bust_mentions_new_assets():
    assert 'admin.css?v=' in HTML
    assert 'admin.js?v=' in HTML
    assert 'display_n' in JS or 'sampleHead' in JS
    assert 'No paper closes yet' in JS or 'waiting for shadow fills' in JS
    assert 'renderShadowLab' in JS
    assert 'overflow:hidden' in CSS
    compact = CSS.replace(' ', '').replace('\n', '')
    assert 'shadow-card.wide{grid-column:span3}' in compact
    assert 'Hardframelock' in compact or '#dash,.shell,.term' in compact
