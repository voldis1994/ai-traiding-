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
    # Forbidden whole-app shrink APIs must not be applied by autofit.
    assert 'style.zoom' not in JS
    assert 'transform:scale' not in JS
    assert 'style.transform' not in JS


def test_cache_bust_mentions_new_assets():
    assert 'admin.css?v=124' in HTML
    assert 'admin.js?v=130' in HTML
