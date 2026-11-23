"""Local Windows Desk must open without typing admin login."""
from fastapi.testclient import TestClient

from app.main import app


def test_local_boot_sets_admin_cookie_on_loopback():
    client = TestClient(app)
    r = client.post('/api/admin/local-boot')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get('ok') is True
    assert body.get('local_boot') is True
    assert 'nta_admin' in r.cookies
    me = client.get('/api/admin/me')
    assert me.status_code == 200
    assert me.json().get('role') == 'admin'


def test_get_admin_page_mints_cookie_on_loopback():
    """Opening /admin on 127.0.0.1 must skip the login gate without JS."""
    client = TestClient(app)
    r = client.get('/admin')
    assert r.status_code == 200
    assert 'nta_admin' in r.cookies
    # Cookie must be usable on plain HTTP (Secure=False).
    set_cookie = r.headers.get('set-cookie') or ''
    assert 'nta_admin=' in set_cookie.lower()
    assert 'secure' not in set_cookie.lower()
    me = client.get('/api/admin/me')
    assert me.status_code == 200


def test_me_auto_mints_on_loopback_without_cookie():
    client = TestClient(app)
    # Fresh client — no cookie jar priming via /admin.
    me = client.get('/api/admin/me')
    assert me.status_code == 200, me.text
    assert me.json().get('role') == 'admin'
    assert 'nta_admin' in me.cookies or 'nta_admin' in (me.headers.get('set-cookie') or '')


def test_admin_js_auto_boots_localhost():
    js = open('app/web/admin/admin.js', encoding='utf-8').read()
    assert '/api/admin/local-boot' in js
    assert "password:'admin123'" in js.replace(' ', '')
    html = open('app/web/admin/index.html', encoding='utf-8').read()
    assert 'admin.js?v=150' in html
