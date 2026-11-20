"""React PerpSignal Desk is served from FastAPI at /desk when built."""
from pathlib import Path


def test_desk_build_present():
    root = Path('app/web/desk')
    assert (root / 'index.html').is_file()
    assets = root / 'assets'
    assert assets.is_dir()
    assert any(assets.glob('index-*.js'))
    assert any(assets.glob('index-*.css'))


def test_main_mounts_desk_route():
    src = Path('app/main.py').read_text(encoding='utf-8')
    assert "/desk" in src
    assert "desk_page" in src
    assert "desk-assets" in src


def test_full_system_launcher_exists():
    assert Path('PALAIST_PILNO_SISTEMU.bat').is_file()
    assert Path('KĀ_PALAIST_LV.txt').is_file()
    bat = Path('perpsignal-desk/start.bat').read_text(encoding='utf-8', errors='ignore')
    assert '8765' in bat
    assert 'PALAIST_PILNO_SISTEMU' in bat or 'run_windows.bat' in bat
