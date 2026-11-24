"""Worker quote loop must keep mid/UPL near Capital tick rate."""
import inspect

from app import runtime_hub
from app import stream as stream_mod


def test_worker_uses_subsecond_quote_bursts_not_3s_idle():
    src = inspect.getsource(runtime_hub.RuntimeHub._worker)
    assert 'asyncio.sleep(3' not in src
    assert 'gap=0.25' in src or 'gap = 0.25' in src
    assert 'bursts' in src
    assert 'quote_upl_tick' in src
    assert '_apply_stream_quote' in src


def test_stream_pushes_quote_into_engine_market():
    src = inspect.getsource(stream_mod.CapitalQuoteStream.run)
    assert '_apply_stream_quote' in src


def test_upl_tick_throttle_is_sub_half_second():
    from app.engine import SingleTradeEngine
    src = inspect.getsource(SingleTradeEngine.quote_upl_tick)
    assert '0.35' in src
    assert '0.85' not in src
