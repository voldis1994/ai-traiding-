"""Offload CPU/AI work off the asyncio event loop (item 11).

IMPORTANT: the Store's shared SQLite connection is NOT thread-safe. Never call
``store.db`` / ``store.candles`` / ``store.set`` from the thread pool. Only pure
CPU work (numpy / market_features / brain.tick) belongs here. Persist results
back on the event-loop thread after ``await run_sync(...)``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from functools import partial
from typing import Any, Callable


_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_CPU_LOCK = threading.Lock()  # only for brain.tick / weight mutation


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        # Parallel non-brain CPU; brain paths opt into serialize=True.
        _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix='nta-offloop')
    return _EXECUTOR


def _guarded(fn: Callable[..., Any], *args, **kwargs) -> Any:
    with _CPU_LOCK:
        return fn(*args, **kwargs)


async def run_sync(
    fn: Callable[..., Any],
    *args,
    timeout: float | None = 30.0,
    serialize: bool = True,
    **kwargs,
) -> Any:
    """Run blocking CPU fn in thread pool; optional timeout. No SQLite here.

    serialize=True (default): hold CPU_LOCK for brain/weight safety.
    serialize=False: allow parallel feature/candle work across clients.
    """
    loop = asyncio.get_running_loop()
    bound = (
        partial(_guarded, fn, *args, **kwargs)
        if serialize else partial(fn, *args, **kwargs))
    fut = loop.run_in_executor(_pool(), bound)
    if timeout is None:
        return await fut
    return await asyncio.wait_for(fut, timeout=timeout)


def schedule(
    fn: Callable[..., Any],
    *args,
    serialize: bool = False,
    **kwargs,
) -> concurrent.futures.Future:
    """Fire-and-forget CPU work. Caller must not pass Store.db operations."""
    bound = (
        partial(_guarded, fn, *args, **kwargs)
        if serialize else partial(fn, *args, **kwargs))
    return _pool().submit(bound)
