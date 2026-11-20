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
        # Same module LIVE uses before OPEN — Admin drawing == robot wait gate.
        assert view.get('entry_zone_source') == 'ohlc_entry_zones'
        assert 'overlays' in view
        ov = view['overlays'] or {}
        # KAMA/EMA series from the same stack LIVE reads (even before full MTF warm).
        assert isinstance(ov.get('kama'), list) and len(ov['kama']) >= 20
        assert isinstance(ov.get('ema5'), list)
        assert 'mtf' in view
        # Swing marks remapped into the displayed candle window.
        n = len(view['candles'])
        for mark in (view['structure'].get('swing_highs') or []):
            if mark.get('i') is not None:
                assert 0 <= int(mark['i']) < n


def test_remap_swing_marks_onto_display_window():
    from app.runtime_hub import _remap_swing_marks
    # Pivots indexed inside normalise path (last 80 of 720).
    marks = [
        {'price': 10.0, 'i': 5},   # abs 645 → display 15
        {'price': 12.0, 'i': 50},  # abs 690 → display 60
        {'price': 11.0, 'i': 79},  # abs 719 → display 89
    ]
    out = _remap_swing_marks(marks, full_n=720, display_n=90, path_n=80)
    assert out == [
        {'price': 10.0, 'i': 15},
        {'price': 12.0, 'i': 60},
        {'price': 11.0, 'i': 89},
    ]
    # Path entirely before display window → dropped.
    early = _remap_swing_marks(
        [{'price': 9.0, 'i': 0}], full_n=720, display_n=40, path_n=80)
    assert early == []


def test_admin_ohlc_page_route_exists():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.get('/admin/ohlc')
        assert r.status_code == 200
        body = r.content
        assert b'ohlc-full' in body or b'OHLC' in body
        # #146 readable/live popup contract.
        assert b'liveBadge' in body
        assert b'chartWrap' in body
        assert b'btnFullscreen' in body
        assert b'ohlc-page.js?v=' in body
        assert b'ohlc-chart.js?v=' in body


def test_admin_markets_ohlc_empty_without_running_robots():
    """Shared boards key off live engines — idle clients do not spawn panels."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        platform = PlatformStore(root / 'platform.sqlite3')
        platform.create_client('idle1', 'pass1234', 'Idle', 'lv')
        hub = RuntimeHub(platform)
        out = hub.admin_markets_ohlc(n=90)
        assert out['count'] == 0
        assert out['markets'] == []


def test_admin_per_client_ohlc_button_contract():
    """Desk wires markets-ohlc; Klienti cards still open per-client OHLC popup."""
    admin_js = Path('app/web/admin/admin.js').read_text(encoding='utf-8')
    index = Path('app/web/admin/index.html').read_text(encoding='utf-8')
    assert 'id="sharedOhlc"' not in index
    assert 'refreshSharedOhlc' not in admin_js
    assert 'data-view="desk"' in index
    assert 'id="deskChart"' in index
    assert 'admin/markets-ohlc' in admin_js
    assert 'renderDesk' in admin_js
    assert 'data-ohlc-client' not in admin_js
    assert 'data-act="ohlc"' in admin_js
    assert 'openClientOhlcWindow' in admin_js
    assert 'OHLC' in admin_js


def test_structure_view_never_invents_gold_without_epic():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        platform = PlatformStore(root / 'platform.sqlite3')
        client = platform.create_client('noepic', 'pass1234', 'No Epic', 'lv')
        cid = int(client['id'])
        data_dir = platform.account_data_dir(cid)
        data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(data_dir / 'robot.sqlite3')
        # account_data_dir is global DATA/ — wipe any prior fixture noise.
        store.set('legacy_brain_epic', None)
        store.db.execute('DELETE FROM trades')
        store.db.execute('DELETE FROM candles')
        store.db.commit()
        hub = RuntimeHub(platform)
        view = hub.admin_client_structure_view(cid, n=90)
        assert view.get('ok') is False
        assert view.get('reason') == 'no_epic'
        assert view.get('epic') in (None, '')
        assert view.get('epic') != 'GOLD'


def test_structure_view_uses_us500_not_gold_default():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        platform = PlatformStore(root / 'platform.sqlite3')
        client = platform.create_client('idx1', 'pass1234', 'Index One', 'lv')
        cid = int(client['id'])
        data_dir = platform.account_data_dir(cid)
        data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(data_dir / 'robot.sqlite3')
        store.db.execute('DELETE FROM trades')
        store.db.execute('DELETE FROM candles')
        store.db.commit()
        store.set('legacy_brain_epic', 'US500')
        for row in _candles(70):
            store.candle('US500', row, source='HISTORICAL')
        hub = RuntimeHub(platform)
        view = hub.admin_client_structure_view(cid, n=90)
        assert view['ok'] is True
        assert view['epic'] == 'US500'
        assert view['epic'] != 'GOLD'
        assert view.get('epic_source') == 'legacy_brain'
