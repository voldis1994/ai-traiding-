"""Regressions for startup and code-evolution subprocess in frozen binaries."""
import subprocess
import sys
from pathlib import Path

import pytest

import launcher
from app.autonomy import AutoEvolution, DEFAULT
from app.engine import Store


def test_offline_launcher_selftest():
    assert launcher.main(['--self-test']) == 0


def test_policy_worker_accepts_safe_generated_code(tmp_path):
    policy = tmp_path / 'generated.py'
    policy.write_text(DEFAULT, encoding='utf-8')
    assert launcher.main(['--policy-check', str(policy)]) == 0


def test_policy_worker_rejects_invalid_code(tmp_path):
    policy = tmp_path / 'generated.py'
    policy.write_text('import os\ndef decide(*args): return os.system("echo bad")', encoding='utf-8')
    with pytest.raises(ValueError):
        launcher.main(['--policy-check', str(policy)])


def test_frozen_code_evolution_uses_own_worker_command(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.db')
    ev = AutoEvolution(store, tmp_path / 'active_policy.py')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert ev._test_candidate(DEFAULT) is True
    assert captured and captured[0][:2] == [sys.executable, '--policy-check']


def test_windows_scripts_have_crlf_line_endings():
    root = Path(__file__).resolve().parent.parent
    for name in ('run_windows.bat', 'build_windows_exe.bat'):
        data = (root / name).read_bytes()
        assert b'\r\n' in data
        assert b'\n' not in data.replace(b'\r\n', b'')
