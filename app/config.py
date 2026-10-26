"""Runtime configuration for local Windows and public hosting."""
from __future__ import annotations
import os


def env_flag(name: str, default: bool=False) -> bool:
    raw=(os.getenv(name) or '').strip().lower()
    if not raw:return default
    return raw in ('1','true','yes','on')


def cookie_secure() -> bool:
    """HTTPS-only cookies in production (Render/Railway/Fly or explicit flag)."""
    raw=(os.getenv('NTA_SECURE_COOKIES') or '').strip().lower()
    if raw in ('0','false','no','off'):return False
    if raw in ('1','true','yes','on'):return True
    if os.getenv('RENDER') or os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('FLY_APP_NAME'):
        return True
    return env_flag('NTA_PRODUCTION')


def bind_host() -> str:
    # Public hosts must listen on all interfaces.
    if os.getenv('PORT') or env_flag('NTA_PRODUCTION') or os.getenv('RENDER') or os.getenv('RAILWAY_ENVIRONMENT'):
        default='0.0.0.0'
    else:
        default='127.0.0.1'
    return (os.getenv('NTA_BIND_HOST') or os.getenv('HOST') or default).strip() or default


def bind_port(default: int=8765) -> int:
    raw=os.getenv('PORT') or os.getenv('NTA_PORT') or str(default)
    try:return int(raw)
    except ValueError:return default


def public_base_url() -> str|None:
    url=(os.getenv('NTA_PUBLIC_URL') or '').strip().rstrip('/')
    return url or None
