"""Diagnosable Windows/source entry point. Also acts as frozen policy-test worker.

Keep third-party imports out of module scope: a frozen executable must process
--policy-check (and multiprocessing.freeze_support) without booting the server.
"""
import json
import logging
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser

def _runtime_bind():
    from app.config import bind_host, bind_port
    host = bind_host()
    port = bind_port(8765)
    # Browser opener always uses localhost even when server binds 0.0.0.0
    browse = '127.0.0.1' if host in ('0.0.0.0', '::') else host
    return host, port, f'http://{browse}:{port}/'

HOST, PORT, URL = '127.0.0.1', 8765, 'http://127.0.0.1:8765/'


def log_directory():
    candidates = []
    if sys.platform == 'win32':
        candidates.append(Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'NeuralTradingAI' / 'logs')
    candidates.append(Path.home() / '.neural_trading_ai' / 'logs')
    candidates.append(Path(tempfile.gettempdir()) / 'NeuralTradingAI' / 'logs')
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except (OSError, PermissionError):
            pass
    raise OSError('Could not create a writable diagnostics directory')


def configure_logging():
    log_file = log_directory() / 'startup.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return log_file


def notify_failure(message, log_file):
    description = f'{message}\n\nDetails: {log_file}'
    print(description, file=sys.stderr, flush=True)
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, description, 'Neural Trading AI - Startup Error', 0x10)
        except Exception:
            pass


def policy_check(path):
    """Run only the restricted generated policy in a separate process.

    sys.executable is our own .exe in frozen mode, NOT python.exe, so tests
    must use this explicit worker command rather than `exe checker.py`.
    """
    from app.autonomy import AutoEvolution
    source = Path(path).read_text(encoding='utf-8')
    AutoEvolution._validate(source)
    scope = {'__builtins__': {'max': max, 'sorted': sorted}}
    exec(compile(source, '<generated-policy>', 'exec'), scope)
    f = scope['decide']
    assert f({'BUY': .1, 'SELL': .1, 'HOLD': .8}) == 'HOLD'
    assert f({'BUY': .99, 'SELL': .005, 'HOLD': .005}) == 'BUY'
    assert f({'BUY': .005, 'SELL': .99, 'HOLD': .005}, 'BUY', 10) == 'CLOSE'
    assert f({'BUY': .005, 'SELL': .99, 'HOLD': .005}, 'BUY', 0) == 'HOLD'
    for scores in ({'BUY': .7, 'SELL': .1, 'HOLD': .2},
                   {'BUY': .1, 'SELL': .7, 'HOLD': .2}):
        assert f(scores) in ('BUY', 'SELL', 'HOLD')
    return 0


def self_test():
    """Offline packaged-runtime test; no broker credentials, no live orders."""
    import fastapi
    import torch
    import httpx
    import websockets
    from app.main import app
    from app.core import NeuralCore
    static = Path(__file__).resolve().parent / 'app' / 'web'
    assert (static / 'index.html').is_file(), f'Missing bundled UI: {static}'
    assert (static / 'style.css').is_file()
    assert (static / 'ui.js').is_file()
    assert (static / 'admin' / 'index.html').is_file()
    assert (static / 'client' / 'index.html').is_file()
    assert (static / 'portal.html').is_file()
    assert app.title == 'Neural Trading AI'
    result = NeuralCore(n=8).step([0.0, 1.0, 0.0])
    assert abs(sum(result['scores'].values()) - 1) < 1e-8
    print('SELF-TEST PASS: imports, UI assets and neural inference are available', flush=True)
    return 0


def browser_when_ready():
    for _ in range(180):
        try:
            with urllib.request.urlopen(f'http://{HOST}:{PORT}/api/health', timeout=0.8) as response:
                info = json.load(response)
                if info.get('app') == 'Neural Trading AI' and info.get('ready'):
                    webbrowser.open(URL)
                    logging.info('Browser opened: %s', URL)
                    return
        except (OSError, ValueError, TimeoutError):
            time.sleep(0.5)
    logging.error('Web service did not become healthy in 90 seconds; open %s manually', URL)


def check_port_available():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PORT))
        except OSError as exc:
            raise RuntimeError(
                f'Port {PORT} is already occupied. Close the previous Neural Trading AI instance '
                f'or another service using http://{HOST}:{PORT} before starting again.'
            ) from exc


def start_server():
    global HOST, PORT, URL
    HOST, PORT, URL = _runtime_bind()
    check_port_available()
    import uvicorn
    from app.main import app
    logging.info('Neural Trading AI starting on %s (bind %s:%s)', URL, HOST, PORT)
    print(f'Neural Trading AI starting. Browser address: {URL}', flush=True)
    print('Portals: /  /app  /admin  /console', flush=True)
    if HOST in ('0.0.0.0', '::') or os.getenv('PORT'):
        # Cloud / LAN: do not force-open a local browser.
        pass
    else:
        threading.Thread(target=browser_when_ready, daemon=True, name='browser-opener').start()
    uvicorn.run(app, host=HOST, port=PORT, access_log=False, log_level='info')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--policy-check']:
        if len(argv) != 2:
            raise ValueError('Expected --policy-check <path>')
        return policy_check(argv[1])
    if argv == ['--self-test']:
        return self_test()
    if argv:
        raise ValueError('Unknown argument: ' + ' '.join(argv))
    return start_server()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    log_path = configure_logging()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.info('Application closed by user')
        sys.exit(0)
    except Exception as exc:
        logging.exception('Application failed to start or run')
        notify_failure(f'{type(exc).__name__}: {exc}', log_path)
        sys.exit(1)
