"""
queue_dispatcher.py — gor://a Async Task Dispatch Queue (lightweight)

Purpose:
- Non-blocking AI calls
- Used for slow: SD → upscale → vision → pdf long parse
- Simple in-process FIFO queue (no redis required for MVP)
"""

from __future__ import annotations
import asyncio
from typing import Callable, Any, Dict


class QueueDispatcher:

    def __init__(self):
        self.queue = asyncio.Queue()

    async def enqueue(self, fn: Callable, *args, **kwargs):
        await self.queue.put((fn, args, kwargs))

    async def worker(self):
        while True:
            fn, args, kwargs = await self.queue.get()
            try:
                await fn(*args, **kwargs)
            except Exception:
                pass
            finally:
                self.queue.task_done()

    def start(self):
        asyncio.create_task(self.worker())
