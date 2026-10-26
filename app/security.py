"""Password hashing and at-rest encryption for broker secrets (stdlib + cryptography)."""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - installed via requirements
    Fernet=None
    InvalidToken=Exception


def hash_password(password: str, *, iterations: int = 100_000) -> str:
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,iterations)
    return f'pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo,iter_s,salt_hex,digest_hex=encoded.split('$',3)
        if algo!='pbkdf2_sha256':return False
        iterations=int(iter_s)
        salt=bytes.fromhex(salt_hex)
        expected=bytes.fromhex(digest_hex)
    except (ValueError,TypeError):
        return False
    digest=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,iterations)
    return hmac.compare_digest(digest,expected)


def _fernet(key_path: Path) -> 'Fernet':
    if Fernet is None:
        raise RuntimeError('cryptography package is required for broker secret storage')
    key_path.parent.mkdir(parents=True,exist_ok=True)
    if key_path.is_file():
        key=key_path.read_bytes().strip()
    else:
        env=os.getenv('NTA_SECRET_KEY','').strip()
        key=(env.encode('utf-8') if env else Fernet.generate_key())
        key_path.write_bytes(key if isinstance(key,bytes) else key)
        try:key_path.chmod(0o600)
        except OSError:pass
    if isinstance(key,str):key=key.encode('utf-8')
    # Accept raw url-safe Fernet keys; if user pasted random string, derive one.
    try:
        return Fernet(key)
    except (ValueError,TypeError):
        derived=hashlib.sha256(key).digest()
        import base64
        return Fernet(base64.urlsafe_b64encode(derived))


class SecretBox:
    def __init__(self,key_path: Path):
        self._fernet=_fernet(key_path)

    def encrypt(self,plain: str) -> str:
        return self._fernet.encrypt(plain.encode('utf-8')).decode('ascii')

    def decrypt(self,token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode('ascii')).decode('utf-8')
        except InvalidToken as exc:
            raise ValueError('Cannot decrypt broker secret') from exc
