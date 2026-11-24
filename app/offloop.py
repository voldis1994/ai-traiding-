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
_CPU_LOCK = threading.Lock()  # serialize brain.tick / weight mutation across workers


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        # Few workers; CPU_LOCK serializes brain mutation across clients.
        _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='nta-offloop')
    return _EXECUTOR


def _guarded(fn: Callable[..., Any], *args, **kwargs) -> Any:
    with _CPU_LOCK:
        return fn(*args, **kwargs)


async def run_sync(fn: Callable[..., Any], *args, timeout: float | None = 30.0, **kwargs) -> Any:
    """Run blocking CPU fn in thread pool; optional timeout. No SQLite here."""
    loop = asyncio.get_running_loop()
    bound = partial(_guarded, fn, *args, **kwargs)
    fut = loop.run_in_executor(_pool(), bound)
    if timeout is None:
        return await fut
    return await asyncio.wait_for(fut, timeout=timeout)


def schedule(fn: Callable[..., Any], *args, **kwargs) -> concurrent.futures.Future:
    """Fire-and-forget CPU work. Caller must not pass Store.db operations."""
    return _pool().submit(partial(_guarded, fn, *args, **kwargs))
