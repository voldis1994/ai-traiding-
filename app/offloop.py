"""Offload heavy SQLite writes and CPU/AI work off the asyncio event loop (item 11).

Light path (quotes/UPL hints) must never await these. Heavy poll may schedule
and optionally await with a timeout.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from functools import partial
from typing import Any, Callable


_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='nta-offloop')
    return _EXECUTOR


async def run_sync(fn: Callable[..., Any], *args, timeout: float | None = 30.0, **kwargs) -> Any:
    """Run blocking fn in thread pool; optional timeout."""
    loop = asyncio.get_running_loop()
    bound = partial(fn, *args, **kwargs)
    fut = loop.run_in_executor(_pool(), bound)
    if timeout is None:
        return await fut
    return await asyncio.wait_for(fut, timeout=timeout)


def schedule(fn: Callable[..., Any], *args, **kwargs) -> concurrent.futures.Future:
    """Fire-and-forget blocking work (do not block event loop)."""
    return _pool().submit(partial(fn, *args, **kwargs))
