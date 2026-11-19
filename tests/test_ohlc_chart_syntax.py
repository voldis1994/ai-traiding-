"""Admin OHLC canvas script must parse — a SyntaxError blanks the popup."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'app' / 'web' / 'admin' / 'ohlc-chart.js'


def test_ohlc_chart_js_parses():
    assert CHART.is_file()
    # Redeclaring const atr inside drawOhlcStructure used to kill the whole
    # script — popup stayed black while subtitle still showed LIVE data.
    src = CHART.read_text(encoding='utf-8')
    assert src.count('const atr=') <= 1
    node = shutil.which('node')
    assert node, 'node required to syntax-check Admin OHLC chart'
    r = subprocess.run([node, '--check', str(CHART)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout
