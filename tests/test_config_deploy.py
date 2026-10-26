"""Production bind/cookie helpers."""
import os
from app import config


def test_bind_defaults_localhost_without_paas(monkeypatch):
    monkeypatch.delenv('PORT',raising=False)
    monkeypatch.delenv('NTA_PRODUCTION',raising=False)
    monkeypatch.delenv('NTA_BIND_HOST',raising=False)
    monkeypatch.delenv('RENDER',raising=False)
    monkeypatch.delenv('RAILWAY_ENVIRONMENT',raising=False)
    assert config.bind_host()=='127.0.0.1'
    assert config.cookie_secure() is False


def test_bind_public_when_port_set(monkeypatch):
    monkeypatch.setenv('PORT','10000')
    monkeypatch.delenv('NTA_BIND_HOST',raising=False)
    assert config.bind_host()=='0.0.0.0'
    assert config.bind_port()==10000


def test_secure_cookies_on_render(monkeypatch):
    monkeypatch.setenv('RENDER','true')
    monkeypatch.delenv('NTA_SECURE_COOKIES',raising=False)
    assert config.cookie_secure() is True
    monkeypatch.setenv('NTA_SECURE_COOKIES','0')
    assert config.cookie_secure() is False
