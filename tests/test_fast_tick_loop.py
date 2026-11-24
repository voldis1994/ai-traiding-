"""Worker quote loop must keep mid/UPL near Capital tick rate without starving light path."""
import inspect
from pathlib import Path

from app import runtime_hub
from app import stream as stream_mod
from app import machine_spec
from app import main as main_mod


def test_worker_runs_independent_light_and_heavy_loops():
    """P0-03: light ticks must not wait for heavy poll to finish."""
    src = inspect.getsource(runtime_hub.RuntimeHub._worker)
    assert '_heavy_loop' in src
    assert '_light_loop' in src
    assert 'create_task' in src
    light = inspect.getsource(runtime_hub.RuntimeHub._light_loop)
    assert 'quote_upl_tick' in light
    assert '_apply_stream_quote' in light
    assert 'asyncio.sleep' in light


def test_legacy_worker_still_has_light_window():
    """Item 13: main.worker runs parallel heavy/light like RuntimeHub."""
    src = inspect.getsource(main_mod.worker)
    assert '_legacy_heavy_loop' in src and '_legacy_light_loop' in src
    light = inspect.getsource(main_mod._legacy_light_loop)
    assert 'quote_upl_tick' in light
    assert 'await engine.poll()' not in light


def test_overview_truth_refresh_is_time_bounded():
    src = inspect.getsource(main_mod.admin_overview)
    assert 'wait_for' in src
    assert 'refresh_live_truth' in src
    assert '0.2' in src


def test_funds_mirror_not_awaited_on_desk_poll():
    """Overview wait_for must not cancel Funds HTTP — schedule in background."""
    src = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    assert '_bg_mirror_capital_funds' in src
    assert 'mirror_scheduled' in src
    assert 'create_task' in src
    assert 'DESK_TRUTH_REV =' in src
    assert int(__import__('re').search(r'DESK_TRUTH_REV\s*=\s*(\d+)', src).group(1)) >= 266


def test_upl_tick_throttle_respects_api_budget():
    """P0-01: UPL REST path must not hammer Capital at ~14 req/s."""
    from app.engine import SingleTradeEngine
    src = inspect.getsource(SingleTradeEngine.quote_upl_tick)
    assert '0.25' in src
    assert '0.07' not in src
    assert 'lock.locked()' in src or 'wait_for' in src


def test_entry_zone_retry_is_sub_quarter_second():
    from app.engine import SingleTradeEngine
    src = inspect.getsource(SingleTradeEngine._retry_entry_when_ohlc_ready)
    assert '<0.12' in src
    assert '<0.25' not in src


def test_machine_spec_documents_fast_cadence():
    assert machine_spec.POLL_SECONDS <= 1
    assert machine_spec.FAST_ENTRY_TICK_SECONDS <= 0.05


def test_stream_has_regular_app_ping_and_subscribe_gate():
    """P1-04: ping on timer + connected after subscribe/quote."""
    src = Path('app/stream.py').read_text(encoding='utf-8')
    assert 'APP_PING_INTERVAL_S' in src
    assert '_ping_loop' in src
    assert '_await_subscribe_ack' in src
    assert 'last_broker_ts' in src
    apply = inspect.getsource(stream_mod.CapitalQuoteStream._apply_quote_payload)
    assert '_apply_stream_quote' in apply
