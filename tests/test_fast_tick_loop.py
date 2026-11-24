"""Worker quote loop must keep mid/UPL near Capital tick rate."""
import inspect
from pathlib import Path

from app import runtime_hub
from app import stream as stream_mod
from app import machine_spec
from app import main as main_mod


def test_worker_guarantees_light_window_after_poll():
    """After poll, must sleep/light-tick — never back-to-back heavy poll."""
    src = inspect.getsource(runtime_hub.RuntimeHub._worker)
    assert 'asyncio.sleep(3 if' not in src
    assert 'light_n' in src
    assert 'gap=0.05' in src or '0.05' in src
    assert 'quote_upl_tick' in src
    assert '_apply_stream_quote' in src
    # Gated heavy-only loop (no post-poll light) must not return.
    assert 'heavy_gap' not in src


def test_legacy_worker_also_has_light_window():
    src = inspect.getsource(main_mod.worker)
    assert 'light_n' in src
    assert '0.05' in src
    assert 'heavy_gap' not in src


def test_overview_truth_refresh_is_time_bounded():
    src = inspect.getsource(main_mod.admin_overview)
    assert 'wait_for' in src
    assert 'refresh_live_truth' in src
    assert '0.4' in src


def test_funds_mirror_not_awaited_on_desk_poll():
    """Overview wait_for(0.4) must not cancel Funds HTTP — schedule in background."""
    src = Path('app/runtime_hub.py').read_text(encoding='utf-8')
    assert '_bg_mirror_capital_funds' in src
    assert 'mirror_scheduled' in src
    assert 'create_task' in src
    assert 'DESK_TRUTH_REV = 266' in src


def test_stream_pushes_quote_into_engine_market():
    src = inspect.getsource(stream_mod.CapitalQuoteStream.run)
    assert '_apply_stream_quote' in src


def test_upl_tick_throttle_is_sub_half_second():
    from app.engine import SingleTradeEngine
    src = inspect.getsource(SingleTradeEngine.quote_upl_tick)
    assert '0.15' in src
    assert '<0.35' not in src


def test_entry_zone_retry_is_quarter_second():
    from app.engine import SingleTradeEngine
    src = inspect.getsource(SingleTradeEngine._retry_entry_when_ohlc_ready)
    assert '<0.25' in src
    assert '<0.8' not in src


def test_machine_spec_documents_fast_cadence():
    assert machine_spec.POLL_SECONDS <= 1
    assert machine_spec.FAST_ENTRY_TICK_SECONDS <= 0.1
