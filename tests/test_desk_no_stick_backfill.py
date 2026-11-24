"""Desk overview must not sync-scan 2000 journal closes (sticks after ~1m)."""
import asyncio
import inspect
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from app.runtime_hub import RuntimeHub


def test_refresh_live_truth_schedules_backfill_not_sync():
    src = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    assert '_bg_backfill_mistake_audits' in src
    assert 'backfill_scheduled' in src
    body = src.split('async def refresh_live_truth')[1].split(
        'async def _bg_backfill_mistake_audits')[0]
    # Desk path only schedules — never calls backfill inline.
    assert 'create_task' in body and '_bg_backfill_mistake_audits' in body
    assert '>= 60.0' in body
    assert 'eng._backfill_mistake_audits_from_journal(' not in body


def test_desk_system_payload_no_sync_journal_backfill():
    """Overview paints _desk_system_payload every poll — must not scan 2000 closes."""
    src = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    desk = src.split('def _desk_system_payload')[1].split(
        'def _shadow_lab_payload')[0]
    assert '_backfill_mistake_audits_from_journal' not in desk
    assert 'limit=2000' not in desk
    lab = src.split('def _shadow_lab_payload')[1].split(
        'def admin_client_card')[0]
    assert '_backfill_mistake_audits_from_journal' not in lab
    assert 'limit=2000' not in lab
    # Only the background helper may call the journal scanner sync.
    assert src.count('_backfill_mistake_audits_from_journal(') <= 2


def test_backfill_event_only_when_new_work():
    eng_src = Path('app/engine.py').read_text(encoding='utf-8')
    chunk = eng_src.split('def _backfill_mistake_audits_from_journal')[1].split(
        'def _shadow_mirror_live_open')[0]
    assert 'if n or gates_n:' in chunk
    assert 'if n or scanned or gates_n:' not in chunk


def test_bg_backfill_is_async_and_yields():
    assert asyncio.iscoroutinefunction(RuntimeHub._bg_backfill_mistake_audits)
    src = inspect.getsource(RuntimeHub._bg_backfill_mistake_audits)
    assert 'asyncio.sleep(0)' in src or 'to_thread' in src
