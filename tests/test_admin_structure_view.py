"""Admin per-client OHLC structure + prospective entry zones."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import Store
from app.platform import PlatformStore
from app.runtime_hub import RuntimeHub


def _candles(n=60):
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    rows = []
    price = 2000.0
    for i in range(n):
        o = price
        h = o + 1.2 + (i % 5) * 0.15
        l = o - 1.0 - (i % 4) * 0.1
        c = o + (0.4 if i % 3 else -0.35)
        rows.append({
            'at': (t0 + timedelta(minutes=i)).isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c,
        })
        price = c
    return rows


def test_admin_structure_view_builds_zones():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        platform = PlatformStore(root / 'platform.sqlite3')
        client = platform.create_client('chart1', 'pass1234', 'Chart One', 'lv')
        cid = int(client['id'])
        data_dir = platform.account_data_dir(cid)
        data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(data_dir / 'robot.sqlite3')
        store.set('legacy_brain_epic', 'GOLD')
        for row in _candles(70):
            store.candle('GOLD', row, source='HISTORICAL')
        hub = RuntimeHub(platform)
        view = hub.admin_client_structure_view(cid, n=90)
        assert view['ok'] is True
        assert view['epic'] == 'GOLD'
        assert view.get('client_name') == 'Chart One' or view.get('username') == 'chart1'
        assert len(view['candles']) >= 40
        zones = view['zones']
        # At least one structural or entry zone should be present when swings exist.
        assert zones.get('buy_entry') or zones.get('sell_entry') or zones.get('support')
        assert 'structure' in view


def test_admin_ohlc_page_route_exists():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.get('/admin/ohlc')
        assert r.status_code == 200
        assert b'ohlc-full' in r.content or b'OHLC' in r.content
