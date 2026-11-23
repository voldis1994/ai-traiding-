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


def test_admin_js_auto_boots_localhost():
    js = open('app/web/admin/admin.js', encoding='utf-8').read()
    assert '/api/admin/local-boot' in js
    assert "host==='127.0.0.1'" in js
    html = open('app/web/admin/index.html', encoding='utf-8').read()
    assert 'admin.js?v=149' in html
