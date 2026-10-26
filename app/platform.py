"""Multi-tenant platform registry: admin, clients, broker accounts, sessions."""
from __future__ import annotations
import json
import secrets
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Optional

from .security import SecretBox, hash_password, verify_password
from .storage import DATA

PLATFORM_DB=DATA/'platform.sqlite3'
SECRET_KEY_PATH=DATA/'secret.key'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class PlatformStore:
    def __init__(self,path: Path|None=None,secret_path: Path|None=None):
        self.path=Path(path or PLATFORM_DB)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.box=SecretBox(Path(secret_path or SECRET_KEY_PATH))
        self.db=sqlite3.connect(str(self.path),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self._migrate()

    def _migrate(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS admins(
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clients(
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'lv',
            active INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_accounts(
            id INTEGER PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            broker_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            api_key_enc TEXT NOT NULL,
            identifier_enc TEXT NOT NULL,
            password_enc TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL,
            UNIQUE(client_id)
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            subject_id INTEGER NOT NULL,
            created TEXT NOT NULL,
            expires TEXT
        );
        ''')
        self.db.commit()

    # --- admin bootstrap ---
    def ensure_admin(self,username: str,password: str) -> dict:
        row=self.db.execute('SELECT id,username FROM admins WHERE username=? COLLATE NOCASE',
                            (username,)).fetchone()
        if row:
            return dict(row)
        cur=self.db.execute(
            'INSERT INTO admins(username,password_hash,created) VALUES(?,?,?)',
            (username,hash_password(password),_now()))
        self.db.commit()
        return {'id':cur.lastrowid,'username':username}

    def admin_count(self) -> int:
        return int(self.db.execute('SELECT COUNT(*) FROM admins').fetchone()[0])

    def verify_admin(self,username: str,password: str) -> Optional[dict]:
        row=self.db.execute('SELECT id,username,password_hash FROM admins WHERE username=? COLLATE NOCASE',
                            (username,)).fetchone()
        if not row or not verify_password(password,row['password_hash']):
            return None
        return {'id':row['id'],'username':row['username'],'role':'admin'}

    def verify_client(self,username: str,password: str) -> Optional[dict]:
        row=self.db.execute(
            'SELECT id,username,display_name,language,active,password_hash FROM clients '
            'WHERE username=? COLLATE NOCASE',(username,)).fetchone()
        if not row or not row['active'] or not verify_password(password,row['password_hash']):
            return None
        return {'id':row['id'],'username':row['username'],'display_name':row['display_name'],
                'language':row['language'],'role':'client'}

    # --- sessions ---
    def create_session(self,role: str,subject_id: int) -> str:
        token=secrets.token_urlsafe(32)
        self.db.execute('INSERT INTO sessions(token,role,subject_id,created) VALUES(?,?,?,?)',
                        (token,role,subject_id,_now()))
        self.db.commit()
        return token

    def get_session(self,token: str|None) -> Optional[dict]:
        if not token:return None
        row=self.db.execute('SELECT token,role,subject_id,created FROM sessions WHERE token=?',
                            (token,)).fetchone()
        return dict(row) if row else None

    def delete_session(self,token: str|None):
        if not token:return
        self.db.execute('DELETE FROM sessions WHERE token=?',(token,))
        self.db.commit()

    # --- clients ---
    def create_client(self,username: str,password: str,display_name: str,
                      language: str='lv') -> dict:
        username=username.strip()
        display_name=(display_name or username).strip()
        language=(language or 'lv').lower()
        if language not in ('lv','en','ru'):language='lv'
        if not username or not password:
            raise ValueError('Username and password are required')
        if len(password)<4:
            raise ValueError('Password must be at least 4 characters')
        try:
            cur=self.db.execute(
                'INSERT INTO clients(username,password_hash,display_name,language,created) '
                'VALUES(?,?,?,?,?)',
                (username,hash_password(password),display_name,language,_now()))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError('Client username already exists') from exc
        return self.get_client(cur.lastrowid)

    def list_clients(self) -> list[dict]:
        rows=self.db.execute(
            'SELECT c.id,c.username,c.display_name,c.language,c.active,c.created,'
            'b.id AS broker_account_id,b.broker_type,b.display_name AS broker_name,b.active AS broker_active '
            'FROM clients c LEFT JOIN broker_accounts b ON b.client_id=c.id '
            'ORDER BY c.id DESC').fetchall()
        out=[]
        for r in rows:
            out.append({
                'id':r['id'],'username':r['username'],'display_name':r['display_name'],
                'language':r['language'],'active':bool(r['active']),'created':r['created'],
                'broker':None if r['broker_account_id'] is None else {
                    'id':r['broker_account_id'],'broker_type':r['broker_type'],
                    'display_name':r['broker_name'],'active':bool(r['broker_active']),
                },
            })
        return out

    def get_client(self,client_id: int) -> Optional[dict]:
        row=self.db.execute(
            'SELECT id,username,display_name,language,active,created FROM clients WHERE id=?',
            (client_id,)).fetchone()
        if not row:return None
        data=dict(row);data['active']=bool(data['active'])
        broker=self.get_broker_for_client(client_id)
        data['broker']={'id':broker['id'],'broker_type':broker['broker_type'],
                        'display_name':broker['display_name'],'active':broker['active']} if broker else None
        return data

    def set_client_language(self,client_id: int,language: str) -> dict:
        language=(language or 'lv').lower()
        if language not in ('lv','en','ru'):raise ValueError('Language must be lv, en or ru')
        self.db.execute('UPDATE clients SET language=? WHERE id=?',(language,client_id))
        self.db.commit()
        return self.get_client(client_id)

    def set_client_active(self,client_id: int,active: bool) -> dict:
        self.db.execute('UPDATE clients SET active=? WHERE id=?',(1 if active else 0,client_id))
        self.db.commit()
        return self.get_client(client_id)

    def reset_client_password(self,client_id: int,password: str) -> dict:
        if len(password)<4:raise ValueError('Password must be at least 4 characters')
        self.db.execute('UPDATE clients SET password_hash=? WHERE id=?',
                        (hash_password(password),client_id))
        self.db.commit()
        return self.get_client(client_id)

    # --- broker accounts ---
    def attach_broker(self,client_id: int,broker_type: str,display_name: str,
                      api_key: str,identifier: str,password: str) -> dict:
        client=self.get_client(client_id)
        if not client:raise ValueError('Client not found')
        broker_type=(broker_type or 'capital_com').strip().lower()
        display_name=(display_name or f"{client['display_name']} · {broker_type}").strip()
        payload=(
            self.box.encrypt(api_key),
            self.box.encrypt(identifier),
            self.box.encrypt(password),
        )
        existing=self.db.execute('SELECT id FROM broker_accounts WHERE client_id=?',
                                 (client_id,)).fetchone()
        if existing:
            self.db.execute(
                'UPDATE broker_accounts SET broker_type=?,display_name=?,api_key_enc=?,'
                'identifier_enc=?,password_enc=?,active=1 WHERE client_id=?',
                (broker_type,display_name,*payload,client_id))
            account_id=existing['id']
        else:
            cur=self.db.execute(
                'INSERT INTO broker_accounts(client_id,broker_type,display_name,api_key_enc,'
                'identifier_enc,password_enc,created) VALUES(?,?,?,?,?,?,?)',
                (client_id,broker_type,display_name,*payload,_now()))
            account_id=cur.lastrowid
        self.db.commit()
        return self.get_broker_account(account_id)

    def get_broker_account(self,account_id: int) -> Optional[dict]:
        row=self.db.execute(
            'SELECT id,client_id,broker_type,display_name,api_key_enc,identifier_enc,'
            'password_enc,active,created FROM broker_accounts WHERE id=?',
            (account_id,)).fetchone()
        if not row:return None
        data=dict(row);data['active']=bool(data['active'])
        return data

    def get_broker_for_client(self,client_id: int) -> Optional[dict]:
        row=self.db.execute(
            'SELECT id,client_id,broker_type,display_name,api_key_enc,identifier_enc,'
            'password_enc,active,created FROM broker_accounts WHERE client_id=?',
            (client_id,)).fetchone()
        if not row:return None
        data=dict(row);data['active']=bool(data['active'])
        return data

    def broker_credentials(self,account: dict) -> dict[str,str]:
        return {
            'api_key':self.box.decrypt(account['api_key_enc']),
            'identifier':self.box.decrypt(account['identifier_enc']),
            'password':self.box.decrypt(account['password_enc']),
            'broker_type':account['broker_type'],
            'display_name':account['display_name'],
        }

    def list_active_broker_accounts(self) -> list[dict]:
        rows=self.db.execute(
            'SELECT b.id,b.client_id,b.broker_type,b.display_name,b.api_key_enc,'
            'b.identifier_enc,b.password_enc,b.active,b.created,c.username,c.active AS client_active '
            'FROM broker_accounts b JOIN clients c ON c.id=b.client_id '
            'WHERE b.active=1 AND c.active=1').fetchall()
        return [dict(r)|{'active':True,'client_active':bool(r['client_active'])} for r in rows]

    def account_data_dir(self,client_id: int) -> Path:
        path=DATA/'accounts'/f'client_{int(client_id)}'
        path.mkdir(parents=True,exist_ok=True)
        return path
