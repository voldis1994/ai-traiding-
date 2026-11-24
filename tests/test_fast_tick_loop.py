"""Worker quote loop must keep mid/UPL near Capital tick rate."""
import inspect

from app import runtime_hub
from app import stream as stream_mod
from app import machine_spec


def test_worker_light_ticks_gate_heavy_poll():
    """Heavy poll must not run every cycle — light path dominates."""
    src = inspect.getsource(runtime_hub.RuntimeHub._worker)
    assert 'asyncio.sleep(3 if' not in src
    assert 'heavy_gap' in src
    assert '0.05' in src
    assert '0.80' in src or '0.8' in src
    assert 'quote_upl_tick' in src
    assert '_apply_stream_quote' in src
    # Old burst-after-every-poll pattern must be gone.
    assert 'gap=0.10' not in src and 'gap = 0.10' not in src


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
